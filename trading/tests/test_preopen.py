"""KRX 동시호가 예상체결 관측 — 창 판정·유니버스·NULL 규약·실패 격리."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import settings
from app.scout import preopen, store

KST = ZoneInfo("Asia/Seoul")


@pytest.fixture(autouse=True)
def _tmp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "scout.db")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(settings, "KIWOOM_APP_KEY", "k")
    monkeypatch.setattr(settings, "WATCHLIST", {"005930": "삼성전자"})


def _at(day: int, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 8, day, h, m, tzinfo=KST)


# --- 창 판정 ---

@pytest.mark.parametrize("day,hhmm,ok", [
    (3, "08:29", False), (3, "08:30", True), (3, "08:45", True),
    (3, "09:00", True), (3, "09:01", False), (3, "13:00", False),
    (1, "08:45", False),          # 토요일
])
def test_동시호가_창에서만_돈다(day, hhmm, ok):
    assert preopen.in_window(_at(day, hhmm)) is ok


# --- 유니버스 ---

def test_유니버스는_감시목록에_당일_프리마켓을_합친다():
    store.record_premarket(
        [{"code": "484810", "venue": "NXT", "name": "티엑스알로보틱스", "rank": 1}],
        _at(3, "08:10"))
    codes, names = preopen.universe(_at(3, "08:40"))
    assert set(codes) == {"005930", "484810"}
    assert names["005930"] == "삼성전자"
    assert names["484810"] == "티엑스알로보틱스"


def test_다른_날_프리마켓은_섞지_않는다():
    store.record_premarket(
        [{"code": "484810", "venue": "NXT", "name": "가", "rank": 1}],
        _at(1, "08:10"))                      # 8/1 관측
    codes, _ = preopen.universe(_at(3, "08:40"))
    assert codes == ["005930"]


def test_유니버스_상한(monkeypatch):
    monkeypatch.setattr(settings, "WATCHLIST",
                        {f"{i:06d}": f"종목{i}" for i in range(10)})
    monkeypatch.setitem(settings.CONFIG, "scout", dict(
        settings.CONFIG.get("scout", {}),
        observe=dict(settings.CONFIG.get("scout", {}).get("observe", {}),
                     preopen={"max_symbols": 3})))
    codes, _ = preopen.universe(_at(3, "08:40"))
    assert len(codes) == 3


# --- 수집·적재 ---

def _payload(code, exp_price=None):
    row = {"stk_cd": code, "cur_prc": "70000", "flu_rt": "0.00",
           "cntr_str": "0", "trde_qty": "0", "trde_prica": "0"}
    if exp_price is not None:
        row["exp_cntr_pric"] = str(exp_price)
        row["exp_cntr_qty"] = "1234"
    return {"return_code": 0, "atn_stk_infr": [row]}


class _Client:
    def __init__(self, payload=None, fail=False):
        self.payload, self.fail, self.calls = payload, fail, []

    async def watch_info(self, codes):
        self.calls.append(list(codes))
        if self.fail:
            raise RuntimeError("boom")
        return self.payload


@pytest.mark.asyncio
async def test_예상체결이_없으면_NULL_로_남는다(monkeypatch):
    """'공개 전' 과 '예상가 0' 을 섞지 않는다 — 첫 실측이 공개 시각을 이
    NULL 구간으로 확정한다."""
    import app.kiwoom.client as mod
    monkeypatch.setattr(mod, "client", _Client(_payload("005930")))
    assert await preopen.collect_once(_at(3, "08:32")) == 1
    with store._conn() as conn:
        r = conn.execute("SELECT exp_price, exp_qty, price FROM preopen_obs").fetchone()
    assert r["exp_price"] is None and r["exp_qty"] is None
    assert r["price"] == 70000


@pytest.mark.asyncio
async def test_예상체결이_오면_그대로_싣는다(monkeypatch):
    import app.kiwoom.client as mod
    monkeypatch.setattr(mod, "client", _Client(_payload("005930", 70500)))
    await preopen.collect_once(_at(3, "08:50"))
    with store._conn() as conn:
        r = conn.execute("SELECT name, exp_price, exp_qty FROM preopen_obs").fetchone()
    assert r["exp_price"] == 70500 and r["exp_qty"] == 1234
    assert r["name"] == "삼성전자"


@pytest.mark.asyncio
async def test_창_밖에서는_호출_자체를_안_한다(monkeypatch):
    import app.kiwoom.client as mod
    c = _Client(_payload("005930"))
    monkeypatch.setattr(mod, "client", c)
    assert await preopen.collect_once(_at(3, "09:05")) == 0
    assert c.calls == []


@pytest.mark.asyncio
async def test_조회_실패는_0건이지_예외가_아니다(monkeypatch):
    import app.kiwoom.client as mod
    monkeypatch.setattr(mod, "client", _Client(fail=True))
    assert await preopen.collect_once(_at(3, "08:40")) == 0


def test_같은_시점_재적재는_덮어쓴다():
    """재시도가 행을 불리지 않는다 — (ts, code) 멱등."""
    q = {"005930": {"price": 70000.0}}
    assert store.record_preopen(q, {}, _at(3, "08:40")) == 1
    assert store.record_preopen(q, {}, _at(3, "08:40")) == 1
    with store._conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM preopen_obs").fetchone()[0]
    assert n == 1


def test_요약에_예상체결_커버리지가_보인다():
    store.record_preopen({"005930": {"price": 1.0},
                          "000660": {"price": 1.0, "exp_price": 2.0}},
                         {}, _at(3, "08:40"))
    (d,) = store.preopen_days()
    assert d["codes"] == 2 and d["with_exp"] == 1
