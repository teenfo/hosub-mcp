"""기법 ON/OFF 영속화 + 신규 후보 규칙(VWAP 되찾기·박스 돌파 리테스트) 테스트."""
import pandas as pd
import pytest

from app import settings
from app.signals import rules

VR = {"min_bars": 30, "below_lookback": 15, "min_below_bars": 5, "vol_ratio": 0,
      "stop_lookback": 5, "atr_stop_mult": 0.5, "atr_period": 14, "target_r": 1.5}
RB = {"range_lookback": 60, "retest_tolerance_pct": 0.3,
      "atr_stop_mult": 0.5, "atr_period": 14, "target_r": 1.5}


def _series(closes, vols=None, start="09:00"):
    idx = pd.date_range(pd.Timestamp(f"2026-07-20 {start}:00"),
                        periods=len(closes), freq="1min")
    rows, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        rows.append({"open": o, "high": max(o, c) + 0.1, "low": min(o, c) - 0.1,
                     "close": c, "volume": (vols[i] if vols else 1000)})
        prev = c
    return pd.DataFrame(rows, index=idx)


def test_save_rule_enabled_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RULES_FILE", tmp_path / "rules.json")
    monkeypatch.setattr(settings, "RULES",
                        {"orb": {"enabled": True}, "gap": {"enabled": True}})
    settings.save_rule_enabled("orb", False)
    assert settings.RULES["orb"]["enabled"] is False
    # 재로딩 시뮬레이션: config 기본값(True)에 override 가 다시 적용된다
    settings.RULES["orb"]["enabled"] = True
    settings._load_rules_overrides()
    assert settings.RULES["orb"]["enabled"] is False   # rules.json 이 이김
    with pytest.raises(ValueError):
        settings.save_rule_enabled("없는규칙", True)


def test_vwap_reclaim_long():
    # 30봉 하락(VWAP 아래 체류) 후 급반등으로 VWAP 상향 돌파 양봉
    closes = [100 - i * 0.15 for i in range(38)] + [96.0, 99.5]
    df = _series(closes)
    s = rules.vwap_reclaim(df, VR)
    assert s is not None and s.rule == "vwap_reclaim" and s.side == "long"
    assert s.stop < s.entry < s.target


def test_vwap_reclaim_none_when_always_above():
    df = _series([100 + i * 0.05 for i in range(40)])   # 계속 VWAP 위 → 되찾기 아님
    assert rules.vwap_reclaim(df, VR) is None


def test_range_break_retest_long():
    box = [100.0, 100.5] * 35                      # 박스 상단 ~100.6
    breakout = [101.5, 102.0]                      # 상단 돌파
    retest = [101.2, 100.9, 100.5, 100.4, 100.45, 100.7]  # 상단 리테스트 후 지지 양봉
    df = _series(box + breakout + retest)
    s = rules.range_break_retest(df, RB)
    assert s is not None and s.rule == "range_break_retest" and s.side == "long"
    assert s.stop < s.entry


def test_range_break_retest_none_without_breakout():
    df = _series([100.0, 100.5] * 40)              # 돌파 없음
    assert rules.range_break_retest(df, RB) is None


GF = {"min_gap_pct": 1.0, "confirm_bars": 5, "min_rr": 1.0, "min_bars": 10,
      "atr_stop_mult": 0.5, "atr_period": 14}


def test_gap_fill_long_after_reversal():
    # 전일 100 → 갭하락 97 시작, 초반 약세 후 초반 고가 돌파 양봉(반전)
    closes = [97.0, 96.5, 96.2, 96.0, 96.3, 96.1, 96.4, 96.8, 97.0, 97.2, 97.5]
    df = _series(closes)
    s = rules.gap_fill(df, GF, prev_close=100.0)
    assert s is not None and s.rule == "gap_fill" and s.side == "long"
    assert s.target == 100.0                        # 목표 = 전일 종가(갭 메우기)
    assert s.stop < s.entry < s.target


def test_gap_fill_none_without_gap_or_after_filled():
    closes = [99.8, 99.9, 100.0, 100.1, 100.0, 100.2, 100.1, 100.3, 100.2, 100.4, 100.6]
    df = _series(closes)
    assert rules.gap_fill(df, GF, prev_close=100.0) is None   # 갭하락 아님
    # 갭하락이었어도 이미 전일종가 회복 → 기회 소멸
    closes2 = [97.0, 97.5, 98.0, 99.0, 99.5, 100.0, 100.5, 100.8, 101.0, 101.2, 101.5]
    assert rules.gap_fill(_series(closes2), GF, prev_close=100.0) is None


def test_orb_volume_filter_blocks_weak_breakout():
    import pandas as pd
    idx = pd.date_range("2026-07-20 09:00", periods=16, freq="1min")
    rows = [{"open": 101, "high": 102, "low": 100, "close": 101, "volume": 1000}] * 15
    rows.append({"open": 101, "high": 103, "low": 101, "close": 102.8, "volume": 500})  # 저거래 돌파
    df = pd.DataFrame(rows, index=idx)
    cfg = {"range_start": "09:00", "range_end": "09:15", "target_r": 1.5}
    assert rules.orb(df, cfg) is not None                     # 필터 없으면 신호
    assert rules.orb(df, {**cfg, "vol_ratio": 1.2}) is None   # 거래량 필터에 차단
