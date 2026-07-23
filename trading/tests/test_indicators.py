import numpy as np
import pandas as pd

from app.signals import indicators as ind


def _df(closes, volume=100):
    idx = pd.date_range("2026-07-20 09:00", periods=len(closes), freq="1min")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame(
        {"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": volume}, index=idx
    )


def test_sma():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    assert ind.sma(s, 3).iloc[-1] == 4.0


def test_rsi_uptrend_near_100():
    closes = list(range(100, 140))
    assert ind.rsi(pd.Series(closes, dtype=float)).iloc[-1] > 95


def test_rsi_downtrend_near_0():
    closes = list(range(140, 100, -1))
    assert ind.rsi(pd.Series(closes, dtype=float)).iloc[-1] < 5


def test_atr_positive():
    df = _df([100 + np.sin(i) * 3 for i in range(50)])
    assert (ind.atr(df).iloc[14:] > 0).all()


def test_vwap_constant_price():
    df = _df([100.0] * 30)
    vw = ind.vwap(df)
    assert np.allclose(vw, 100.0)


def test_macd_shapes():
    df = _df(list(range(100, 160)))
    line, sig, hist = ind.macd(df["close"])
    assert len(line) == len(sig) == len(hist) == 60
    assert line.iloc[-1] > 0  # 상승 추세에서 MACD 양수
