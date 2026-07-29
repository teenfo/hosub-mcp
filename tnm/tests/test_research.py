"""증권사 리서치 수집 — 파싱·매칭·러너.

실 네트워크·DB 를 쓰지 않는다. HTML 픽스처는 2026-07-29 실제 응답에서
구조만 남기고 줄인 것이다(칸 수·클래스·링크 형식은 그대로).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import brokers as broker_reg
from app.collect import CollectRunner
from app.collectors import research

KST = ZoneInfo("Asia/Seoul")


# ---------------- 네이버 픽스처 ----------------

NAVER_COMPANY = """
<table summary="종목분석 리포트 게시판 글목록" class="type_1">
<tr><th>종목명</th><th>제목</th><th>증권사</th><th>첨부</th><th>작성일</th><th>조회수</th></tr>
<tr>
  <td style="padding-left:10"><a href="/item/main.naver?code=000660" title="SK하이닉스" class="stock_item">SK하이닉스</a></td>
  <td><a href="company_read.naver?nid=94703&amp;page=1">2Q26 Conference call</a><img src="x.gif" class="ico_new" alt="NEW"></td>
  <td>SK증권</td>
  <td class="file"><a href="https://stock.pstatic.net/a/20260729_company_77392000.pdf" target="_blank"><img src="down.gif" alt="pdf"></a></td>
  <td class="date" style="padding-left:5px">26.07.29</td>
  <td class="date">9486</td>
</tr>
<tr>
  <td style="padding-left:10"><a href="/item/main.naver?code=138930" title="BNK금융지주" class="stock_item">BNK금융지주</a></td>
  <td><a href="company_read.naver?nid=94702&amp;page=1">이익 추정치 하향에도 높은 환원 수익률</a></td>
  <td>한화투자증권</td>
  <td class="file"></td>
  <td class="date" style="padding-left:5px">26.07.28</td>
  <td class="date">1998</td>
</tr>
</table>
"""

NAVER_MARKET = """
<table summary="시황정보 리포트 게시판 글목록" class="type_1">
<tr><th>제목</th><th>증권사</th><th>첨부</th><th>작성일</th><th>조회수</th></tr>
<tr>
  <td style="padding-left:10px"><a href="market_info_read.naver?nid=36945&amp;page=1">마켓레이더 - 높았던 기대</a></td>
  <td>유안타 리서치</td>
  <td class="file"><a href="https://stock.pstatic.net/b/20260729_market.pdf"><img src="down.gif" alt="pdf"></a></td>
  <td class="date">26.07.29</td>
  <td class="date">312</td>
</tr>
</table>
"""

NAVER_DETAIL = """
<div class="view_sm">
  <em>SK하이닉스</em> 목표가 <em class="money">320,000</em> 투자의견 <em class="coment">매수</em>
</div>
"""

NAVER_DETAIL_NONE = """목표가 <em class="money">없음</em> 투자의견 <em class="coment">없음</em>"""


CONSENSUS = """
<table class="table_style01">
<tr><th class="first">작성일</th><th>제목</th><th>적정가격</th><th>투자의견</th><th>작성자</th><th>제공출처</th><th>첨부</th></tr>
<tr>
  <td class="first txt_number">2026-07-29</td>
  <td class="text_l">
    <a href="/analysis/downpdf?report_idx=651245" target="_blank">한화시스템(272210) 어닝 서프라이즈 기록</a>
    <div class="layerPop"><div id="content_651245" class="pop01 disNone">
      <strong>한화시스템(272210) 어닝 서프라이즈 기록</strong>
      <ul><li>어닝 서프라이즈 기록</li></ul>
    </div></div>
  </td>
  <td class="text_r txt_number">100,000</td>
  <td>
Buy                </td>
  <td>이재</td>
  <td>LS증권</td>
  <td><div class="txt_file"><!-- <a href="/x">pdf</a> --></div></td>
</tr>
</table>
"""


# ---------------- 파싱 ----------------

def test_naver_company_rows():
    rows = research.parse_naver_list(NAVER_COMPANY, "company")
    assert len(rows) == 2
    a = rows[0]
    assert a.ticker == "000660"
    assert a.stock_name == "SK하이닉스"
    assert a.broker == "SK증권"
    assert a.title == "2Q26 Conference call"
    assert a.source_uid == "company:94703"
    assert a.pdf_url.endswith(".pdf")
    assert a.published_at == datetime(2026, 7, 29, tzinfo=KST)
    # 첨부가 없어도 행은 살아남는다 — PDF 는 부가 정보다
    assert rows[1].pdf_url is None
    assert rows[1].ticker == "138930"


def test_naver_market_has_no_ticker():
    """칸이 5개인 분류(시황·경제·투자정보)는 종목이 없다.

    뒤에서부터 읽기 때문에 칸 수가 달라도 증권사·작성일이 어긋나지 않는다 —
    앞에서부터 세면 여기서 종목명 칸을 제목으로 읽는다.
    """
    rows = research.parse_naver_list(NAVER_MARKET, "market")
    assert len(rows) == 1
    r = rows[0]
    assert r.ticker is None and r.stock_name is None
    assert r.broker == "유안타 리서치"
    assert r.title == "마켓레이더 - 높았던 기대"
    assert r.category == "market"


def test_naver_detail_target_and_none():
    got = research.parse_naver_detail(NAVER_DETAIL)
    assert got == {"target_price": 320_000, "opinion": "매수"}
    # '없음' 을 문자열로 저장하면 나중에 목표가 유무를 셀 수 없다
    assert research.parse_naver_detail(NAVER_DETAIL_NONE) == {
        "target_price": None, "opinion": None}


# 산업·시장·채권은 칸 배치가 기업과 다르다(실측 2026-07-29). 고정 인덱스로
# 읽으면 이 셋이 통째로 0건이 된다 — 그 회귀를 막는 픽스처다.
CONSENSUS_INDUSTRY = """
<table><tr>
  <td class="first txt_number">2026-07-29</td>
  <td class="text_l"><a href="/analysis/downpdf?report_idx=651200">더욱 확대된 격차 : 2Q26 Scorecard</a>
    <div class="layerPop"><ul><li>격차 확대</li></ul></div></td>
  <td>-</td>
  <td>설용진</td>
  <td>iM증권</td>
  <td></td><td></td>
</tr></table>
"""

CONSENSUS_MARKET = """
<table><tr>
  <td class="first txt_number">2026-07-29</td>
  <td class="text_l"><a href="/analysis/downpdf?report_idx=651100">Start With IBKS</a></td>
  <td>투자분석부</td>
  <td>IBK투자증권</td>
  <td></td><td></td>
</tr></table>
"""

# 채권은 **제목이 두 번째 칸이 아니다** — 분류가 먼저 온다.
CONSENSUS_BOND = """
<table><tr>
  <td class="first txt_number">2026-07-27</td>
  <td>채권</td>
  <td class="text_l"><a href="/analysis/downpdf?report_idx=650900">연준의 금리인상, 모든 것이 틀어진다</a></td>
  <td>윤여삼,김영준</td>
  <td>메리츠증권</td>
  <td></td>
</tr></table>
"""


def test_consensus_industry_layout():
    rows = research.parse_consensus_list(CONSENSUS_INDUSTRY, "industry")
    assert len(rows) == 1
    r = rows[0]
    assert r.broker == "iM증권" and r.analyst == "설용진"
    assert r.target_price is None, "산업 리포트의 '-' 는 목표가가 아니다"
    assert r.ticker is None


def test_consensus_market_layout():
    r = research.parse_consensus_list(CONSENSUS_MARKET, "market")[0]
    assert r.broker == "IBK투자증권" and r.analyst == "투자분석부"
    assert r.title == "Start With IBKS"


def test_consensus_bond_title_is_not_second_column():
    r = research.parse_consensus_list(CONSENSUS_BOND, "economy")[0]
    assert r.title == "연준의 금리인상, 모든 것이 틀어진다"
    assert r.broker == "메리츠증권" and r.analyst == "윤여삼,김영준"


def test_consensus_opinion_shifted_into_analyst_column():
    """작성자가 비면 투자의견이 그 칸으로 밀린다 — 사람 이름으로 저장하면 안 된다."""
    html = CONSENSUS.replace("<td>이재</td>", "<td></td>")
    r = research.parse_consensus_list(html, "company")[0]
    assert r.analyst is None
    assert r.opinion == "Buy"
    assert r.broker == "LS증권"


def test_consensus_row():
    rows = research.parse_consensus_list(CONSENSUS, "company")
    assert len(rows) == 1
    r = rows[0]
    assert r.ticker == "272210"
    assert r.stock_name == "한화시스템"
    assert r.title == "어닝 서프라이즈 기록"     # 종목명(코드) 접두는 칼럼으로 뺀다
    assert r.target_price == 100_000
    assert r.opinion == "Buy"
    assert r.analyst == "이재"
    assert r.broker == "LS증권"
    assert r.summary == "어닝 서프라이즈 기록"
    assert r.source == "consensus" and r.source_uid == "651245"


def test_consensus_title_excludes_hover_summary():
    """마우스오버 요약(div.layerPop)이 제목에 섞이면 안 된다."""
    r = research.parse_consensus_list(CONSENSUS, "company")[0]
    assert "layerPop" not in r.title
    assert r.title.count("어닝 서프라이즈 기록") == 1


def test_html_comment_does_not_leak_as_text():
    """주석 안에 '>' 가 있으면 태그 정규식이 끊겨 '-->' 가 본문으로 샌다.

    실측(2026-07-29): 한경 기업 리포트의 마지막 칸이 '-->' 로 읽혀 '마지막
    비어 있지 않은 칸' 이 그쪽으로 잡히고 증권사가 작성자 칸으로 밀렸다.
    """
    assert research._text('<td><div><!-- <a href="/x">pdf</a> --></div></td>') == ""
    r = research.parse_consensus_list(CONSENSUS, "company")[0]
    assert r.broker == "LS증권" and r.analyst == "이재"


def test_parse_empty_html_is_zero_not_error():
    """구조가 바뀌면 예외가 아니라 0건 — 러너가 경고로 잡는다."""
    assert research.parse_naver_list("<html>바뀐 구조</html>", "company") == []
    assert research.parse_consensus_list("<html></html>", "company") == []


def test_parse_ymd_forms():
    assert research.parse_ymd("26.07.29", two_digit_year=True).year == 2026
    assert research.parse_ymd("2026-07-29").month == 7
    assert research.parse_ymd("헛소리") is None


# ---------------- 증권사 매칭 ----------------

def test_alias_matching_unifies_naming():
    idx = broker_reg.build_index([
        {"name": "유안타증권", "aliases": ["유안타 리서치", "동양증권"], "enabled": True},
    ])
    for raw in ("유안타증권", "유안타 리서치", "동양증권", " 유안타리서치 "):
        assert broker_reg.match(raw, idx)["name"] == "유안타증권"


def test_unknown_broker_is_none_not_error():
    idx = broker_reg.build_index([{"name": "SK증권", "aliases": [], "enabled": True}])
    assert broker_reg.match("처음보는증권", idx) is None


def test_alias_does_not_override_registered_name():
    """별칭이 다른 회사의 정식명을 덮으면 그 회사 리포트가 통째로 오귀속된다."""
    idx = broker_reg.build_index([
        {"name": "하나증권", "aliases": [], "enabled": True},
        {"name": "다른증권", "aliases": ["하나증권"], "enabled": True},
    ])
    assert broker_reg.match("하나증권", idx)["name"] == "하나증권"


def test_norm_strips_research_dept_but_keeps_securities():
    assert broker_reg.norm("삼성증권 리서치센터") == broker_reg.norm("삼성증권")
    # '증권' 을 떼면 '하나증권'과 '하나금융지주'가 같아진다
    assert broker_reg.norm("하나증권") != broker_reg.norm("하나")


def test_seed_covers_both_kinds_and_marks_foreign_limit():
    rows = broker_reg.seed_rows()
    names = {r["name"] for r in rows}
    assert "NH투자증권" in names and "골드만삭스" in names
    foreign = [r for r in rows if r["kind"] == "foreign"]
    assert foreign and all(r["note"] for r in foreign), "해외사는 한계를 note 로 남긴다"
    assert len(names) == len(rows), "시드에 중복 이름이 없어야 한다"


# 실측 전수조사(2026-07-29, 네이버 5분류×5p + 한경 4종×30일)에서 소스에 실제로
# 나타난 21개. 시드가 이걸 전부 덮지 못하면 '미등록' 경고가 상시로 떠서,
# 정작 진짜로 놓친 증권사를 가린다.
_OBSERVED = [
    "DS투자증권", "IBK투자증권", "KB증권", "LS증권", "SK증권", "iM증권", "교보증권",
    "다올투자증권", "대신증권", "메리츠증권", "미래에셋증권", "상상인증권",
    "신한투자증권", "우리은행", "유안타증권", "유진투자증권", "키움증권", "하나증권",
    "한국IR협의회", "한화투자증권", "현대차증권",
]


@pytest.mark.parametrize("name", _OBSERVED)
def test_seed_covers_every_observed_publisher(name):
    idx = broker_reg.build_index(broker_reg.seed_rows())
    assert broker_reg.match(name, idx) is not None, f"{name} 이 미등록으로 남는다"


def test_non_broker_publishers_are_labelled():
    """증권사가 아닌 발행기관은 note 로 구분한다 — 목록에서 성격이 보여야 한다."""
    rows = {r["name"]: r for r in broker_reg.seed_rows()}
    assert "증권사가 아님" in (rows["한국IR협의회"]["note"] or "")
    assert "증권사가 아님" in (rows["우리은행"]["note"] or "")


# ---------------- 해외 인용 ----------------

def _rss(*titles: str) -> str:
    items = "".join(
        f"<item><title>{t}</title><link>https://n.example/{i}</link>"
        f"<description>본문</description>"
        f"<pubDate>Wed, 29 Jul 2026 01:00:00 GMT</pubDate>"
        f"<source>연합뉴스</source></item>" for i, t in enumerate(titles))
    return f'<?xml version="1.0"?><rss><channel>{items}</channel></rss>'


RSS = """<?xml version="1.0"?><rss><channel>
<item><title>골드만삭스, 삼성전자 목표가 상향</title>
 <link>https://n.example/1</link><description>목표주가 12만원</description>
 <pubDate>Wed, 29 Jul 2026 01:00:00 GMT</pubDate><source>연합뉴스</source></item>
<item><title>지난주 기사</title>
 <link>https://n.example/0</link><description>옛것</description>
 <pubDate>Mon, 20 Jul 2026 01:00:00 GMT</pubDate><source>연합뉴스</source></item>
</channel></rss>"""


def test_foreign_feed_respects_cursor_and_marks_quote():
    b = {"name": "골드만삭스", "aliases": ["Goldman Sachs"]}
    since = datetime(2026, 7, 25, tzinfo=timezone.utc)
    rows = research.parse_foreign_feed(RSS, b, since)
    assert len(rows) == 1
    r = rows[0]
    assert r.broker == "골드만삭스" and r.source == "news"
    assert r.evidence.get("quoted") is True, "원문이 아니라 인용임을 표시해야 한다"


def test_foreign_query_has_two_forms():
    b = {"name": "골드만삭스", "aliases": ["Goldman Sachs"]}
    stock = research.foreign_query(b)
    market = research.foreign_query(b, market=True)
    assert "골드만삭스" in stock and "Goldman Sachs" in stock
    assert "목표주가" in stock, "회사명만 넣으면 채용·실적 기사가 섞인다"
    assert "코스피" in market and "코스피" not in stock


# 실측 표본(2026-07-29). 앞 넷은 버려야 할 것, 뒤 넷은 살려야 할 것이다.
_NOISE = [
    "골드만삭스, 제미니 스페이스 스테이션 투자의견 ’매도’ 하향 조정",
    "모건 스탠리, 치즈케이크 팩토리 주식 투자의견 상향... 매출 호조",
    "JP모건, 라이너보드 가격 상승 속 인터내셔널 페이퍼 투자의견 상향",
    "골드만삭스, AMD 목표주가 450→640달러로 대폭 상향",
]
_KEEP = [
    "골드만삭스 \"코스피 목표치 9,000→12,000…강한 실적 모멘텀\"",       # 시장 용어
    "맥쿼리 SK하이닉스 목표주가 290만 원으로 상향",                      # 상장사명
    "노무라 \"삼전 목표가 59만원→67만원…2분기 실적 호조 전망\"",          # 축약
    "JP모건, 한국 증시 ’비중 확대’ 의견 유지",                           # 시장 용어
]
_NAMES = {"삼성전자", "SK하이닉스", "현대차", "NAVER", "대한항공"}


@pytest.mark.parametrize("title", _NOISE)
def test_us_stock_quotes_are_filtered_out(title):
    """Investing.com 이 번역한 미국 종목 기사 — 국내 매매에 쓸 값이 아니다."""
    assert not research.is_korea_related(title, _NAMES)


@pytest.mark.parametrize("title", _KEEP)
def test_korea_related_quotes_survive(title):
    assert research.is_korea_related(title, _NAMES)


def test_two_char_names_do_not_match():
    """'대한'·'삼양' 같은 두 글자 상장사명은 일상어와 충돌해 오탐을 만든다."""
    assert not research.is_korea_related("골드만삭스, 대한 전망을 밝게 봤다",
                                         {"대한", "삼양"})


def test_no_name_cache_falls_back_to_market_terms_only():
    """캐시가 없으면 모르는 것을 통과시키지 않고 좁게 잡는다."""
    assert research.is_korea_related("골드만삭스, 코스피 목표치 상향", set())
    assert not research.is_korea_related("맥쿼리 SK하이닉스 목표주가 상향", set())


def test_stock_query_requires_korea_but_market_query_does_not():
    b = {"name": "골드만삭스", "aliases": []}
    xml = _rss(*_NOISE[:1], *_KEEP[1:2])
    wide = research.parse_foreign_feed(xml, b, None, _NAMES, require_korea=False)
    narrow = research.parse_foreign_feed(xml, b, None, _NAMES, require_korea=True)
    assert len(wide) == 2
    assert [r.title for r in narrow] == [_KEEP[1]]


# ---------------- 러너 ----------------

class _FakeDB:
    def __init__(self, brokers, watch=None):
        self._brokers = brokers
        self._watch = watch or {}
        self.reports: list[dict] = []
        self.raw: list[tuple[int, str, list[dict]]] = []
        self.ingested: set[str] = set()
        self.discovered: list[dict] = []
        self.bumped: dict[str, int] = {}
        self.linked = 0
        self.state: dict[str, str] = {}
        self.ready = True

    async def seed_brokers(self, rows):
        return 0

    async def list_brokers(self, enabled_only=False):
        return self._brokers

    async def known_report_keys(self, keys):
        have = {(r["source"], r["source_uid"]) for r in self.reports}
        return {k for k in keys if k in have}

    async def pending_report_ingest(self, days=7, limit=200, max_attempts=3):
        """적재 대기 = 종목이 붙었는데 아직 raw_items 로 안 간 것."""
        return [dict(r, id=i) for i, r in enumerate(self.reports)
                if r.get("ticker") and r["source_uid"] not in self.ingested]

    async def bump_report_attempts(self, ids):
        return len(ids)

    async def known_tickers(self):
        return set(self._watch)

    async def count_origin(self, origin):
        return 0

    async def add_discovered(self, rows, tier="other", origin="dart"):
        for i, r in enumerate(rows):
            self._watch[r["ticker"]] = 500 + i
        self.discovered.extend(rows)
        return len(rows)

    async def insert_reports(self, rows):
        self.reports.extend(rows)
        return len(rows)

    async def watch_ids_by_ticker(self):
        return self._watch

    async def insert_raw_items(self, wid, source, rows):
        self.raw.append((wid, source, rows))
        self.ingested.update(r["source_uid"].split(":", 1)[1] for r in rows)
        return len(rows)

    async def link_report_items(self):
        self.linked += 1
        return 1

    async def bump_brokers(self, counts, seen_at):
        self.bumped = counts
        return len(counts)

    async def get_state(self, key):
        return self.state.get(key)

    async def set_state(self, key, value):
        self.state[key] = value


def _use_db(monkeypatch, fake):
    """collect 와 ingest 둘 다 db 를 모듈 전역으로 잡는다 — 한쪽만 갈면 진짜 DB 를 친다."""
    from app import collect as collect_mod
    from app import ingest as ingest_mod

    monkeypatch.setattr(collect_mod, "db", fake)
    monkeypatch.setattr(ingest_mod, "db", fake)


def _report(broker, uid="1", ticker=None, cat="company"):
    return research.Report(
        source="naver", source_uid=uid, broker=broker, category=cat,
        title="제목", url="https://x/1", published_at=datetime(2026, 7, 29, tzinfo=KST),
        ticker=ticker, stock_name="종목" if ticker else None)


@pytest.fixture
def runner(monkeypatch):
    r = CollectRunner()
    monkeypatch.setattr(research, "fetch_consensus",
                        lambda **kw: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(research, "fill_naver_targets",
                        lambda *a, **kw: asyncio.sleep(0, result=0))
    return r


def test_disabled_broker_reports_are_not_stored(runner, monkeypatch):
    """토글이 실제로 수집을 막는가 — 이 기능의 핵심."""
    fake = _FakeDB([
        {"name": "SK증권", "kind": "domestic", "enabled": True, "aliases": []},
        {"name": "한화투자증권", "kind": "domestic", "enabled": False, "aliases": []},
    ])
    _use_db(monkeypatch, fake)
    monkeypatch.setattr(research, "fetch_naver", lambda **kw: asyncio.sleep(
        0, result=[_report("SK증권", "1"), _report("한화투자증권", "2")]))

    out = asyncio.run(runner.run_once("research"))
    assert out["disabled_skip"] == 1
    assert [r["broker"] for r in fake.reports] == ["SK증권"]


def test_unregistered_broker_is_kept_and_counted(runner, monkeypatch):
    """미등록 이름은 버리지 않는다 — 놓친 이름이 있다는 사실이 정보다."""
    fake = _FakeDB([{"name": "SK증권", "kind": "domestic", "enabled": True, "aliases": []}])
    _use_db(monkeypatch, fake)
    monkeypatch.setattr(research, "fetch_naver", lambda **kw: asyncio.sleep(
        0, result=[_report("처음보는증권", "9")]))

    out = asyncio.run(runner.run_once("research"))
    assert out["unregistered"] == 1
    assert [r["broker"] for r in fake.reports] == ["처음보는증권"]


def test_alias_name_is_normalized_before_storing(runner, monkeypatch):
    """'유안타 리서치'로 들어와도 '유안타증권'으로 저장돼야 집계가 갈리지 않는다."""
    fake = _FakeDB([{"name": "유안타증권", "kind": "domestic", "enabled": True,
                     "aliases": ["유안타 리서치"]}])
    _use_db(monkeypatch, fake)
    monkeypatch.setattr(research, "fetch_naver", lambda **kw: asyncio.sleep(
        0, result=[_report("유안타 리서치", "3")]))

    asyncio.run(runner.run_once("research"))
    assert fake.reports[0]["broker"] == "유안타증권"
    assert fake.bumped == {"유안타증권": 1}


def test_every_stock_report_enters_analysis_pipeline(runner, monkeypatch):
    """종목이 붙은 리포트는 **관심종목 밖이어도** 분석을 탄다.

    종전에는 이미 감시 중인 종목만 태워, 268건 중 2건만 파이프라인에 들어갔다.
    DART 가 미매칭 공시로 신규 종목을 발굴하는 것과 같은 판단을 적용한다.
    """
    fake = _FakeDB([{"name": "SK증권", "kind": "domestic", "enabled": True, "aliases": []}],
                   watch={"000660": 7})
    _use_db(monkeypatch, fake)
    monkeypatch.setattr(research, "fetch_naver", lambda **kw: asyncio.sleep(0, result=[
        _report("SK증권", "1", ticker="000660"),      # 이미 관심종목
        _report("SK증권", "2", ticker="999999"),      # 관심종목 밖 → 등록 후 적재
        _report("SK증권", "3", cat="industry"),        # 종목 없음 → 원장에만
    ]))

    out = asyncio.run(runner.run_once("research"))
    assert out["inserted"] == 3, "원장에는 셋 다 남는다"
    assert out["ingest_inserted"] == 2, "종목이 붙은 둘 다 분석 큐로"
    assert out["ingest_registered"] == 1, "관심종목 밖 종목은 등록하고 태운다"
    assert [d["ticker"] for d in fake.discovered] == ["999999"]
    assert fake.linked == 1
    uids = {r["source_uid"] for _, _, rows in fake.raw for r in rows}
    assert uids == {"naver:1", "naver:2"}


def test_industry_report_has_no_stock_to_attach(runner, monkeypatch):
    """산업·시황 리포트는 종목이 없어 raw_items 에 들어갈 수 없다(원장에만 남는다)."""
    fake = _FakeDB([{"name": "SK증권", "kind": "domestic", "enabled": True, "aliases": []}])
    _use_db(monkeypatch, fake)
    monkeypatch.setattr(research, "fetch_naver", lambda **kw: asyncio.sleep(
        0, result=[_report("SK증권", "9", cat="market")]))

    out = asyncio.run(runner.run_once("research"))
    assert out["inserted"] == 1
    assert out.get("ingest_inserted", 0) == 0
    assert fake.raw == []


def test_ingest_runs_even_when_nothing_new_was_collected(runner, monkeypatch):
    """수집 신규가 0건이어도 지난 사이클에 밀린 적재분은 처리돼야 한다.

    수집과 적재를 붙여 두면 상한에 걸린 리포트가 영영 큐에 못 들어간다.
    """
    fake = _FakeDB([{"name": "SK증권", "kind": "domestic", "enabled": True, "aliases": []}],
                   watch={"000660": 7})
    fake.reports.append({"source": "naver", "source_uid": "old", "broker": "SK증권",
                         "category": "company", "ticker": "000660",
                         "stock_name": "SK하이닉스", "title": "지난 사이클 리포트",
                         "summary": None, "url": "https://x/old", "pdf_url": None,
                         "analyst": None, "target_price": None, "opinion": None,
                         "published_at": datetime(2026, 7, 29, tzinfo=KST)})
    _use_db(monkeypatch, fake)
    monkeypatch.setattr(research, "fetch_naver", lambda **kw: asyncio.sleep(0, result=[]))

    out = asyncio.run(runner.run_once("research"))
    assert out["kept"] == 0 and out["inserted"] == 0
    assert out["ingest_inserted"] == 1


def test_raw_body_carries_target_price():
    """목표가가 칼럼에만 있으면 분류 LLM 이 못 읽는다 — 본문에 옮겨야 한다."""
    row = CollectRunner()._report_to_raw({
        "source": "naver", "source_uid": "1", "broker": "SK증권",
        "title": "목표가 상향", "summary": "실적 호조", "url": "https://x/1",
        "target_price": 320_000, "opinion": "매수", "analyst": "홍길동",
        "published_at": datetime(2026, 7, 29, tzinfo=KST)})
    assert "320,000원" in row["body"] and "투자의견 매수" in row["body"]
    assert row["source_uid"] == "naver:1"      # link_report_items 의 결합 규약
    assert row["content_hash"]


def test_already_collected_reports_are_not_reprocessed(runner, monkeypatch):
    """목록은 매 사이클 같은 건을 다시 준다 — 두 번째 사이클은 아무 일도 없어야."""
    fake = _FakeDB([{"name": "SK증권", "kind": "domestic", "enabled": True, "aliases": []}])
    _use_db(monkeypatch, fake)
    monkeypatch.setattr(research, "fetch_naver", lambda **kw: asyncio.sleep(
        0, result=[_report("SK증권", "1"), _report("SK증권", "2")]))

    detail_calls = []

    async def _fill(reports, limit=20):
        detail_calls.append(len(reports))
        return 0

    monkeypatch.setattr(research, "fill_naver_targets", _fill)

    first = asyncio.run(runner.run_once("research"))
    second = asyncio.run(runner.run_once("research"))
    assert first["inserted"] == 2 and second["inserted"] == 0
    assert second["kept"] == 0
    assert detail_calls == [2], "이미 받은 건은 상세를 다시 조회하지 않는다"
    assert fake.bumped == {"SK증권": 2}, "누적 건수가 매 사이클 불어나면 안 된다"


def test_source_failure_is_isolated(runner, monkeypatch):
    """한 소스가 죽어도 나머지 소스로 사이클이 성립해야 한다."""
    fake = _FakeDB([{"name": "LS증권", "kind": "domestic", "enabled": True, "aliases": []}])
    _use_db(monkeypatch, fake)

    async def _boom(**kw):
        raise RuntimeError("표 구조 변경")

    monkeypatch.setattr(research, "fetch_naver", _boom)
    monkeypatch.setattr(research, "fetch_consensus", lambda **kw: asyncio.sleep(
        0, result=[_report("LS증권", "5")]))

    out = asyncio.run(runner.run_once("research"))
    assert out["errors"] == 1
    assert out["inserted"] == 1


def test_foreign_cursor_advances(runner, monkeypatch):
    """해외 인용은 커서로 증분 수집한다 — 매 사이클 같은 기사를 다시 받지 않게."""
    fake = _FakeDB([{"name": "골드만삭스", "kind": "foreign", "enabled": True,
                     "aliases": ["Goldman Sachs"]}])
    _use_db(monkeypatch, fake)
    monkeypatch.setattr(research, "fetch_naver", lambda **kw: asyncio.sleep(0, result=[]))

    seen = datetime(2026, 7, 29, 10, tzinfo=timezone.utc)

    async def _foreign(b, since, initial_days=3, names=None):
        r = _report("골드만삭스", "f1")
        r.source, r.published_at = "news", seen
        return [r]

    monkeypatch.setattr(research, "fetch_foreign", _foreign)
    asyncio.run(runner.run_once("research"))
    assert fake.state["research_foreign_seen"]
    assert seen.isoformat() in fake.state["research_foreign_seen"]


def test_disabled_by_config(monkeypatch):
    from app import collect as collect_mod
    from app import settings as tnm_settings

    monkeypatch.setattr(collect_mod, "db", _FakeDB([]))
    monkeypatch.setitem(tnm_settings.COLLECT, "research", {"enabled": False})
    out = asyncio.run(CollectRunner().run_once("research"))
    assert out.get("skipped") == "비활성"
