"""추가 롱 규칙(모멘텀 돌파·눌림목) 테스트."""
import pandas as pd

from app.signals import rules

MOM = {"lookback": 20, "stop_lookback": 5, "atr_stop_mult": 0.5,
       "atr_period": 14, "target_r": 1.5}
PB = {"near_pct": 0.4, "stop_lookback": 5, "atr_stop_mult": 0.5,
      "atr_period": 14, "target_r": 1.5}


def _series(closes, start="09:00"):
    """close 리스트 → 1분봉(open=직전 종가, high/low 는 ±0.1 패딩)."""
    idx = pd.date_range(pd.Timestamp(f"2026-07-20 {start}:00"),
                        periods=len(closes), freq="1min")
    rows, prev = [], closes[0]
    for c in closes:
        o = prev
        rows.append({"open": o, "high": max(o, c) + 0.1, "low": min(o, c) - 0.1,
                     "close": c, "volume": 1000})
        prev = c
    return pd.DataFrame(rows, index=idx)


def test_momentum_breakout_long():
    df = _series([100.5] * 25 + [102.0])          # 25봉 횡보 후 신고가 돌파 양봉
    s = rules.momentum_breakout(df, MOM)
    assert s is not None and s.rule == "momentum" and s.side == "long"
    assert s.entry == 102.0 and s.stop < 102.0 and s.target > 102.0


def test_momentum_no_signal_when_below_vwap():
    df = _series([102.0] * 25 + [100.0])          # 하락 마감(신고가 아님)
    assert rules.momentum_breakout(df, MOM) is None


def test_pullback_long():
    rising = [100 + i * 0.1 for i in range(35)]   # 상승추세
    pull = [103.2, 103.0, 102.9, 102.85, 102.9, 103.05]  # 20MA 눌림 후 반등 양봉
    df = _series(rising + pull)
    s = rules.pullback_long(df, PB)
    assert s is not None and s.rule == "pullback" and s.side == "long"
    assert s.stop < s.entry < s.target


def test_pullback_no_signal_in_downtrend():
    df = _series([105 - i * 0.1 for i in range(41)])   # 하락추세 → 눌림 아님
    assert rules.pullback_long(df, PB) is None


def test_evaluate_all_includes_new_long_rules():
    df = _series([100.5] * 25 + [102.0])
    cfg = {"momentum": {**MOM, "enabled": True},
           "pullback": {**PB, "enabled": True}, "max_stop_pct": 10}
    sigs = rules.evaluate_all(df, cfg)
    assert any(s.rule == "momentum" for s in sigs)


# --- 상승 다이버전스 (연구 문서 §2.2 반영, 2026-08-01) --------------------------

DIV = {"lookback": 30, "min_bars": 60, "rsi_period": 14,
       "min_price_gap_pct": 0.15, "min_rsi_gap": 3.0, "rsi_low_max": 45,
       "atr_stop_mult": 0.5, "atr_period": 14, "target_r": 1.5}


def _divergence_bars():
    """가격은 더 낮은 저점(LL), 두 번째 하락의 기울기는 완만(RSI HL) + 반등 양봉.

    1차 급락(-6)과 2차 완만한 하락(-0.35/봉)이 다이버전스의 전형이다 —
    가격 저점은 낮아졌는데 하락 '기세' 는 줄었다.
    """
    base = [100.0] * 45                                   # 워밍업(RSI 안정)
    drop1 = [100 - 6 * (i + 1) / 8 for i in range(8)]     # 94 까지 급락
    bounce = [94 + 2.0 * (i + 1) / 7 for i in range(7)]   # 96 반등
    drop2 = [96 - 0.35 * (i + 1) for i in range(8)]       # 93.2 — 더 낮은 저점
    confirm = [94.6]                                      # 반등 양봉(직전 고가 회복)
    return _series(base + drop1 + bounce + drop2 + confirm)


def test_divergence_long_fires():
    df = _divergence_bars()
    s = rules.divergence_long(df, DIV)
    assert s is not None and s.rule == "divergence" and s.side == "long"
    assert s.stop < s.entry < s.target
    assert "다이버전스" in s.reason


def test_divergence_needs_lower_low():
    """가격이 이중바닥(같은 저점)이면 다이버전스가 아니다."""
    base = [100.0] * 45
    drop1 = [100 - 6 * (i + 1) / 8 for i in range(8)]
    bounce = [94 + 2.0 * (i + 1) / 7 for i in range(7)]
    drop2 = [96 - 0.25 * (i + 1) for i in range(8)]       # 94.0 — 같은 저점
    df = _series(base + drop1 + bounce + drop2 + [94.6])
    assert rules.divergence_long(df, DIV) is None


def test_divergence_needs_confirmation_bar():
    """다이버전스가 성립해도 반등 확인 전에는 진입하지 않는다(문서 규약)."""
    base = [100.0] * 45
    drop1 = [100 - 6 * (i + 1) / 8 for i in range(8)]
    bounce = [94 + 2.0 * (i + 1) / 7 for i in range(7)]
    drop2 = [96 - 0.35 * (i + 1) for i in range(8)]
    df = _series(base + drop1 + bounce + drop2 + [93.0])  # 확인봉이 음봉
    assert rules.divergence_long(df, DIV) is None


def test_divergence_needs_min_bars():
    assert rules.divergence_long(_series([100.0] * 40), DIV) is None
