"""DART 신규 종목 발굴 — 뉴스 소스가 처음으로 새 종목을 낼 수 있게 된다.

지금까지 TNM 관심종목은 trading 감시목록에서만 동기화됐고, 공시는 그 관심종목에
매칭될 때만 적재됐다. 그래서 사이클마다 ~1,120건을 세기만 하고 버렸고
`promote` 는 늘 `already` 만 냈다 — **원리적으로 새 종목이 나올 수 없었다.**

여기서 지키는 것:
  - 일상 공시(정기보고서·지분보고)가 LLM 분류 물량을 터뜨리지 않을 것
  - 비상장사·이미 등록된 종목이 들어오지 않을 것
  - **상한이 실제로 걸릴 것** — 등록할수록 RSS 폴링이 선형으로 늘어난다
  - 사용자가 제외한 종목을 발굴이 되살리지 않을 것
"""
import pytest

from app import discover


def _pair(corp="AAA", ticker="005930", name="삼성전자", report="단일판매ㆍ공급계약체결"):
    return (corp, object(), {"stock_code": ticker, "corp_name": name,
                             "report_nm": report, "rcept_no": "20260727000001"})


# --- ① 보고서명 allowlist ---

@pytest.mark.parametrize("report,expect", [
    ("단일판매ㆍ공급계약체결", "공급계약"),
    ("단일판매·공급계약 체결", "공급계약"),          # 표기 흔들림
    ("매출액또는손익구조30%(대규모법인은15%)이상변경", "실적"),
    ("연결재무제표기준영업(잠정)실적(공정공시)", "실적"),
    ("매매거래정지", "거래정지"),
    ("[기재정정]연결재무제표기준영업(잠정)실적(공정공시)", "실적"),   # 정정은 본문으로 판정
    ("유상증자결정", "자본변동"),
    ("자기주식취득결정", "자본변동"),
])
def test_allowlist_passes_material_reports(report, expect):
    assert discover.classify_report(report) == expect


@pytest.mark.parametrize("report", [
    "분기보고서", "반기보고서", "사업보고서", "감사보고서",
    "주식등의대량보유상황보고서", "임원ㆍ주요주주특정증권등소유상황보고서",
    "주주총회소집공고", "기업설명회(IR)개최",
])
def test_daily_filings_are_rejected(report):
    """전부 통과시키면 LLM 분류 물량이 수십 배가 된다."""
    assert discover.classify_report(report) is None


def test_deny_wins_over_allow():
    """'정정' 은 넓다 — 분기보고서 정정은 매매 신호가 아니다."""
    assert discover.classify_report("[기재정정]분기보고서") is None
    assert discover.classify_report("[기재정정]단일판매ㆍ공급계약체결") == "공급계약"


def test_correction_is_judged_by_what_was_corrected():
    """정정을 통째로 통과시키면 일상 공시의 정정판이 다 들어온다.

    실서버 예행(2026-07-27): 후보 191건 중 59건(31%)이 그런 것들이었다 —
    대규모기업집단현황공시 정정 같은 것. 앞머리를 떼고 본 보고서명으로 판정한다.
    """
    assert discover.strip_correction("[기재정정]단일판매ㆍ공급계약체결") == \
           ("단일판매ㆍ공급계약체결", True)
    assert discover.strip_correction("단일판매ㆍ공급계약체결")[1] is False
    assert discover.classify_report("[기재정정]대규모기업집단현황공시") is None
    assert discover.classify_report("[첨부정정]분기보고서") is None


@pytest.mark.parametrize("report", [
    "증권발행실적보고서",           # '실적' 두 글자에 걸리던 자금조달 사후보고
    "대규모기업집단현황공시",
    "기업지배구조보고서",
    "특수관계인과의내부거래",
])
def test_false_positives_from_the_live_dry_run(report):
    """실서버 예행에서 실제로 통과했던 것들 — 회귀로 못박는다."""
    assert discover.classify_report(report) is None


def test_empty_report_name_is_rejected():
    assert discover.classify_report("") is None
    assert discover.classify_report(None) is None


def test_allowlist_centres_on_measured_categories():
    """실측(2026-07-27)에서 본페로니를 통과한 것은 실적·공급계약 둘뿐이다.

    나머지는 '수익률이 아니라 운영상 반드시 알아야 하는 것' 이라는 별도 근거로
    들어가 있다. 목록이 근거 없이 늘어나는 것을 막는다.
    """
    assert {"실적", "공급계약"} <= set(discover.ALLOW)
    assert set(discover.ALLOW) == {"실적", "공급계약", "거래정지", "자본변동"}


# --- ② 후보 추출 ---

def test_candidates_from_unmatched_only():
    pairs = [_pair(corp="AAA", ticker="000001"), _pair(corp="BBB", ticker="000002")]
    got = discover.candidates(pairs, known={"AAA"}, blocked_tickers=set())
    assert [c["ticker"] for c in got] == ["000002"]


def test_unlisted_companies_are_skipped():
    """비상장사는 stock_code 가 비어 있다 — 매매 대상이 아니다."""
    got = discover.candidates([_pair(ticker="")], known=set(), blocked_tickers=set())
    assert got == []
    got = discover.candidates([_pair(ticker="12345")], known=set(), blocked_tickers=set())
    assert got == []       # 6자리가 아니면 종목코드가 아니다


def test_excluded_tickers_are_skipped():
    """사용자가 제외한 종목은 후보가 되지 않는다."""
    got = discover.candidates([_pair(ticker="005930")], known=set(),
                              blocked_tickers={"005930"})
    assert got == []


def test_one_entry_per_ticker_per_cycle():
    """같은 종목이 하루에 공시를 여러 개 내도 후보는 하나다."""
    pairs = [_pair(ticker="000001", report="단일판매ㆍ공급계약체결"),
             _pair(ticker="000001", report="유상증자결정")]
    got = discover.candidates(pairs, known=set(), blocked_tickers=set())
    assert len(got) == 1 and got[0]["reason"] == "공급계약"   # 첫 통과 사유


def test_candidate_carries_the_evidence():
    """'왜 이 종목이 들어왔나' 를 나중에 물을 수 있어야 한다."""
    got = discover.candidates([_pair()], known=set(), blocked_tickers=set())[0]
    assert got["ticker"] == "005930" and got["name"] == "삼성전자"
    assert got["corp_code"] == "AAA" and got["reason"] == "공급계약"
    assert got["report_nm"] and got["rcept_no"]


def test_non_material_reports_produce_no_candidate():
    got = discover.candidates([_pair(report="분기보고서")], known=set(),
                              blocked_tickers=set())
    assert got == []


def test_empty_input_is_safe():
    assert discover.candidates([], set(), set()) == []


# --- ③ 상한 — 등록할수록 RSS 폴링이 늘어난다 ---

class _FakeDB:
    def __init__(self, dart_count=0, known=(), excluded=()):
        self.dart_count = dart_count
        self._known = set(known)
        self._excluded = set(excluded)
        self.added: list[dict] = []
        self.revived: list[str] = []
        self.tier = None
        self.revive_origin = None

    async def known_tickers(self):
        return self._known | self._excluded

    async def excluded_tickers(self):
        return self._excluded

    async def count_origin(self, origin):
        return self.dart_count

    async def add_discovered(self, rows, tier="other", origin="dart"):
        self.added.extend(rows)
        self.tier = tier
        return len(rows)

    async def revive_for_source(self, tickers, tier, origin):
        live = [t for t in tickers if t not in self._excluded]
        self.revived.extend(live)
        self.tier = tier
        self.revive_origin = origin
        return len(live)


def _collector(monkeypatch, fake, **over):
    import asyncio

    from app import collect as collect_mod
    from app import settings

    monkeypatch.setattr(collect_mod, "db", fake)
    monkeypatch.setitem(settings.COLLECT, "dart", {
        "discover": {"enabled": True, "max_per_cycle": 10, "max_total": 300,
                     "tier": "other"} | over})
    c = collect_mod.CollectRunner.__new__(collect_mod.CollectRunner)
    return lambda pairs, known=frozenset(): asyncio.run(c._discover(list(pairs), set(known)))


def test_per_cycle_cap_is_enforced(monkeypatch):
    fake = _FakeDB()
    run = _collector(monkeypatch, fake, max_per_cycle=3)
    pairs = [_pair(ticker=f"00000{i}") for i in range(1, 9)]
    out = run(pairs)
    assert out["candidates"] == 8 and out["discovered"] == 3
    assert len(fake.added) == 3


def test_total_cap_blocks_everything(monkeypatch):
    """총량을 넘으면 한 건도 안 넣는다 — 넘긴 상태에서 계속 늘리지 않는다."""
    fake = _FakeDB(dart_count=300)
    out = _collector(monkeypatch, fake, max_total=300)([_pair(ticker="000001")])
    assert out["discovered"] == 0 and out.get("discover_capped") is True
    assert fake.added == []


def test_total_cap_limits_the_remainder(monkeypatch):
    """남은 자리만큼만 넣는다."""
    fake = _FakeDB(dart_count=298)
    out = _collector(monkeypatch, fake, max_total=300, max_per_cycle=10)(
        [_pair(ticker=f"00000{i}") for i in range(1, 6)])
    assert out["discovered"] == 2


def test_registered_with_slow_tier(monkeypatch):
    """기본 tier 'trade' 로 넣으면 종목마다 30분 주기 RSS 폴링이 붙는다."""
    fake = _FakeDB()
    _collector(monkeypatch, fake)([_pair(ticker="000001")])
    assert fake.tier == "other"


def test_disabled_switch_stops_everything(monkeypatch):
    fake = _FakeDB()
    out = _collector(monkeypatch, fake, enabled=False)([_pair(ticker="000001")])
    assert out == {} and fake.added == []


def test_excluded_tickers_are_never_touched_by_the_collector(monkeypatch):
    """사용자가 제외한 종목을 발굴이 되살리면 사용자 의도를 덮는 것이다."""
    fake = _FakeDB(excluded={"000001"})
    out = _collector(monkeypatch, fake)([_pair(ticker="000001")])
    assert out["candidates"] == 0
    assert fake.added == [] and fake.revived == []


def test_inactive_ticker_is_revived_not_reregistered(monkeypatch):
    """비활성은 사용자 결정이 아니다 — 관심 공시가 다시 나오면 되살린다.

    종전에는 등록된 적 있는 ticker 전부를 후보에서 뺐다. 그래서 한 번
    감시목록을 스쳐간 종목은 공급계약·실적 공시를 아무리 내도 다시는
    들어오지 못했다.
    """
    fake = _FakeDB(known={"000001"})
    out = _collector(monkeypatch, fake)([_pair(ticker="000001")])
    assert out["candidates"] == 1
    assert fake.revived == ["000001"]
    assert fake.added == [], "신규 등록이 아니라 되살리기다"
    assert out["revived"] == 1 and out["discovered"] == 0
    # origin 을 넘기지 않으면 watchsync 가 30분마다 다시 내려 핑퐁이 된다
    assert fake.revive_origin == "dart" and fake.tier == "other"


def test_revive_and_new_share_one_budget(monkeypatch):
    """되살리기도 폴링 부하가 같다 — 같은 상한을 쓰고, 아는 종목이 먼저다."""
    fake = _FakeDB(known={"000002"})
    run = _collector(monkeypatch, fake, max_per_cycle=1)
    out = run([_pair(ticker="000001"), _pair(ticker="000002")])
    assert fake.revived == ["000002"] and fake.added == []
    assert out["revived"] == 1 and out["discovered"] == 0
