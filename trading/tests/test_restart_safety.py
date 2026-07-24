"""재시작 안전화 — 오늘 발사한 신호를 주문 이력에서 복원해 중복 발주 방지."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import settings
from app.signals.engine import SignalEngine
from app.signals.rules import Signal
from app.trade import orders

KST = ZoneInfo("Asia/Seoul")


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(orders, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(settings, "INVERSE_ETF", "")
    monkeypatch.setattr(settings, "RISK", {"signal_ttl_min": 10})


def test_restore_fired_dedups_todays_signal(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    sig = Signal(rule="orb", side="long", entry=10_000, stop=9_800,
                 target=10_400, reason="x", symbol="005930")
    orders.propose(sig, qty=5)                    # 오늘 진입 주문 생성

    eng = SignalEngine()                          # 재시작 상황(빈 _fired)
    assert eng._fired == {}
    eng._restore_fired()
    today = datetime.now(KST).date().isoformat()
    assert (today, "005930", "orb") in eng._fired  # 이력에서 복원됨


@pytest.mark.asyncio
async def test_restored_signal_not_reproposed(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    from app.signals import engine as engine_mod

    monkeypatch.setattr(settings, "WATCHLIST", {"005930": "삼성전자"})
    orders.propose(Signal(rule="orb", side="long", entry=10_000, stop=9_800,
                          target=10_400, reason="x", symbol="005930"), qty=5)

    eng = SignalEngine(equity=10_000_000)
    eng.equity_synced = True

    async def _noop_sync():
        return None

    async def _noop_backfill(sym):
        return None

    monkeypatch.setattr(eng, "_sync_equity", _noop_sync)
    monkeypatch.setattr(eng, "day_guard_status",
                        lambda: {"halted": False, "reason": "", "pct": 0.0})
    import types
    monkeypatch.setattr(eng, "_today_df",
                        lambda s: (types.SimpleNamespace(empty=False), None))
    monkeypatch.setattr(eng, "_rules_for", lambda s: {})
    monkeypatch.setattr(engine_mod.collector, "backfill_minutes", _noop_backfill)
    monkeypatch.setattr(engine_mod.rules, "evaluate_all",
                        lambda df, cfg, prev: [Signal(
                            rule="orb", side="long", entry=10_000, stop=9_800,
                            target=10_400, reason="x")])
    calls = []
    monkeypatch.setattr(engine_mod.orders, "propose",
                        lambda s, q: (calls.append(q), "oid")[1])

    found = await eng.run_once()
    # 이미 오늘 발사된 orb/005930 신호 → 재시작 후에도 재제안되지 않는다
    assert calls == []
    assert found == []
