"""승인 시 발주 수량 사용자 지정(qty override) 테스트."""
import pytest

from app import settings
from app.signals.rules import Signal
from app.trade import ledger, orders


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(orders, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(settings, "WATCHLIST", {"005930": "삼성전자"})
    monkeypatch.setattr(settings, "INVERSE_ETF", "")
    monkeypatch.setattr(settings, "RISK", {"signal_ttl_min": 10})
    monkeypatch.setattr(settings, "COSTS", {"commission_pct": 0.015,
                                            "sell_tax_pct": 0.15, "slippage_bp": 5})


def _sig():
    return Signal(rule="orb", side="long", entry=10_000, stop=9_800,
                  target=10_400, reason="테스트", symbol="005930")


@pytest.mark.asyncio
async def test_approve_uses_qty_override(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    oid = orders.propose(_sig(), qty=10)

    sent = {}

    async def fake_order(side, symbol, qty, price=0):
        sent["qty"] = qty
        return {"ord_no": "Z1", "return_code": 0}

    monkeypatch.setattr("app.kiwoom.client.client.order", fake_order)
    res = await orders.approve_and_send(oid, qty=3)
    assert res["ok"]
    assert sent["qty"] == 3                     # 조정한 수량으로 발주됐다
    (p,) = ledger.positions(status="open")
    assert p["qty"] == 3                          # 장부에도 조정 수량이 기록된다


@pytest.mark.asyncio
async def test_approve_rejects_zero_qty(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    oid = orders.propose(_sig(), qty=10)

    async def fake_order(side, symbol, qty, price=0):  # 호출되면 안 됨
        raise AssertionError("0주는 발주되면 안 된다")

    monkeypatch.setattr("app.kiwoom.client.client.order", fake_order)
    res = await orders.approve_and_send(oid, qty=0)
    assert not res["ok"]
    assert "1주" in res["error"]
    assert orders.get(oid)["status"] == "pending"  # 상태 그대로


@pytest.mark.asyncio
async def test_approve_without_override_keeps_original(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    oid = orders.propose(_sig(), qty=7)

    sent = {}

    async def fake_order(side, symbol, qty, price=0):
        sent["qty"] = qty
        return {"ord_no": "Z2", "return_code": 0}

    monkeypatch.setattr("app.kiwoom.client.client.order", fake_order)
    res = await orders.approve_and_send(oid)         # qty 미지정
    assert res["ok"] and sent["qty"] == 7            # 원래 수량 유지
