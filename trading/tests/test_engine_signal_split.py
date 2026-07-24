"""감시 신호(금액 제한 없음) vs 승인대기 주문(잔고 참고) 분리 동작 테스트."""
import types

import pytest

from app import settings
from app.signals import engine as engine_mod
from app.signals.engine import SignalEngine
from app.signals.rules import Signal


def _prep(monkeypatch, eng, sig):
    """run_once 의존성(잔고 동기화·가드·데이터·규칙)을 테스트용으로 고정."""
    monkeypatch.setattr(settings, "WATCHLIST", {"005930": "삼성전자"})
    eng.equity_synced = True

    async def _noop_sync():
        return None

    async def _noop_backfill(sym):
        return None

    monkeypatch.setattr(eng, "_sync_equity", _noop_sync)
    monkeypatch.setattr(eng, "day_guard_status",
                        lambda: {"halted": False, "reason": "", "pct": 0.0})
    monkeypatch.setattr(eng, "_today_df",
                        lambda sym: (types.SimpleNamespace(empty=False), None))
    monkeypatch.setattr(eng, "_rules_for", lambda sym: {})
    monkeypatch.setattr(engine_mod.collector, "backfill_minutes", _noop_backfill)
    monkeypatch.setattr(engine_mod.rules, "evaluate_all",
                        lambda df, cfg, prev: [sig])


@pytest.mark.asyncio
async def test_affordable_signal_creates_pending_order(monkeypatch):
    eng = SignalEngine(equity=10_000_000)
    sig = Signal(rule="orb", side="long", entry=10_000, stop=9_800,
                 target=10_400, reason="테스트")
    _prep(monkeypatch, eng, sig)
    calls = []
    monkeypatch.setattr(engine_mod.orders, "propose",
                        lambda s, q: (calls.append(q), "oid1")[1])

    found = await eng.run_once()
    assert len(found) == 1
    assert found[0]["actionable"] is True
    assert found[0]["order_id"] == "oid1"
    assert calls and calls[0] >= 1                  # 잔고 충분 → 발주 수량 제안
    assert eng.last_signals[0]["symbol"] == "005930"


@pytest.mark.asyncio
async def test_long_only_blocks_short_orders(monkeypatch):
    # 롱 전용 모드: 숏 신호는 기록만 하고 발주하지 않는다(잔고 충분해도).
    monkeypatch.setitem(settings.RISK, "long_only", True)
    eng = SignalEngine(equity=10_000_000)
    sig = Signal(rule="orb", side="short", entry=10_000, stop=10_200,
                 target=9_400, reason="하락 이탈")
    _prep(monkeypatch, eng, sig)
    calls = []
    monkeypatch.setattr(engine_mod.orders, "propose",
                        lambda s, q: (calls.append(q), "oid")[1])

    found = await eng.run_once()
    assert len(found) == 1
    assert found[0]["actionable"] is False
    assert "롱 전용" in found[0]["note"]
    assert calls == []                              # 숏은 발주되지 않는다


@pytest.mark.asyncio
async def test_long_only_allows_long_orders(monkeypatch):
    monkeypatch.setitem(settings.RISK, "long_only", True)
    eng = SignalEngine(equity=10_000_000)
    sig = Signal(rule="orb", side="long", entry=10_000, stop=9_800,
                 target=10_400, reason="롱 신호")
    _prep(monkeypatch, eng, sig)
    calls = []
    monkeypatch.setattr(engine_mod.orders, "propose",
                        lambda s, q: (calls.append(q), "oidL")[1])

    found = await eng.run_once()
    assert found[0]["actionable"] is True and calls    # 롱은 정상 발주


@pytest.mark.asyncio
async def test_unaffordable_signal_recorded_but_no_order(monkeypatch):
    # 자산 142,589원으로 44만원짜리 종목 → 1주도 못 산다
    eng = SignalEngine(equity=142_589)
    sig = Signal(rule="orb", side="long", entry=441_500, stop=428_500,
                 target=460_000, reason="고가주 신호")
    _prep(monkeypatch, eng, sig)
    calls = []
    monkeypatch.setattr(engine_mod.orders, "propose",
                        lambda s, q: (calls.append(q), "oid")[1])

    found = await eng.run_once()
    # 감시 신호는 금액 제한 없이 기록된다(최근 신호에 남음)
    assert len(found) == 1
    assert found[0]["qty"] == 0
    assert found[0]["actionable"] is False
    assert "order_id" not in found[0]
    assert "잔고 부족" in found[0]["note"]
    # 승인대기 주문은 만들어지지 않는다(잔고 참고)
    assert calls == []
    assert eng.last_signals[0]["actionable"] is False
