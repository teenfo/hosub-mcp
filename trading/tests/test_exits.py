"""청산 자동화(B: 손절 자동, 목표 승인) 테스트."""
import pytest

from app import settings
from app.trade import ledger, orders


def _order(oid, symbol="005930", side="long", entry=10_000, stop=9_800,
           target=10_400, rule="orb", qty=10, exec_symbol=None):
    return {"id": oid, "symbol": symbol, "side": side, "entry": entry,
            "stop": stop, "target": target, "rule": rule, "qty": qty,
            "exec_symbol": exec_symbol}


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(orders, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(settings, "WATCHLIST", {"005930": "삼성전자"})
    monkeypatch.setattr(settings, "COSTS", {"commission_pct": 0.015,
                                            "sell_tax_pct": 0.15, "slippage_bp": 5})


def test_due_exits_detects_stop_and_target(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    ledger.open_position(_order("p1"), fill=10_000)   # stop 9,800 / target 10,400
    assert ledger.due_exits(lambda s: 9_700)[0]["reason"] == "stop"
    assert ledger.due_exits(lambda s: 10_500)[0]["reason"] == "target"
    assert ledger.due_exits(lambda s: 10_000) == []   # 범위 안 → 없음


def test_propose_exit_sets_pending_and_hides_from_due(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    ledger.open_position(_order("p2"), fill=10_000)
    ex = ledger.due_exits(lambda s: 10_500)[0]
    oid = orders.propose_exit(ex, "target", ex["exit_px"])
    # 승인 대기 청산 주문이 생기고, 중복 제안 방지로 due 에서 빠진다
    assert any(o["kind"] == "exit" and o["id"] == oid
               for o in orders.list_orders(status="pending"))
    assert ledger.due_exits(lambda s: 10_500) == []


def test_승인_문구가_청산_사유를_구분한다(tmp_path, monkeypatch):
    """승인 화면은 사람이 급할 때 보는 목록이다.

    문구가 사유와 무관하게 '목표 도달' 로 고정돼 있어서, **손절 승인이 이익
    실현처럼 읽혔다.** 데스크를 끄면 손절도 이 경로로 올라오므로 더 중요해졌다.
    """
    _fresh(tmp_path, monkeypatch)
    ledger.open_position(_order("p9"), fill=10_000)
    ex = ledger.due_exits(lambda s: 9_700)[0]
    assert ex["reason"] == "stop"
    orders.propose_exit(ex, "stop", ex["exit_px"])
    o = [x for x in orders.list_orders(status="pending") if x["kind"] == "exit"][0]
    assert "손절" in o["reason"] and "목표" not in o["reason"]


@pytest.mark.asyncio
async def test_execute_exit_closes_position(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    ledger.open_position(_order("p3"), fill=10_000)
    ex = ledger.due_exits(lambda s: 9_700)[0]

    async def fake_order(side, symbol, qty, price=0):
        assert side == "sell" and symbol == "005930" and qty == 10
        return {"ord_no": "X1", "return_code": 0}

    monkeypatch.setattr("app.kiwoom.client.client.order", fake_order)
    r = await orders.execute_exit(ex, "stop", ex["exit_px"])
    assert r["ok"]
    (p,) = ledger.positions(status="closed")
    assert p["exit_reason"] == "stop" and p["exit"] == 9_800


@pytest.mark.asyncio
async def test_approve_exit_order_closes_position(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    ledger.open_position(_order("p4"), fill=10_000)
    ex = ledger.due_exits(lambda s: 10_500)[0]
    oid = orders.propose_exit(ex, "target", ex["exit_px"])

    async def fake_order(side, symbol, qty, price=0):
        return {"ord_no": "X2"}

    monkeypatch.setattr("app.kiwoom.client.client.order", fake_order)
    res = await orders.approve_and_send(oid)
    assert res["ok"]
    (p,) = ledger.positions(status="closed")
    assert p["exit_reason"] == "target" and p["exit"] == 10_400


def test_reject_exit_reopens_for_proposal(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    ledger.open_position(_order("p5"), fill=10_000)
    ex = ledger.due_exits(lambda s: 10_500)[0]
    oid = orders.propose_exit(ex, "target", ex["exit_px"])
    orders.reject(oid)
    # 보류(거부)하면 포지션은 열린 채로, 다시 청산 후보로 잡힌다
    assert ledger.due_exits(lambda s: 10_500)[0]["id"] == "p5"
