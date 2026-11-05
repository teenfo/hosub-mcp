"""청산 직전 진입 미체결 잔량 취소 — 사용자 결정 2026-11-05(추천안대로)의 회귀 방지.

실측(mock): 11/4 아이크래프트 — 484주 진입 주문이 누계 173주일 때 목표가 청산이 173주만
팔았고 311주가 청산 뒤 체결돼 고아가 됐다. 11/5 고려산업 — 1,688주 중 40주가 같은 경로.
"""
import json
from datetime import UTC, datetime

import pytest

from app import settings
from app.trade import ledger, orders

SYM = "052460"
ENTRY_ORD = "0072432"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(orders, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(settings, "CONFIG", {"execution": {
        "exit_residual_sec": 0,                 # 잔량 폴백 예약 없이 — 이 테스트는 취소만 본다
        "cancel_entry_on_exit": True}})
    return tmp_path


class _Spy:
    def __init__(self, on_cancel=None, cancel_raises=False):
        self.calls, self.cancels = [], []
        self.on_cancel, self.cancel_raises = on_cancel, cancel_raises

    async def order(self, side, symbol, qty, price=0, trde_tp=None):
        self.calls.append({"side": side, "symbol": symbol, "qty": qty})
        return {"return_code": 0, "ord_no": f"S{len(self.calls)}"}

    async def cancel_order(self, orig_ord_no, symbol):
        self.cancels.append(orig_ord_no)
        if self.cancel_raises:
            raise RuntimeError("취소 API 오류")
        if self.on_cancel:
            self.on_cancel()
        return {"return_code": 0, "return_msg": "취소 접수"}


def _install(monkeypatch, spy):
    import app.kiwoom.client as mod
    monkeypatch.setattr(mod, "client", spy)


def _entry(ordered=484, rule="vwap_reclaim", kind="entry", status="sent"):
    """진입 주문(orders.id == positions.id) + 오픈 포지션을 만든다."""
    now = datetime.now(UTC)
    order = {"id": "e1", "symbol": SYM, "side": "long", "rule": rule, "qty": ordered,
             "entry": 6_266.0, "stop": 6_200.0, "target": 6_288.0}
    with orders._conn() as conn:
        conn.execute(
            f"INSERT INTO orders ({orders._ENTRY_COLS}) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("e1", now.isoformat(), now.isoformat(), SYM, "long", rule, "",
             6_266.0, 6_200.0, 6_288.0, ordered, SYM, "buy", ordered, status,
             json.dumps({"ord_no": ENTRY_ORD, "return_code": 0}), kind, None, None))
    ledger.open_position(order, fill=6_266.0, ord_no=ENTRY_ORD)


def _ws_fill(cum, price=6_266.0):
    """실시간 체결 — 체결량은 누계로 온다(2026-11-05 실측)."""
    ledger.record_fill({"ord_no": ENTRY_ORD, "symbol": SYM, "price": price, "qty": cum,
                        "state": "체결"})


def _pos():
    return next(p for p in ledger.positions(status="open") if p["id"] == "e1")


async def test_진입이_덜_체결됐으면_나머지를_취소하고_체결된_만큼만_판다(monkeypatch):
    _entry()
    _ws_fill(173)
    spy = _Spy()
    _install(monkeypatch, spy)
    r = await orders.execute_exit(_pos(), "target", 6_288.0)
    assert r["status"] == "sent"
    assert spy.cancels == [ENTRY_ORD]
    assert spy.calls[-1]["qty"] == 173


async def test_다_체결된_진입은_취소하지_않는다(monkeypatch):
    _entry()
    _ws_fill(484)
    spy = _Spy()
    _install(monkeypatch, spy)
    await orders.execute_exit(_pos(), "stop", 6_200.0)
    assert spy.cancels == [] and spy.calls[-1]["qty"] == 484


async def test_취소_직전에_들어온_진입_체결까지_판다(monkeypatch):
    _entry()
    _ws_fill(173)
    spy = _Spy(on_cancel=lambda: _ws_fill(180))       # 취소가 닿기 전 7주 더 체결
    _install(monkeypatch, spy)
    await orders.execute_exit(_pos(), "target", 6_288.0)
    assert spy.calls[-1]["qty"] == 180


async def test_취소가_실패해도_청산은_나간다(monkeypatch):
    _entry()
    _ws_fill(173)
    spy = _Spy(cancel_raises=True)
    _install(monkeypatch, spy)
    r = await orders.execute_exit(_pos(), "stop", 6_200.0)
    assert r["status"] == "sent" and spy.calls[-1]["qty"] == 173


async def test_끄면_취소하지_않는다(monkeypatch):
    settings.CONFIG["execution"]["cancel_entry_on_exit"] = False
    _entry()
    _ws_fill(173)
    spy = _Spy()
    _install(monkeypatch, spy)
    await orders.execute_exit(_pos(), "stop", 6_200.0)
    assert spy.cancels == []


async def test_잔량_편입_행은_주문이_없어_취소하지_않는다(monkeypatch):
    _entry(rule="residual")
    _ws_fill(173)
    spy = _Spy()
    _install(monkeypatch, spy)
    await orders.execute_exit(_pos(), "residual", 6_288.0)
    assert spy.cancels == []


def test_지난날_연_포지션은_진입이_살아_있을_수_없다():
    _entry()
    _ws_fill(173)
    with ledger._conn() as conn:
        conn.execute("UPDATE positions SET opened='2026-11-04T09:56:12+09:00' WHERE id='e1'")
    assert orders._entry_unfilled(_pos()) == 0


def test_REST_체결_누계도_본다():
    """실시간을 놓쳐도 REST 동기화(broker_fills)가 다 체결을 알면 취소하지 않는다."""
    from app.trade import fills
    _entry()
    day = datetime.now(ledger.KST).date().isoformat()
    fills.store_today([{"ord_no": ENTRY_ORD, "code": SYM, "name": "아이크래프트",
                        "side": "buy", "price": 6_266.0, "qty": 484, "time": "095612",
                        "commission": 450, "tax": 0}], day)
    assert orders._entry_unfilled(_pos()) == 0


async def test_승인_경로_청산도_진입_나머지를_취소하고_늘어난_체결까지_판다(monkeypatch):
    _entry()
    _ws_fill(173)
    now = datetime.now(UTC)
    with orders._conn() as conn:                       # 제안 시점(173주)의 청산 주문
        conn.execute(
            f"INSERT INTO orders ({orders._ENTRY_COLS}) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("x1", now.isoformat(), now.replace(year=now.year + 1).isoformat(), SYM,
             "long", "vwap_reclaim", "목표 도달", 6_266.0, 6_200.0, 6_288.0, 173, SYM,
             "sell", 173, "pending", None, "exit", "e1", 6_288.0))
    _ws_fill(200)                                      # 승인 전 27주 더 체결
    spy = _Spy()
    _install(monkeypatch, spy)
    r = await orders.approve_and_send("x1")
    assert r["ok"] and spy.cancels == [ENTRY_ORD]
    assert spy.calls[-1]["qty"] == 200
