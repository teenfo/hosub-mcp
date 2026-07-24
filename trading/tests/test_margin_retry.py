"""증거금 부족 거부 시 주문을 대기열에 유지(재시도 가능) 테스트."""
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
async def test_margin_shortfall_keeps_order_pending(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    oid = orders.propose(_sig(), qty=47)

    async def fake_order(side, symbol, qty, price=0):
        return {"return_code": 20,
                "return_msg": "[2000](855056:매수증거금이 부족합니다. 2주 매수가능)"}

    monkeypatch.setattr("app.kiwoom.client.client.order", fake_order)
    res = await orders.approve_and_send(oid)
    # 거부됐지만 대기열에 유지 + 재시도 안내(최대 2주)
    assert res["ok"] is False and res["retryable"] is True
    assert res["status"] == "pending"
    assert "2주" in res["message"]
    assert orders.get(oid)["status"] == "pending"          # 대기중 유지
    assert ledger.positions(status="open") == []            # 유령 포지션 없음
    # 만료시간이 미래로 갱신돼 바로 사라지지 않는다
    assert oid in {o["id"] for o in orders.list_orders(status="pending")}


@pytest.mark.asyncio
async def test_margin_retry_with_reduced_qty_succeeds(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    oid = orders.propose(_sig(), qty=47)
    calls = []

    async def fake_order(side, symbol, qty, price=0):
        calls.append(qty)
        if qty > 2:
            return {"return_code": 20,
                    "return_msg": "매수증거금이 부족합니다. 2주 매수가능"}
        return {"return_code": 0, "ord_no": "OK1"}

    monkeypatch.setattr("app.kiwoom.client.client.order", fake_order)
    r1 = await orders.approve_and_send(oid)                 # 47주 → 증거금 부족
    assert r1["retryable"] and orders.get(oid)["status"] == "pending"
    r2 = await orders.approve_and_send(oid, qty=2)          # 2주로 재시도 → 성공
    assert r2["ok"] and r2["status"] == "sent"
    assert calls == [47, 2]
    assert orders.get(oid)["status"] == "sent"


@pytest.mark.asyncio
async def test_non_margin_rejection_still_rejected(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    oid = orders.propose(_sig(), qty=5)

    async def fake_order(side, symbol, qty, price=0):
        return {"return_code": 40, "return_msg": "장 마감 시간입니다"}

    monkeypatch.setattr("app.kiwoom.client.client.order", fake_order)
    res = await orders.approve_and_send(oid)
    # 증거금 외 사유는 기존대로 거부 처리(재시도 대상 아님)
    assert res["ok"] is False and not res.get("retryable")
    assert res["status"] == "rejected"
    assert orders.get(oid)["status"] == "rejected"
