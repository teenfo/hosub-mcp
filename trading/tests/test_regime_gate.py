"""시장 국면 게이트 — 강세장에서 인버스 ETF 매수 보류 테스트."""
import types

import pytest

from app import settings
from app.signals import engine as engine_mod
from app.signals.engine import SignalEngine
from app.signals.rules import Signal


def test_inverse_blocked_only_in_block_regime(monkeypatch):
    monkeypatch.setitem(settings.CONFIG, "regime_gate",
                        {"enabled": True, "inverse_block_regime": "강세"})
    monkeypatch.setitem(settings.CONFIG, "inverse_etfs", ["114800", "251340"])
    eng = SignalEngine()
    eng.regime = "강세"
    assert eng._inverse_blocked("114800") is True     # 인버스 + 강세 → 차단
    assert eng._inverse_blocked("010140") is False    # 일반주는 무관
    eng.regime = "약세"
    assert eng._inverse_blocked("114800") is False    # 약세장 → 허용
    eng.regime = "중립"
    assert eng._inverse_blocked("114800") is False    # 중립 → 허용


def test_effective_regime_blends_base_gap_night(tmp_path, monkeypatch):
    monkeypatch.setitem(settings.CONFIG, "regime_gate",
                        {"enabled": True, "use_open_gap": True, "open_gap_th": 0.5,
                         "use_night_bias": True})
    eng = SignalEngine()
    monkeypatch.setattr(eng, "_base_regime", lambda: "강세")   # 전일 breadth 강세
    # 시가 갭 하락(약세) → 강세 base 를 한 단계 낮춰 중립
    monkeypatch.setattr(eng, "_open_gap_bias", lambda: "약세")
    monkeypatch.setattr(eng, "_read_night_bias", lambda: "중립")
    assert eng._effective_regime() == "중립"
    # 야간리포트(미국장) 약세면 그것을 기준으로, 갭까지 약세면 약세 유지
    monkeypatch.setattr(eng, "_read_night_bias", lambda: "약세")
    assert eng._effective_regime() == "약세"


@pytest.mark.asyncio
async def test_run_once_gates_inverse_in_bull(monkeypatch):
    monkeypatch.setitem(settings.CONFIG, "regime_gate",
                        {"enabled": True, "inverse_block_regime": "강세"})
    monkeypatch.setitem(settings.CONFIG, "inverse_etfs", ["114800"])
    monkeypatch.setattr(settings, "WATCHLIST", {"114800": "KODEX 인버스"})
    eng = SignalEngine(equity=10_000_000)
    eng.equity_synced = True
    eng.regime = "강세"                                       # 강세장 고정

    async def _noop():
        return None

    monkeypatch.setattr(eng, "_sync_equity", _noop)
    monkeypatch.setattr(eng, "_effective_regime", lambda: "강세")
    monkeypatch.setattr(eng, "day_guard_status",
                        lambda: {"halted": False, "reason": "", "pct": 0.0})
    monkeypatch.setattr(eng, "_today_df",
                        lambda s: (types.SimpleNamespace(empty=False), None))
    monkeypatch.setattr(eng, "_rules_for", lambda s: {})
    monkeypatch.setattr(engine_mod.collector, "backfill_minutes", lambda s: _noop())
    monkeypatch.setattr(engine_mod.rules, "evaluate_all",
                        lambda df, cfg, prev: [Signal(
                            rule="momentum", side="long", entry=1125, stop=1110,
                            target=1160, reason="돌파")])
    calls = []
    monkeypatch.setattr(engine_mod.orders, "propose",
                        lambda s, q: (calls.append(q), "oid")[1])

    found = await eng.run_once()
    assert found and found[0]["actionable"] is False
    assert "국면 게이트" in found[0]["note"]
    assert calls == []                                # 강세장 인버스 매수 미발주
