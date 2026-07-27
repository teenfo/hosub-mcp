"""돌파 확인(confirm_on_close) — 진행 중인 현재 분봉의 신호는 발사하지 않는다.

키움 분봉 응답은 형성 중인 봉을 현재가를 종가로 해서 함께 준다. 그래서 감시
주기를 짧게 두면 '찍고 되밀린 가짜 돌파'에도 반응하는데, 신호는 (종목·규칙)당
하루 한 번만 발사되므로 가짜 돌파 한 번이 그날의 기회를 소진해 버린다.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from app.signals import rules

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 7, 27, 9, 20, 30, tzinfo=KST)      # 09:20 봉이 형성 중


def test_bar_closed_judgement():
    closed = datetime(2026, 7, 27, 9, 19, tzinfo=KST)
    forming = datetime(2026, 7, 27, 9, 20, tzinfo=KST)
    assert rules.bar_closed(closed, NOW) is True
    assert rules.bar_closed(forming, NOW) is False       # 현재 분 = 미완성
    assert rules.bar_closed(None, NOW) is True           # 시각 미상 → 통과


def test_bar_closed_naive_index():
    """tz-naive 인덱스(테스트·백테스트 픽스처)도 같은 판정."""
    assert rules.bar_closed(datetime(2026, 7, 27, 9, 19), NOW) is True
    assert rules.bar_closed(datetime(2026, 7, 27, 9, 20), NOW) is False


def _orb_df(last_minute: int):
    """09:00~09:15 범위 형성 후 상단 돌파 — 마지막 봉 시각을 인자로 정한다."""
    idx, rows = [], []
    for m in range(15):                                  # 범위 구간
        idx.append(datetime(2026, 7, 27, 9, m, tzinfo=KST))
        rows.append({"open": 100.0, "high": 101.0, "low": 99.0,
                     "close": 100.0, "volume": 100})
    idx.append(datetime(2026, 7, 27, 9, last_minute, tzinfo=KST))
    rows.append({"open": 101.0, "high": 103.0, "low": 101.0,
                 "close": 103.0, "volume": 500})         # 돌파봉
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx))


ORB = {"enabled": True, "range_start": "09:00", "range_end": "09:15",
       "target_r": 1.5}


def test_forming_bar_signal_suppressed_when_on():
    df = _orb_df(20)                                     # 09:20 = 형성 중
    assert rules.evaluate_all(df, {"orb": ORB}, now=NOW) != []          # 기본 OFF
    assert rules.evaluate_all(df, {"orb": ORB, "confirm_on_close": True},
                              now=NOW) == []             # 전역 ON → 억제


def test_closed_bar_signal_passes_when_on():
    df = _orb_df(19)                                     # 09:19 = 마감된 봉
    out = rules.evaluate_all(df, {"orb": ORB, "confirm_on_close": True}, now=NOW)
    assert [s.rule for s in out] == ["orb"]


def test_per_rule_override_beats_global():
    """개별 규칙 설정이 전역 기본값을 덮어쓴다(규칙별로 켜고 끌 수 있게)."""
    df = _orb_df(20)
    off = {"orb": {**ORB, "confirm_on_close": False}, "confirm_on_close": True}
    assert rules.evaluate_all(df, off, now=NOW) != []
    on = {"orb": {**ORB, "confirm_on_close": True}, "confirm_on_close": False}
    assert rules.evaluate_all(df, on, now=NOW) == []


def test_default_now_does_not_suppress_history():
    """now 미지정(=현재 시각)이면 과거 봉은 모두 마감봉 — 백테스트 무영향."""
    df = _orb_df(20)
    assert rules.evaluate_all(df, {"orb": ORB, "confirm_on_close": True}) != []


# --- 손절폭 대역 필터 (min_stop_pct ~ max_stop_pct) ---
# 너무 좁으면 왕복 비용이 이익을 먹고, 너무 넓으면 목표가 하루 변동폭 밖으로 나간다.

def _band_df(stop_pct: float):
    """ORB 상단 돌파 — 손절폭(범위 하단까지 거리)을 원하는 %로 만든다."""
    idx, rows = [], []
    lo = 100.0
    entry = 103.0
    hi_range = entry / (1 + stop_pct / 100)      # 진입가 대비 손절폭이 stop_pct 가 되게
    lo = entry * (1 - stop_pct / 100)
    for m in range(15):                          # 09:00~09:14 범위 구간
        idx.append(datetime(2026, 7, 27, 9, m, tzinfo=KST))
        rows.append({"open": lo, "high": hi_range, "low": lo,
                     "close": lo, "volume": 100})
    idx.append(datetime(2026, 7, 27, 9, 19, tzinfo=KST))
    rows.append({"open": hi_range, "high": entry, "low": hi_range,
                 "close": entry, "volume": 500})
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx))


def _eval(stop_pct, **cfg):
    rules_cfg = {"orb": {**ORB, "min_range_pct": 0, "max_range_pct": 0}, **cfg}
    return rules.evaluate_all(_band_df(stop_pct), rules_cfg, now=NOW)


def test_stop_band_filters_both_ends():
    assert _eval(1.6) != []                                  # 대역 안 — 통과
    assert _eval(1.6, min_stop_pct=1.0, max_stop_pct=2.5) != []
    assert _eval(0.3, min_stop_pct=1.0, max_stop_pct=2.5) == []   # 너무 좁다
    assert _eval(3.4, min_stop_pct=1.0, max_stop_pct=2.5) == []   # 너무 넓다


def test_stop_band_boundaries_inclusive():
    """경계값은 통과시킨다(1.0% 하한이면 정확히 1.0% 는 살린다)."""
    assert _eval(1.0, min_stop_pct=1.0, max_stop_pct=2.5) != []
    assert _eval(2.5, min_stop_pct=1.0, max_stop_pct=2.5) != []


def test_stop_band_disabled_when_zero():
    assert _eval(0.2, min_stop_pct=0, max_stop_pct=0) != []
    assert _eval(9.0, min_stop_pct=0, max_stop_pct=0) != []


def test_per_rule_stop_band_overrides_global():
    """규칙별 설정이 전역 대역을 덮어쓴다."""
    cfg = {"orb": {**ORB, "min_range_pct": 0, "max_range_pct": 0,
                   "min_stop_pct": 0.1, "max_stop_pct": 9.0},
           "min_stop_pct": 1.0, "max_stop_pct": 2.5}
    assert rules.evaluate_all(_band_df(0.3), cfg, now=NOW) != []   # 규칙별이 이김


def test_stop_bound_helper():
    assert rules._stop_bound({"min_stop_pct": 2.0}, {"min_stop_pct": 1.0},
                             "min_stop_pct") == 2.0     # 규칙별 우선
    assert rules._stop_bound({}, {"min_stop_pct": 1.0}, "min_stop_pct") == 1.0
    assert rules._stop_bound({}, {}, "min_stop_pct") == 0.0
    assert rules._stop_bound({"min_stop_pct": "bad"}, {}, "min_stop_pct") == 0.0
    assert rules._stop_bound({"min_stop_pct": -3}, {}, "min_stop_pct") == 0.0
