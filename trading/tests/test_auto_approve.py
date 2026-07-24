"""완전 자동 발주 모드 테스트 — 신호 즉시 발주 + 설정 영속화."""
import types

import pytest

from app import settings
from app.signals import engine as engine_mod
from app.signals.engine import SignalEngine
from app.signals.rules import Signal


def _prep(monkeypatch, eng):
    monkeypatch.setattr(settings, "WATCHLIST", {"005930": "삼성전자"})
    eng.equity_synced = True

    async def _noop():
        return None

    async def _nb(sym):
        return None

    monkeypatch.setattr(eng, "_sync_equity", _noop)
    monkeypatch.setattr(eng, "_effective_regime", lambda: "중립")
    monkeypatch.setattr(eng, "day_guard_status",
                        lambda: {"halted": False, "reason": "", "pct": 0.0})
    monkeypatch.setattr(eng, "_today_df",
                        lambda s: (types.SimpleNamespace(empty=False), None))
    monkeypatch.setattr(eng, "_rules_for", lambda s: {})
    monkeypatch.setattr(engine_mod.collector, "backfill_minutes", _nb)
    monkeypatch.setattr(engine_mod.rules, "evaluate_all",
                        lambda df, cfg, prev: [Signal(
                            rule="orb", side="long", entry=10_000, stop=9_800,
                            target=10_400, reason="x")])


@pytest.mark.asyncio
async def test_auto_approve_sends_immediately(monkeypatch):
    monkeypatch.setitem(settings.RISK, "auto_approve", True)
    eng = SignalEngine(equity=10_000_000)
    _prep(monkeypatch, eng)
    monkeypatch.setattr(engine_mod.orders, "propose", lambda s, q: "oid1")
    approved = []

    async def fake_approve(oid):
        approved.append(oid)
        return {"ok": True, "status": "sent", "message": "발주 접수 · 주문번호 X1"}

    monkeypatch.setattr(engine_mod.orders, "approve_and_send", fake_approve)
    found = await eng.run_once()
    assert approved == ["oid1"]                     # 승인 없이 즉시 발주
    assert found[0]["auto_status"] == "sent"
    assert "주문번호" in found[0]["auto_message"]


@pytest.mark.asyncio
async def test_manual_mode_does_not_auto_send(monkeypatch):
    monkeypatch.setitem(settings.RISK, "auto_approve", False)
    eng = SignalEngine(equity=10_000_000)
    _prep(monkeypatch, eng)
    monkeypatch.setattr(engine_mod.orders, "propose", lambda s, q: "oid2")
    approved = []

    async def fake_approve(oid):
        approved.append(oid)
        return {"ok": True, "status": "sent"}

    monkeypatch.setattr(engine_mod.orders, "approve_and_send", fake_approve)
    found = await eng.run_once()
    assert approved == []                            # 수동 모드 — 승인 대기 유지
    assert found[0]["actionable"] is True and "auto_status" not in found[0]


def test_save_risk_persists_auto_approve(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RISK_FILE", tmp_path / "risk.json")
    monkeypatch.setattr(settings, "RISK", {"risk_per_trade_pct": 0.8})
    settings.save_risk(auto_approve=True)
    assert settings.RISK["auto_approve"] is True
    settings.RISK["auto_approve"] = False            # 재로딩 시뮬레이션
    settings._load_risk_overrides()
    assert settings.RISK["auto_approve"] is True     # risk.json 이 복원
