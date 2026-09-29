"""잔량 고아 — 2026-09-28 mock 실사고의 회귀 방지.

시장가 청산이 일부만 체결됐는데(KCC건설 680주 중 18주) 원장은 '접수' 만 보고
닫혔다. 잔량 5종목이 자산의 98%를 묶어 9/22 이후 모든 신규 매수가
`RC4025 매수증거금 부족` 으로 거부됐다. 두 겹으로 막는다:

  ① 자동(시장가) 청산도 잔량을 재발주한다(orders.execute_exit → 폴백)
  ② 그래도 남은 '원장 없는 보유' 는 rule='residual' 포지션으로 드러낸다
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import settings
from app.trade import fills, ledger, orders

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 9, 28, 10, 0, tzinfo=KST)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(orders, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(settings, "COSTS", {"commission_pct": 0.015,
                                            "sell_tax_pct": 0.20,
                                            "slippage_bp": 5})
    monkeypatch.setitem(settings.CONFIG, "execution",
                        dict(settings.CONFIG.get("execution", {}),
                             residual_adopt=True))
    return tmp_path


def _open(oid="p1", symbol="021320", qty=680, entry=5_621.0):
    ledger.open_position({"id": oid, "symbol": symbol, "side": "long",
                          "entry": entry, "stop": entry * 0.98,
                          "target": entry * 1.03, "rule": "orb", "qty": qty,
                          "name": symbol}, fill=entry, ord_no="B1")


def _residuals():
    return [p for p in ledger.positions(status="open", limit=200)
            if p["rule"] == fills.RESIDUAL_RULE]


INFO = {"021320": {"avg_price": 5_629, "name": "KCC건설"}}


# --------------------------------------------------------------------------
# ② 원장 없는 보유 편입
# --------------------------------------------------------------------------
def test_원장_없는_보유는_residual_포지션으로_편입된다(env):
    """9/8 KCC건설: 원장은 청산으로 닫혔는데 계좌엔 662주가 남은 상황."""
    got = fills.adopt_residuals({"021320": 662}, NOW, INFO, set())
    assert got == {"adopted": 1, "cleared": 0}
    [r] = _residuals()
    assert (r["symbol"], r["qty"], r["entry"]) == ("021320", 662, 5_629)
    assert r["stop"] is None and r["target"] is None, \
        "손절·목표가 없어야 데스크 자동 청산 대상이 아니다"
    assert r["name"] == "KCC건설"


def test_원장이_보유를_다_설명하면_편입하지_않는다(env):
    _open(qty=680)
    got = fills.adopt_residuals({"021320": 680}, NOW, INFO, set())
    assert got["adopted"] == 0 and _residuals() == []


def test_원장보다_많이_들고_있으면_차이만_편입한다(env):
    _open(qty=680)
    fills.adopt_residuals({"021320": 700}, NOW, INFO, set())
    [r] = _residuals()
    assert r["qty"] == 20


def test_방금_청산한_종목은_유예한다(env):
    """원장은 접수 시점에 닫히고 잔고는 체결 뒤에 준다 — 그 사이를 고아로 읽지 않는다."""
    got = fills.adopt_residuals({"021320": 662}, NOW, INFO, {"021320"})
    assert got["adopted"] == 0 and _residuals() == []


def test_잔량이_사라지면_void_로_정리한다(env):
    fills.adopt_residuals({"021320": 662}, NOW, INFO, set())
    got = fills.adopt_residuals({}, NOW + timedelta(minutes=1), INFO, set())
    assert got == {"adopted": 0, "cleared": 1}
    assert _residuals() == []


def test_잔량_수량이_바뀌면_갱신한다(env):
    fills.adopt_residuals({"021320": 662}, NOW, INFO, set())
    fills.adopt_residuals({"021320": 300}, NOW + timedelta(minutes=1), INFO, set())
    [r] = _residuals()
    assert r["qty"] == 300


def test_두_번_돌려도_중복_편입하지_않는다(env):
    fills.adopt_residuals({"021320": 662}, NOW, INFO, set())
    got = fills.adopt_residuals({"021320": 662}, NOW + timedelta(minutes=1), INFO, set())
    assert got["adopted"] == 0 and len(_residuals()) == 1


def test_설정으로_끌_수_있다(env, monkeypatch):
    monkeypatch.setitem(settings.CONFIG, "execution",
                        dict(settings.CONFIG.get("execution", {}),
                             residual_adopt=False))
    got = fills.adopt_residuals({"021320": 662}, NOW, INFO, set())
    assert got["adopted"] == 0 and _residuals() == []


def test_조회_실패면_sync_는_잔량을_건드리지_않는다(env):
    """holdings=None 은 '모름' 이지 '없음' 이 아니다 — 편입도 정리도 안 한다."""
    fills.adopt_residuals({"021320": 662}, NOW, INFO, set())
    day = NOW.date().isoformat()
    fills.store_today([], day)
    got = fills.sync_from_fills(day, None, now=NOW)
    assert got["residual_cleared"] == 0 and len(_residuals()) == 1


def test_residual_은_데스크_자동청산_판정에서_빠진다(env):
    fills.adopt_residuals({"021320": 662}, NOW, INFO, set())
    hits = ledger.due_exits(lambda sym: 1.0, NOW)   # 어떤 가격이어도
    assert hits == []


# --------------------------------------------------------------------------
# ① 자동(시장가) 청산의 잔량 재발주
# --------------------------------------------------------------------------
class _Spy:
    def __init__(self, held=None):
        self.calls, self.cancels, self.held = [], [], held

    async def order(self, side, symbol, qty, price=0, trde_tp=None):
        self.calls.append({"side": side, "qty": qty, "trde_tp": trde_tp})
        return {"return_code": 0, "ord_no": f"O{len(self.calls)}"}

    async def cancel_order(self, orig_ord_no, symbol):
        self.cancels.append(orig_ord_no)
        return {"return_code": 0}

    async def balance(self):
        if self.held is None:
            raise RuntimeError("조회 실패")
        return {"return_code": 0, "acnt_evlt_remn_indv_tot": [
            {"stk_cd": f"A{c}", "rmnd_qty": str(q)} for c, q in self.held.items()]}


def _install(monkeypatch, spy):
    import app.kiwoom.client as mod
    monkeypatch.setattr(mod, "client", spy)


async def test_자동_손절도_잔량_폴백을_건다(env, monkeypatch):
    """종전에는 수동(최유리)만 폴백이 있었다 — 이번 사고의 직접 원인."""
    _open()
    spy = _Spy()
    _install(monkeypatch, spy)
    scheduled = []

    async def _fb(pos, sym, ord_no, qty, wait):
        scheduled.append((sym, ord_no, qty, wait))

    monkeypatch.setattr(orders, "_best_exit_fallback", _fb)
    monkeypatch.setattr(orders, "_in_closing_auction", lambda now=None: False)  # 벽시계 무관
    monkeypatch.setitem(settings.CONFIG, "execution",
                        dict(settings.CONFIG.get("execution", {}),
                             exit_residual_sec=20))
    await orders.execute_exit(ledger.positions("open")[0], "stop", 5_500)
    import asyncio
    await asyncio.sleep(0)
    assert scheduled == [("021320", "O1", 680, 20.0)]


async def test_잔량_폴백_끄면_걸지_않는다(env, monkeypatch):
    _open()
    _install(monkeypatch, _Spy())
    scheduled = []

    async def _fb(*a):
        scheduled.append(a)

    monkeypatch.setattr(orders, "_best_exit_fallback", _fb)
    monkeypatch.setitem(settings.CONFIG, "execution",
                        dict(settings.CONFIG.get("execution", {}),
                             exit_residual_sec=0))
    await orders.execute_exit(ledger.positions("open")[0], "stop", 5_500)
    assert scheduled == []


async def test_잔량_재발주는_계좌보유와_다른_포지션_몫으로_자른다(env, monkeypatch):
    """WS 체결 수신을 놓쳐 rem 이 부풀어도 다른 포지션 몫까지 팔지 않는다."""
    _open(oid="p1", qty=680)
    _open(oid="p2", qty=100)                     # 같은 종목의 다른 오픈 포지션
    spy = _Spy(held={"021320": 400})             # 계좌엔 400주
    _install(monkeypatch, spy)
    pos = next(p for p in ledger.positions("open") if p["id"] == "p1")
    ledger.close_position("p1", 5_500, "stop", ord_no="X1")
    # 체결 수신 0건 → rem 680 으로 보이지만 팔 수 있는 건 400 − 100 = 300
    await orders._best_exit_fallback(pos, "021320", "X1", 680, wait_sec=0)
    assert spy.cancels == ["X1"]
    assert spy.calls[-1]["qty"] == 300


async def test_계좌조회_실패면_종전대로_잔량을_던진다(env, monkeypatch):
    _open(oid="p1", qty=10)
    spy = _Spy(held=None)
    _install(monkeypatch, spy)
    pos = ledger.positions("open")[0]
    with ledger._conn() as conn:
        conn.execute("INSERT INTO exec_fills VALUES (?,?,?,?,?,?,?)",
                     ("t", "X1", "021320", 5_500.0, 3, "체결", 0))
    await orders._best_exit_fallback(pos, "021320", "X1", 10, wait_sec=0)
    assert spy.calls[-1]["qty"] == 7


def test_최근_청산_종목_조회(env):
    now = datetime.now().astimezone()
    with orders._conn() as conn:
        conn.execute(
            f"INSERT INTO orders ({orders._ENTRY_COLS}) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("o1", now.isoformat(), now.isoformat(), "021320", "long", "orb",
             "자동청산(stop)", 5_600, 5_500, 5_800, 10, "021320", "sell", 10,
             "sent", "{}", "exit", "p1", 5_500))
    assert orders.recent_exit_symbols(3) == {"021320"}


# --------------------------------------------------------------------------
# 마감 동시호가 (실측 2026-09-29)
# --------------------------------------------------------------------------
def test_동시호가_구간_판정():
    d = datetime(2026, 9, 29, tzinfo=KST)
    assert orders._in_closing_auction(d.replace(hour=15, minute=20)) is True
    assert orders._in_closing_auction(d.replace(hour=15, minute=29, second=59)) is True
    assert orders._in_closing_auction(d.replace(hour=15, minute=19)) is False
    assert orders._in_closing_auction(d.replace(hour=15, minute=30)) is False


async def test_동시호가_중_청산은_잔량_폴백을_걸지_않는다(env, monkeypatch):
    """15:20 마감 정리는 15:30 단일가에 체결된다 — 20초 뒤 취소·재발주는 순서만 잃는다."""
    _open()
    _install(monkeypatch, _Spy())
    scheduled = []

    async def _fb(*a):
        scheduled.append(a)

    monkeypatch.setattr(orders, "_best_exit_fallback", _fb)
    monkeypatch.setattr(orders, "_in_closing_auction", lambda now=None: True)
    res = await orders.execute_exit(ledger.positions("open")[0], "eod", 5_500)
    assert res["ok"] is True and scheduled == []


def test_동시호가_뒤에는_편입_유예가_15시19분부터다(env):
    """15:20 에 판 종목은 15:30 체결까지 잔고가 그대로다 — 3분 유예로는 모자라다."""
    from datetime import UTC
    sent = datetime(2026, 9, 29, 15, 20, 20, tzinfo=KST)
    with orders._conn() as conn:
        conn.execute(
            f"INSERT INTO orders ({orders._ENTRY_COLS}) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("o1", sent.astimezone(UTC).isoformat(), sent.isoformat(), "021320",
             "long", "residual", "자동청산(eod)", 5_600, 0, 0, 10, "021320", "sell",
             10, "sent", "{}", "exit", "p1", 5_500))
    at_1528 = datetime(2026, 9, 29, 15, 28, tzinfo=KST).astimezone(UTC)
    at_1100 = datetime(2026, 9, 30, 11, 0, tzinfo=KST).astimezone(UTC)   # 다음 날 장중
    assert orders.recent_exit_symbols(3, now=at_1528) == {"021320"}
    assert orders.recent_exit_symbols(3, now=at_1100) == set()
