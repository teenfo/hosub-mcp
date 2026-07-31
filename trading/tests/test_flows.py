"""수급 관측 — 누가 샀고 누가 팔았나.

실호출로 확인(2026-07-31): 수급 계열 TR 15개가 열려 있고 `ka10086` 이 OHLCV 에
개인·기관·외국인 + 프로그램 + 외국인지분율 + 신용비율을 **한 콜에** 얹어 준다.
"""
import asyncio
from datetime import datetime

import pytest

from app import settings
from app.kiwoom import flows as parse
from app.scout import flows, store

KST = store.KST

# 실응답에서 그대로 가져온 행 (삼성전자 2026-07-31)
PRICE = {
    "date": "20260731", "open_pric": "+257000", "high_pric": "+267000",
    "low_pric": "+243000", "close_pric": "+262500", "flu_rt": "+26.81",
    "trde_qty": "58478873", "amt_mn": "14924242", "crd_rt": "0.39",
    "ind": "--11681307", "orgn": "+3618959", "for_qty": "+9897241",
    "prm": "+4967270", "for_wght": "+46.70",
}
DETAIL = {
    "dt": "20260731", "ind_invsr": "-2958633", "frgnr_invsr": "2113789",
    "orgn": "935049", "fnnc_invt": "116000", "insrnc": "-42816",
    "invtrt": "442896", "etc_fnnc": "2923", "bank": "12379",
    "penfnd_etc": "70860", "samo_fund": "332808", "natn": "0",
    "etc_corp": "-76352", "natfor": "-13852",
}
SHORT = {"dt": "20260731", "shrts_qty": "1700722", "trde_wght": "+2.91",
         "shrts_avg_pric": "42890"}
DETAIL_SESSION = {
    "dt": "20260730", "bf_mkrt_trde_wght": "0.00",
    "opmr_trde_wght": "+99.24", "af_mkrt_trde_wght": "+0.75",
}


# --- 파싱 ---------------------------------------------------------------------

def test_일별주가에_수급이_같이_온다():
    (r,) = parse.parse_daily_price({"return_code": 0, "daly_stkpc": [PRICE]})
    assert r["d"] == "2026-07-31"
    assert r["close"] == 262500 and r["high"] == 267000
    assert r["orgn"] == 3618959 and r["foreign"] == 9897241
    assert r["program"] == 4967270 and r["for_wght"] == 46.70
    assert r["crd_rt"] == 0.39


def test_부호가_두_번_붙는다_여기서도():
    """`ind: "--11681307"` — 개인 순매도. ka10063 에서 잡은 것과 같은 함정이고
    수급은 부호가 곧 정보라 절댓값으로 뭉갤 수 없다."""
    (r,) = parse.parse_daily_price({"return_code": 0, "daly_stkpc": [PRICE]})
    assert r["ind"] == -11681307


def test_가격은_절댓값으로_읽는다():
    """가격에 붙는 +/- 는 전일대비 방향이다 — 그대로 쓰면 저가가 음수가 된다."""
    (r,) = parse.parse_daily_price({"return_code": 0, "daly_stkpc": [
        {**PRICE, "low_pric": "-243000", "close_pric": "-262500"}]})
    assert r["low"] == 243000 and r["close"] == 262500


def test_기관_세부_13주체():
    (r,) = parse.parse_investor_daily({"return_code": 0, "stk_invsr_orgn": [DETAIL]})
    assert r["d"] == "2026-07-31"
    assert r["invtrt"] == 442896 and r["insrnc"] == -42816
    assert r["samo_fund"] == 332808 and r["natn"] == 0
    # 기관 합계는 담지 않는다 — ka10086 이 이미 준다. 두 컬럼에 두면 나중에
    # 어느 쪽이 진실인지 알 수 없다.
    assert "orgn" not in r


def test_공매도():
    (r,) = parse.parse_short({"return_code": 0, "shrts_trnsn": [SHORT]})
    assert r["short_qty"] == 1700722 and r["short_wght"] == 2.91


def test_장전_장중_장후_비중():
    """NXT 프리마켓 관측이 어느 종목에서 의미 있는지를 이 값이 가른다."""
    (r,) = parse.parse_trade_detail(
        {"return_code": 0, "daly_trde_dtl": [DETAIL_SESSION]})
    assert r["bf_mkrt_wght"] == 0.0 and r["opmr_wght"] == 99.24
    assert r["af_mkrt_wght"] == 0.75


@pytest.mark.parametrize("fn, key", [
    (parse.parse_daily_price, "daly_stkpc"),
    (parse.parse_investor_daily, "stk_invsr_orgn"),
    (parse.parse_short, "shrts_trnsn"),
    (parse.parse_trade_detail, "daly_trde_dtl"),
])
def test_실패_응답과_깨진_날짜는_버린다(fn, key):
    assert fn({"return_code": 1, key: [PRICE | DETAIL | SHORT]}) == []
    assert fn({"return_code": 0, key: [{"date": "x", "dt": "x"}]}) == []


# --- 적재 ---------------------------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "scout.db")


def _one(day="2026-07-31"):
    with store._conn() as conn:
        r = conn.execute("SELECT * FROM flow_obs WHERE d=?", (day,)).fetchone()
    return dict(r) if r else None


def test_네_소스가_한_행에_모인다(db):
    store.record_flows("005930", parse.parse_daily_price(
        {"return_code": 0, "daly_stkpc": [PRICE]}), "삼성전자")
    store.record_flows("005930", parse.parse_investor_daily(
        {"return_code": 0, "stk_invsr_orgn": [DETAIL]}))
    store.record_flows("005930", parse.parse_short(
        {"return_code": 0, "shrts_trnsn": [SHORT]}))
    r = _one()
    assert r["name"] == "삼성전자"
    assert r["orgn"] == 3618959          # ka10086
    assert r["invtrt"] == 442896         # ka10059
    assert r["short_qty"] == 1700722     # ka10014


def test_한_소스_실패가_다른_소스의_값을_지우지_않는다(db):
    """`INSERT OR REPLACE` 였다면 공매도 조회 한 번 실패에 그날 수급이 통째로
    사라진다. 각 upsert 는 자기 컬럼만 쓴다."""
    store.record_flows("005930", parse.parse_daily_price(
        {"return_code": 0, "daly_stkpc": [PRICE]}), "삼성전자")
    # 공매도만 다시 넣는다 — 수급 칸은 그대로여야 한다
    store.record_flows("005930", parse.parse_short(
        {"return_code": 0, "shrts_trnsn": [SHORT]}))
    r = _one()
    assert r["orgn"] == 3618959 and r["close"] == 262500
    assert r["short_qty"] == 1700722


def test_같은_소스를_다시_넣으면_갱신된다(db):
    store.record_flows("005930", parse.parse_daily_price(
        {"return_code": 0, "daly_stkpc": [PRICE]}))
    store.record_flows("005930", parse.parse_daily_price(
        {"return_code": 0, "daly_stkpc": [{**PRICE, "orgn": "+1"}]}))
    assert _one()["orgn"] == 1


def test_커버리지가_칸별로_센다(db):
    """전종목(ka10086)과 감시목록 한정(나머지)은 대상이 다르다. 하나의
    '적재 건수' 로 보면 실패한 날과 원래 대상이 적은 날을 구분할 수 없다."""
    store.record_flows("005930", parse.parse_daily_price(
        {"return_code": 0, "daly_stkpc": [PRICE]}))
    store.record_flows("000660", parse.parse_daily_price(
        {"return_code": 0, "daly_stkpc": [PRICE]}))
    store.record_flows("005930", parse.parse_short(
        {"return_code": 0, "shrts_trnsn": [SHORT]}))
    (cov,) = store.flow_coverage()
    assert cov["codes"] == 2 and cov["with_flow"] == 2
    assert cov["with_short"] == 1 and cov["with_detail"] == 0


def test_빈_목록은_아무것도_안_쓴다(db):
    assert store.record_flows("005930", []) == 0
    assert store.flow_coverage() == []


# --- 수집 ---------------------------------------------------------------------

def test_한_축이_죽어도_나머지는_넣는다(db, monkeypatch):
    class Fake:
        async def investor_daily(self, code, dt):
            raise RuntimeError("서버 오류")

        async def short_trend(self, code, s, e):
            return {"return_code": 0, "shrts_trnsn": [SHORT]}

        async def trade_detail(self, code, s):
            return {"return_code": 0, "daly_trde_dtl": [DETAIL_SESSION]}

    import app.kiwoom.client as kc
    monkeypatch.setattr(kc, "client", Fake(), raising=False)
    monkeypatch.setattr(flows, "_cfg", lambda: {})
    got = asyncio.run(flows.collect_symbol(
        "005930", "삼성전자", datetime(2026, 7, 31, tzinfo=KST)))
    assert got["detail"] == 0 and got["short"] == 1 and got["session"] == 1


def test_감시목록_상한을_지킨다(db, monkeypatch):
    seen = []

    async def fake_symbol(code, name, day):
        seen.append(code)
        return {"detail": 0, "short": 0, "session": 0}

    monkeypatch.setattr(flows, "collect_symbol", fake_symbol)
    monkeypatch.setattr(flows, "_cfg", lambda: {"max_symbols": 2})
    # `settings.WATCHLIST` 는 dict[str, str] — 값이 **종목명 문자열**이다.
    # 이걸 dict 로 흉내 냈다가 실서버에서 AttributeError 로 500 이 났다.
    monkeypatch.setattr(settings, "WATCHLIST",
                        {"A": "가나다", "B": "라마바", "C": "사아자"}, raising=False)
    out = asyncio.run(flows.collect_once(datetime(2026, 7, 31, tzinfo=KST)))
    assert out["symbols"] == 2 and len(seen) == 2


def test_일별주가는_조회일이_필수다():
    """빈 문자열로 보내면 rc=2 `1511 필수 입력 값에 값이 존재하지 않습니다` 가
    온다 — 다른 조회 TR 들이 빈 값을 '최근' 으로 받아 주는 것과 다르다.
    이걸 빈 채로 배포했다가 전종목 배치 3,925콜이 통째로 실패할 뻔했다."""
    import asyncio as _aio

    from app.kiwoom import client as kc

    sent = {}

    class Probe(kc.KiwoomClient):
        def __init__(self):
            pass

        async def _call(self, path, tr, body):
            sent.update(body)
            return {"return_code": 0, "daly_stkpc": []}

    _aio.run(Probe().daily_price("005930"))
    assert sent["qry_dt"] and len(sent["qry_dt"]) == 8 and sent["qry_dt"].isdigit()

    sent.clear()
    _aio.run(Probe().daily_price("005930", "20260731"))
    assert sent["qry_dt"] == "20260731"


def test_감시목록_값은_종목명_문자열이다(db, monkeypatch):
    """실서버 회귀 — dict 로 읽고 `.get("name")` 을 부르면 루프 전체가 죽는다.
    픽스처가 현실과 다르면 그건 검사가 아니다."""
    seen = []

    async def fake_symbol(code, name, day):
        seen.append((code, name))
        return {"detail": 0, "short": 0, "session": 0}

    monkeypatch.setattr(flows, "collect_symbol", fake_symbol)
    monkeypatch.setattr(flows, "_cfg", lambda: {})
    monkeypatch.setattr(settings, "WATCHLIST", {"005930": "삼성전자"}, raising=False)
    asyncio.run(flows.collect_once(datetime(2026, 7, 31, tzinfo=KST)))
    assert seen == [("005930", "삼성전자")]


@pytest.mark.parametrize("val, want", [
    ("삼성전자", "삼성전자"),
    ({"name": "삼성전자"}, "삼성전자"),   # 예전 모양도 받아 준다
    ("", None), (None, None), (123, None),
])
def test_이름_추출은_모양을_가리지_않는다(val, want):
    assert flows._name_of(val) == want


def test_거래상세는_기준일에서_과거로_준다():
    """`strt_dt` 는 이름과 달리 '시작일' 이 아니라 '기준일' 이다. 범위 시작으로
    알고 10일 전을 넘겼더니 그보다 최근 날짜가 하나도 안 왔다 —
    실측: strt_dt=20260721 → 07-21, 07-20 … 순으로만 응답."""
    import asyncio as _aio

    from app.kiwoom import client as kc

    sent = {}

    class Probe(kc.KiwoomClient):
        def __init__(self):
            pass

        async def _call(self, path, tr, body):
            sent.update(body)
            return {"return_code": 0, "daly_trde_dtl": []}

    _aio.run(Probe().trade_detail("005930"))
    assert sent["strt_dt"] and len(sent["strt_dt"]) == 8   # 기본값은 오늘


def test_수집은_기준일로_오늘을_넘긴다(db, monkeypatch):
    """회귀 — `lookback_days` 만큼 과거를 넘기면 최근 관측이 통째로 빈다."""
    sent = []

    class Fake:
        async def investor_daily(self, code, dt):
            return {"return_code": 0, "stk_invsr_orgn": []}

        async def short_trend(self, code, s, e):
            return {"return_code": 0, "shrts_trnsn": []}

        async def trade_detail(self, code, anchor=""):
            sent.append(anchor)
            return {"return_code": 0, "daly_trde_dtl": []}

    import app.kiwoom.client as kc
    monkeypatch.setattr(kc, "client", Fake(), raising=False)
    monkeypatch.setattr(flows, "_cfg", lambda: {"lookback_days": 10})
    asyncio.run(flows.collect_symbol("005930", None,
                                     datetime(2026, 7, 31, tzinfo=KST)))
    assert sent == ["20260731"]        # 20260721 이 아니다
