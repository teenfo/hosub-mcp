import pandas as pd

from app.signals import rules

ORB_CFG = {"range_start": "09:00", "range_end": "09:15", "target_r": 1.5}


def _bars(rows):
    """rows: (hhmm, o, h, l, c) 리스트 → 1분봉 DataFrame."""
    idx, data = [], []
    for hhmm, o, h, l, c in rows:
        idx.append(pd.Timestamp(f"2026-07-20 {hhmm}:00"))
        data.append({"open": o, "high": h, "low": l, "close": c, "volume": 1000})
    return pd.DataFrame(data, index=pd.DatetimeIndex(idx))


def _range_bars(n, start="09:00", o=101, h=102, l=100, c=101):
    start_ts = pd.Timestamp(f"2026-07-20 {start}:00")
    idx = pd.date_range(start_ts, periods=n, freq="1min")
    return pd.DataFrame(
        [{"open": o, "high": h, "low": l, "close": c, "volume": 1000}] * n, index=idx
    )


def test_orb_short_on_range_break_down():
    df = pd.concat([
        _range_bars(15),                                # 09:00~09:14 범위 100~102
        _bars([("09:15", 100, 100.5, 98.9, 99.0)]),     # 하단 이탈
    ])
    sig = rules.orb(df, ORB_CFG)
    assert sig is not None and sig.side == "short"
    assert sig.entry == 99.0
    assert sig.stop == 102.0                            # 범위 반대끝
    assert sig.target == 99.0 - 1.5 * 3.0               # 1.5R
    assert sig.risk == 3.0


def test_orb_long_on_range_break_up():
    df = pd.concat([
        _range_bars(15),
        _bars([("09:15", 102, 103.2, 101.9, 103.0)]),
    ])
    sig = rules.orb(df, ORB_CFG)
    assert sig is not None and sig.side == "long"
    assert sig.stop == 100.0


def test_orb_none_inside_range():
    df = pd.concat([_range_bars(15), _bars([("09:15", 101, 101.5, 100.5, 101.0)])])
    assert rules.orb(df, ORB_CFG) is None


def test_gap_requires_min_gap():
    cfg = {"min_gap_pct": 2.0, "range_wait_until": "10:00",
           "trail_long_pct": 8.0, "trail_short_pct": 4.0}
    df = pd.concat([_range_bars(61), _bars([("10:01", 99, 99.5, 98.5, 98.7)])])
    # 전일 종가 101 → 시가 101, 갭 0% → 신호 없음
    assert rules.gap(df, cfg, prev_close=101.0) is None
    # 전일 종가 105 → 시가 101, 갭 -3.8% + 첫시간 하단(100) 이탈 → 숏
    sig = rules.gap(df, cfg, prev_close=105.0)
    assert sig is not None and sig.side == "short"
    assert sig.meta["trail_pct"] == 4.0


def test_breakdown_retest_short():
    cfg = {"support_lookback": 30, "retest_tolerance_pct": 0.3}
    base = _range_bars(50)                              # 지지선 low=100
    recent = _bars([
        ("09:50", 100, 100.1, 98.9, 99.0),              # 붕괴 (99 < 99.7)
        ("09:51", 99, 99.3, 98.8, 99.1),
        ("09:52", 99.1, 99.6, 99.0, 99.5),
        ("09:53", 99.5, 99.9, 99.4, 99.8),
        ("09:54", 99.8, 100.0, 99.6, 99.9),
        ("09:55", 99.9, 100.1, 99.7, 100.0),
        ("09:56", 100.0, 100.2, 99.8, 100.05),
        ("09:57", 100.05, 100.15, 99.9, 100.0),
        ("09:58", 100.0, 100.1, 99.85, 99.95),
        ("09:59", 100.1, 100.2, 99.8, 99.9),            # 리테스트 실패 음봉
    ])
    df = pd.concat([base, recent])
    sig = rules.breakdown_retest(df, cfg)
    assert sig is not None and sig.side == "short"
    assert sig.stop > 100.0


def test_bounce_fade_none_on_uptrend():
    closes = [100 + i * 0.1 for i in range(60)]
    idx = pd.date_range("2026-07-20 09:00", periods=60, freq="1min")
    df = pd.DataFrame(
        {"open": closes, "high": [c + 0.2 for c in closes],
         "low": [c - 0.2 for c in closes], "close": closes, "volume": 1000},
        index=idx,
    )
    assert rules.bounce_fade(df, {"rsi_hot": 60, "lookback": 30}) is None


def test_every_signal_has_exit():
    """Exit 우선 원칙: 모든 신호는 stop/target 을 가진다."""
    df = pd.concat([_range_bars(15), _bars([("09:15", 100, 100.5, 98.9, 99.0)])])
    for sig in rules.evaluate_all(df, {"orb": {"enabled": True, **ORB_CFG}}):
        assert sig.stop and sig.target and sig.risk > 0
