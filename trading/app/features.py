"""종목별 피처 계산 — 스케줄러(분석기)가 소비할 수치 테이블의 단일 소스.

일봉 DataFrame(오름차순, 최소 60행) → 종목 1행짜리 피처 dict.
발굴 스크리닝(screen_daily)과 데이터셋 내보내기(export)가 같은 계산을 공유한다.
"""
import numpy as np
import pandas as pd

from .signals.indicators import atr, rsi


def _r(v: float, n: int = 2) -> float:
    try:
        f = float(v)
        return round(f, n) if np.isfinite(f) else 0.0
    except (TypeError, ValueError):
        return 0.0


def compute_features(df: pd.DataFrame, cfg: dict | None = None) -> dict | None:
    """일봉 → 피처 dict. 60행 미만이면 None. code/name 은 호출자가 채운다.

    'liquid' 는 가격·거래대금 게이트 통과 여부(발굴 후보 자격).
    'score'/'reasons' 는 발굴 3규칙 결과(유동성과 무관하게 계산).
    """
    if len(df) < 60:
        return None
    cfg = cfg or {}
    c, v = df["close"], df["volume"]
    last = df.iloc[-1]
    close = float(last.close)
    prev_close = float(c.iloc[-2])
    avg20 = float(v.iloc[-21:-1].mean())
    high20 = float(df["high"].iloc[-20:].max())
    high60 = float(df["high"].iloc[-60:].max())
    low60 = float(df["low"].iloc[-60:].min())
    ma5 = float(c.rolling(5).mean().iloc[-1])
    ma20 = float(c.rolling(20).mean().iloc[-1])
    ma60 = float(c.rolling(60).mean().iloc[-1])
    aligned = (c.rolling(5).mean() > c.rolling(20).mean()) & (
        c.rolling(20).mean() > c.rolling(60).mean()
    )
    aligned_new = bool(aligned.iloc[-1]) and not bool(aligned.iloc[-6:-1].all())
    vol_ratio = close_ret(v.iloc[-1], avg20)
    rsi14 = float(rsi(c, 14).iloc[-1])

    # --- 확장 피처 ---
    atr14 = float(atr(df, 14).iloc[-1])
    atr_pct = atr14 / close * 100 if close else 0.0          # 변동성 국면(가격 대비 ATR%)
    disparity20 = close / ma20 * 100 if ma20 else 0.0        # 20일 이격도
    disparity60 = close / ma60 * 100 if ma60 else 0.0        # 60일 이격도
    range20 = float(df["high"].iloc[-20:].max() - df["low"].iloc[-20:].min())
    range20_pct = range20 / close * 100 if close else 0.0    # 20일 변동폭%(작을수록 베이스 수렴)
    bearish_align = ma5 < ma20 < ma60                        # 역배열(하락 추세)
    near_low60_pct = close / low60 * 100 if low60 else 0.0   # 종가/60일최저가(하락·저점 근접)
    ret_120d = close_ret_pct(close, c, 120)
    # 연속봉: +n 양봉 / -n 음봉 (오늘 기준 연속)
    up_streak = 0
    for d in reversed(c.diff().tolist()):
        if d is None or not np.isfinite(d) or d == 0:
            break
        if d > 0 and up_streak >= 0:
            up_streak += 1
        elif d < 0 and up_streak <= 0:
            up_streak -= 1
        else:
            break
    # VCP형: 최근 5일 거래량이 20일평균의 70% 미만으로 말랐다가 오늘 2배↑ 터짐
    vol5 = float(v.iloc[-6:-1].mean())
    vcp = int(avg20 > 0 and vol5 < 0.7 * avg20 and v.iloc[-1] >= 2 * avg20)

    # 발굴 3규칙 (사유 텍스트는 대시보드용)
    reasons: list[str] = []
    if avg20 > 0 and v.iloc[-1] >= cfg.get("vol_surge_ratio", 3.0) * avg20:
        reasons.append(f"거래량 20일평균 {v.iloc[-1] / avg20:.1f}배")
    if high60 > 0 and close >= high60 * cfg.get("near_high_ratio", 0.97):
        reasons.append(f"60일 고가({high60:,.0f}) 대비 {close / high60 * 100:.0f}%")
    if aligned_new:
        reasons.append("이평 정배열 신규 형성")

    liquid = (
        close >= cfg.get("min_price", 1_000)
        and close * v.iloc[-1] >= cfg.get("min_trade_value_krw", 1_000_000_000)
    )
    return {
        "close": int(close),
        "change_pct": _r((close - prev_close) / prev_close * 100 if prev_close else 0),
        "volume": int(v.iloc[-1]),
        "vol_ratio20": _r(vol_ratio),
        "trade_value": int(close * v.iloc[-1]),
        "near_high20_pct": _r(close / high20 * 100 if high20 else 0, 1),
        "near_high60_pct": _r(close / high60 * 100 if high60 else 0, 1),
        "off_low60_pct": _r((close - low60) / low60 * 100 if low60 else 0, 1),
        "ma5": int(ma5), "ma20": int(ma20), "ma60": int(ma60),
        "ma_aligned": int(bool(aligned.iloc[-1])),
        "ma_aligned_new": int(aligned_new),
        "ret_5d": _r(close_ret_pct(close, c, 5), 1),
        "ret_20d": _r(close_ret_pct(close, c, 20), 1),
        "ret_60d": _r(close_ret_pct(close, c, 60), 1),
        "ret_120d": _r(ret_120d, 1),
        "rsi14": _r(rsi14, 1),
        "atr_pct": _r(atr_pct, 2),
        "disparity20": _r(disparity20, 1),
        "disparity60": _r(disparity60, 1),
        "range20_pct": _r(range20_pct, 1),
        "up_streak": int(up_streak),
        "above_ma20": int(close > ma20),
        "above_ma60": int(close > ma60),
        "bearish_align": int(bool(bearish_align)),
        "near_low60_pct": _r(near_low60_pct, 1),
        "vcp": int(vcp),
        "liquid": int(liquid),
        "score": float(len(reasons)),
        "reasons": reasons,
    }


def close_ret(cur, base) -> float:
    return float(cur) / float(base) if base else 0.0


def close_ret_pct(close: float, series: pd.Series, n: int) -> float:
    if len(series) <= n:
        return 0.0
    base = float(series.iloc[-1 - n])
    return (close - base) / base * 100 if base else 0.0
