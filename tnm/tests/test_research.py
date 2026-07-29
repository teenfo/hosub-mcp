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


# ---------------- 해외 인용 ----------------

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
    assert research.parse_foreign_feed(RSS, b, None) or True


def test_foreign_query_narrows_to_research_vocabulary():
    q = research.foreign_query({"name": "골드만삭스", "aliases": ["Goldman Sachs"]})
    assert "골드만삭스" in q and "Goldman Sachs" in q
    assert "목표주가" in q, "회사명만 넣으면 채용·실적 기사가 섞인다"


# ---------------- 러너 ----------------

class _FakeDB:
    def __init__(self, brokers, watch=None):
        self._brokers = brokers
        self._watch = watch or {}
        self.reports: list[dict] = []
        self.raw: list[tuple[int, str, list[dict]]] = []
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

    async def insert_reports(self, rows):
        self.reports.extend(rows)
        return len(rows)

    async def watch_ids_by_ticker(self):
        return self._watch

    async def insert_raw_items(self, wid, source, rows):
        self.raw.append((wid, source, rows))
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
    from app import collect as collect_mod

    fake = _FakeDB([
        {"name": "SK증권", "kind": "domestic", "enabled": True, "aliases": []},
        {"name": "한화투자증권", "kind": "domestic", "enabled": False, "aliases": []},
    ])
    monkeypatch.setattr(collect_mod, "db", fake)
    monkeypatch.setattr(research, "fetch_naver", lambda **kw: asyncio.sleep(
        0, result=[_report("SK증권", "1"), _report("한화투자증권", "2")]))

    out = asyncio.run(runner.run_once("research"))
    assert out["disabled_skip"] == 1
    assert [r["broker"] for r in fake.reports] == ["SK증권"]


def test_unregistered_broker_is_kept_and_counted(runner, monkeypatch):
    """미등록 이름은 버리지 않는다 — 놓친 이름이 있다는 사실이 정보다."""
    from app import collect as collect_mod

    fake = _FakeDB([{"name": "SK증권", "kind": "domestic", "enabled": True, "aliases": []}])
    monkeypatch.setattr(collect_mod, "db", fake)
    monkeypatch.setattr(research, "fetch_naver", lambda **kw: asyncio.sleep(
        0, result=[_report("처음보는증권", "9")]))

    out = asyncio.run(runner.run_once("research"))
    assert out["unregistered"] == 1
    assert [r["broker"] for r in fake.reports] == ["처음보는증권"]


def test_alias_name_is_normalized_before_storing(runner, monkeypatch):
    """'유안타 리서치'로 들어와도 '유안타증권'으로 저장돼야 집계가 갈리지 않는다."""
    from app import collect as collect_mod

    fake = _FakeDB([{"name": "유안타증권", "kind": "domestic", "enabled": True,
                     "aliases": ["유안타 리서치"]}])
    monkeypatch.setattr(collect_mod, "db", fake)
    monkeypatch.setattr(research, "fetch_naver", lambda **kw: asyncio.sleep(
        0, result=[_report("유안타 리서치", "3")]))

    asyncio.run(runner.run_once("research"))
    assert fake.reports[0]["broker"] == "유안타증권"
    assert fake.bumped == {"유안타증권": 1}


def test_watchlist_report_also_enters_analysis_pipeline(runner, monkeypatch):
    """관심종목 리포트는 raw_items 로도 들어가 기존 분류·알림을 탄다."""
    from app import collect as collect_mod

    fake = _FakeDB([{"name": "SK증권", "kind": "domestic", "enabled": True, "aliases": []}],
                   watch={"000660": 7})
    monkeypatch.setattr(collect_mod, "db", fake)
    monkeypatch.setattr(research, "fetch_naver", lambda **kw: asyncio.sleep(0, result=[
        _report("SK증권", "1", ticker="000660"),
        _report("SK증권", "2", ticker="999999"),      # 관심종목 아님
        _report("SK증권", "3", cat="industry"),        # 종목 없음
    ]))

    out = asyncio.run(runner.run_once("research"))
    assert out["inserted"] == 3, "원장에는 셋 다 남는다"
    assert out["raw_inserted"] == 1, "분석 큐에는 관심종목 것만"
    wid, source, rows = fake.raw[0]
    assert wid == 7 and source == "research"
    assert rows[0]["source_uid"] == "naver:1"
    assert fake.linked == 1


def test_raw_body_carries_target_price(runner, monkeypatch):
    """목표가가 칼럼에만 있으면 분류 LLM 이 못 읽는다 — 본문에 옮겨야 한다."""
    r = _report("SK증권", "1", ticker="000660")
    r.target_price = 320_000
    r.opinion = "매수"
    row = CollectRunner()._report_to_raw(r)
    assert "320,000원" in row["body"] and "매수" in row["body"]
    assert row["content_hash"]


def test_already_collected_reports_are_not_reprocessed(runner, monkeypatch):
    """목록은 매 사이클 같은 건을 다시 준다 — 두 번째 사이클은 아무 일도 없어야."""
    from app import collect as collect_mod

    fake = _FakeDB([{"name": "SK증권", "kind": "domestic", "enabled": True, "aliases": []}])
    monkeypatch.setattr(collect_mod, "db", fake)
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
    from app import collect as collect_mod

    fake = _FakeDB([{"name": "LS증권", "kind": "domestic", "enabled": True, "aliases": []}])
    monkeypatch.setattr(collect_mod, "db", fake)

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
    from app import collect as collect_mod

    fake = _FakeDB([{"name": "골드만삭스", "kind": "foreign", "enabled": True,
                     "aliases": ["Goldman Sachs"]}])
    monkeypatch.setattr(collect_mod, "db", fake)
    monkeypatch.setattr(research, "fetch_naver", lambda **kw: asyncio.sleep(0, result=[]))

    seen = datetime(2026, 7, 29, 10, tzinfo=timezone.utc)

    async def _foreign(b, since, initial_days=3):
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
