"""증거금 부족 시 매수가능 수량으로 자동 재발주 테스트."""
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
async def test_margin_shortfall_auto_resubmits(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    oid = orders.propose(_sig(), qty=47)
    calls = []

    async def fake_order(side, symbol, qty, price=0):
        calls.append(qty)
        if qty > 2:  # 매수가능 2주까지
            return {"return_code": 20,
                    "return_msg": "[2000](매수증거금이 부족합니다. 2주 매수가능)"}
        return {"return_code": 0, "ord_no": "OK1"}

    monkeypatch.setattr("app.kiwoom.client.client.order", fake_order)
    res = await orders.approve_and_send(oid)
    # 증거금 부족 → 매수가능 2주로 즉시 자동 재발주하여 체결 접수
    assert res["ok"] and res["status"] == "sent"
    assert res["auto_adjusted"] == 2 and "자동 조정" in res["message"]
    assert calls == [47, 2]                          # 47주 실패 → 2주 재발주
    row = orders.get(oid)
    assert row["status"] == "sent" and row["exec_qty"] == 2
    (p,) = ledger.positions(status="open")
    assert p["qty"] == 2                              # 장부에도 조정 수량 기록


@pytest.mark.asyncio
async def test_margin_zero_buyable_stays_pending(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    oid = orders.propose(_sig(), qty=5)

    async def fake_order(side, symbol, qty, price=0):
        # 매수가능 수량 안내가 없는(=0) 증거금 부족 — 1주도 못 산다
        return {"return_code": 20, "return_msg": "매수증거금이 부족합니다"}

    monkeypatch.setattr("app.kiwoom.client.client.order", fake_order)
    res = await orders.approve_and_send(oid)
    assert res["ok"] is False and res["retryable"] is True
    assert res["status"] == "pending"
    assert orders.get(oid)["status"] == "pending"    # 대기열 유지
    assert ledger.positions(status="open") == []      # 유령 포지션 없음


@pytest.mark.asyncio
async def test_non_margin_rejection_still_rejected(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    oid = orders.propose(_sig(), qty=5)

    async def fake_order(side, symbol, qty, price=0):
        return {"return_code": 40, "return_msg": "장 마감 시간입니다"}

    monkeypatch.setattr("app.kiwoom.client.client.order", fake_order)
    res = await orders.approve_and_send(oid)
    # 증거금 외 사유는 기존대로 거부(재시도 대상 아님)
    assert res["ok"] is False and not res.get("retryable")
    assert res["status"] == "rejected"
    assert orders.get(oid)["status"] == "rejected"
