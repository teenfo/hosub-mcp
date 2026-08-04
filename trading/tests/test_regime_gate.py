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


def test_rules_for_regime_linked_activation(monkeypatch):
    # momentum(regimes=[강세,중립])은 약세 유효국면에서 자동 비활성, 강세에선 활성.
    monkeypatch.setattr(settings, "RULES", {
        "momentum": {"enabled": True, "regimes": ["강세", "중립"]},
        "orb": {"enabled": True},                       # regimes 미지정 → 항상 가동
        "max_stop_pct": 4.0,
    })
    eng = SignalEngine()
    eng.regime = "약세"
    cfg = eng._rules_for("005930")
    assert cfg["momentum"]["enabled"] is False           # 약세장 → 자동 OFF
    assert cfg["orb"]["enabled"] is True                 # 무관 규칙은 그대로
    eng.regime = "강세"
    cfg = eng._rules_for("005930")
    assert cfg["momentum"]["enabled"] is True            # 강세장 → 자동 ON
    eng.regime = "중립"
    assert eng._rules_for("005930")["momentum"]["enabled"] is True
    # 원본 settings.RULES 는 오염되지 않는다
    assert settings.RULES["momentum"]["enabled"] is True


def test_effective_regime_blends_base_gap_night(tmp_path, monkeypatch):
    monkeypatch.setitem(settings.CONFIG, "regime_gate",
                        {"enabled": True, "use_open_gap": True, "open_gap_th": 0.5,
                         "use_night_bias": True})
    eng = SignalEngine()
    monkeypatch.setattr(eng, "_base_regime", lambda: "강세")   # 전일 breadth 강세
    # 시가 갭 하락(약세) → 강세 base 를 한 단계 낮춰 중립
    monkeypatch.setattr(eng, "_gap_and_drift", lambda: ("약세", "중립", None))
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
    monkeypatch.setattr(engine_mod.rules, "entry_window_note",
                        lambda rule, now, cfg: None)   # 벽시계 비의존
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


# --- 장중 방향 관측(2026-08-04) — 판정 미사용, regime_log 적재용 계산 검증 ---

def _bar_df(open_px, close_px):
    import pandas as pd
    idx = pd.DatetimeIndex([pd.Timestamp("2026-08-04 09:30:00")])
    return pd.DataFrame({"open": [open_px], "high": [max(open_px, close_px)],
                         "low": [min(open_px, close_px)], "close": [close_px],
                         "volume": [1]}, index=idx)


def test_장중_방향은_시가_대비_현재가_중앙값으로_계산된다(monkeypatch):
    """8/4 실측(갭업→하락 전환)의 관측 경로 — 시가 대비 −1% 다수면 약세."""
    monkeypatch.setitem(settings.CONFIG, "regime_gate",
                        {"use_open_gap": True, "open_gap_th": 0.5,
                         "observe_intraday": True})
    monkeypatch.setattr(settings, "WATCHLIST",
                        {f"00000{i}": f"S{i}" for i in range(5)})
    eng = SignalEngine()
    monkeypatch.setattr(eng, "_today_df", lambda s: (_bar_df(100.0, 99.0), None))
    gap, ib, med = eng._gap_and_drift()
    assert gap == "중립"                  # prev 종가 없음 — 갭 표본 0
    assert ib == "약세" and med == -1.0


def test_장중_관측_표본_부족이면_중립_None(monkeypatch):
    """못 잰 것을 0(중립 확신)으로 세지 않는다 — 값은 NULL 로 남는다."""
    monkeypatch.setitem(settings.CONFIG, "regime_gate",
                        {"use_open_gap": True, "open_gap_th": 0.5,
                         "observe_intraday": True})
    monkeypatch.setattr(settings, "WATCHLIST", {"000001": "S1", "000002": "S2"})
    eng = SignalEngine()
    monkeypatch.setattr(eng, "_today_df", lambda s: (_bar_df(100.0, 98.0), None))
    gap, ib, med = eng._gap_and_drift()
    assert (ib, med) == ("중립", None)


def test_장중_관측은_config_로_끈다(monkeypatch):
    monkeypatch.setitem(settings.CONFIG, "regime_gate",
                        {"use_open_gap": True, "open_gap_th": 0.5,
                         "observe_intraday": False})
    monkeypatch.setattr(settings, "WATCHLIST",
                        {f"00000{i}": f"S{i}" for i in range(5)})
    eng = SignalEngine()
    monkeypatch.setattr(eng, "_today_df", lambda s: (_bar_df(100.0, 99.0), None))
    gap, ib, med = eng._gap_and_drift()
    assert (ib, med) == ("중립", None)
