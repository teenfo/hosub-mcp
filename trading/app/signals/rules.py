"""하락장 매매 규칙 → Signal 생성.

모든 규칙은 '오늘의 1분봉 DataFrame(장 시작~현재)' 을 입력으로 받고,
진입가·손절가·목표가가 채워진 Signal 을 반환하거나 None 을 반환한다.
Exit 우선 원칙: stop/target 없는 신호는 존재할 수 없다.
"""
from dataclasses import dataclass, field
from datetime import datetime, time

import pandas as pd

from . import indicators as ind


@dataclass
class Signal:
    rule: str
    side: str          # 'long' | 'short'
    entry: float
    stop: float
    target: float
    reason: str
    ts: datetime | None = None
    symbol: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)


def _hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _target(entry: float, stop: float, side: str, r: float) -> float:
    dist = abs(entry - stop) * r
    return entry + dist if side == "long" else entry - dist


def orb(df: pd.DataFrame, cfg: dict) -> Signal | None:
    """시초가 범위 돌파. 범위(09:00~09:15) 형성 후 상/하단 이탈 시 진입.
    손절은 범위 반대쪽 끝(보수적), 목표는 target_r × 손절 거리."""
    start, end = _hhmm(cfg["range_start"]), _hhmm(cfg["range_end"])
    tt = df.index.time
    rng = df[(tt >= start) & (tt < end)]
    after = df[tt >= end]
    if len(rng) < 3 or after.empty:
        return None
    hi, lo = rng["high"].max(), rng["low"].min()
    last = after.iloc[-1]
    r = cfg.get("target_r", 1.5)
    if last.close > hi:
        return Signal("orb", "long", float(last.close), float(lo),
                      _target(float(last.close), float(lo), "long", r),
                      f"ORB 상단 {hi:,.0f} 돌파", ts=after.index[-1])
    if last.close < lo:
        return Signal("orb", "short", float(last.close), float(hi),
                      _target(float(last.close), float(hi), "short", r),
                      f"ORB 하단 {lo:,.0f} 이탈", ts=after.index[-1])
    return None


def gap(df: pd.DataFrame, cfg: dict, prev_close: float | None) -> Signal | None:
    """갭 매매. 시가 갭 ≥ min_gap_pct 인 날, 첫 1시간(range_wait_until까지) 범위를
    기다린 뒤 범위 이탈 방향으로 진입. 손절은 범위 반대쪽 끝."""
    if not prev_close or df.empty:
        return None
    gap_pct = (df.iloc[0].open - prev_close) / prev_close * 100
    if abs(gap_pct) < cfg.get("min_gap_pct", 2.0):
        return None
    wait_until = _hhmm(cfg.get("range_wait_until", "10:00"))
    tt = df.index.time
    rng = df[tt < wait_until]
    after = df[tt >= wait_until]
    if rng.empty or after.empty:
        return None
    hi, lo = rng["high"].max(), rng["low"].min()
    last = after.iloc[-1]
    if last.close > hi:
        return Signal("gap", "long", float(last.close), float(lo),
                      _target(float(last.close), float(lo), "long", 1.5),
                      f"갭 {gap_pct:+.1f}% 후 첫시간 상단 돌파",
                      ts=after.index[-1], meta={"trail_pct": cfg.get("trail_long_pct", 8.0)})
    if last.close < lo:
        return Signal("gap", "short", float(last.close), float(hi),
                      _target(float(last.close), float(hi), "short", 1.5),
                      f"갭 {gap_pct:+.1f}% 후 첫시간 하단 이탈",
                      ts=after.index[-1], meta={"trail_pct": cfg.get("trail_short_pct", 4.0)})
    return None


def bounce_fade(df: pd.DataFrame, cfg: dict) -> Signal | None:
    """반등 페이드. 조건:
    1) 하락 구조: 현재가가 세션 VWAP 아래 + 세션 저점이 초반 저점보다 낮음(저점 갱신)
    2) 반등: 세션 저점 대비 반등해 VWAP 또는 20봉 SMA 근처(0.3% 이내)까지 접근
    3) 소진: 분봉 RSI ≥ rsi_hot 이고 마지막 봉이 음봉
    손절 = 반등 고점 위, 목표 = 세션 저점."""
    look = cfg.get("lookback", 30)
    if len(df) < look + 15:
        return None
    vw = ind.vwap(df)
    sma20 = ind.sma(df["close"], 20)
    rsi = ind.rsi(df["close"], 14)
    last = df.iloc[-1]
    if last.close >= vw.iloc[-1]:
        return None
    early_low = df["low"].iloc[:look].min()
    session_low = df["low"].min()
    if session_low >= early_low:  # 저점 갱신 없음 → 하락 구조 아님
        return None
    near_vwap = abs(last.close - vw.iloc[-1]) / vw.iloc[-1] * 100 <= 0.3
    near_sma = (
        not pd.isna(sma20.iloc[-1])
        and abs(last.close - sma20.iloc[-1]) / sma20.iloc[-1] * 100 <= 0.3
    )
    bounced = last.close > session_low * 1.005
    if not (bounced and (near_vwap or near_sma)):
        return None
    if rsi.iloc[-1] < cfg.get("rsi_hot", 60) or last.close >= last.open:
        return None
    bounce_high = float(df["high"].iloc[-10:].max())
    return Signal("bounce_fade", "short", float(last.close),
                  bounce_high * 1.001, float(session_low),
                  f"VWAP/20MA 반등 소진 (RSI {rsi.iloc[-1]:.0f})", ts=df.index[-1])


def breakdown_retest(df: pd.DataFrame, cfg: dict) -> Signal | None:
    """지지 붕괴 후 리테스트 실패. 조건:
    1) 지지선: 최근 support_lookback 봉(최근 10봉 제외)의 최저가
    2) 붕괴: 그 뒤 tolerance 를 넘겨 종가 이탈한 봉 존재
    3) 리테스트: 현재가가 지지선 tolerance 이내로 되돌아왔고 마지막 봉이 음봉."""
    look = cfg.get("support_lookback", 60)
    tol = cfg.get("retest_tolerance_pct", 0.3)
    if len(df) < look + 15:
        return None
    base, recent = df.iloc[:-10], df.iloc[-10:]
    support = float(base["low"].iloc[-look:].min())
    broke = (recent["close"] < support * (1 - tol / 100)).any()
    last = df.iloc[-1]
    back_at_support = abs(last.close - support) / support * 100 <= tol
    if not (broke and back_at_support and last.close < last.open):
        return None
    stop = support * (1 + 2 * tol / 100)
    return Signal("breakdown_retest", "short", float(last.close), stop,
                  _target(float(last.close), stop, "short", 1.5),
                  f"지지 {support:,.0f} 붕괴 후 리테스트 실패", ts=df.index[-1])


def evaluate_all(df: pd.DataFrame, rules_cfg: dict,
                 prev_close: float | None = None) -> list[Signal]:
    out: list[Signal] = []
    if df.empty:
        return out
    if rules_cfg.get("orb", {}).get("enabled") and (s := orb(df, rules_cfg["orb"])):
        out.append(s)
    if rules_cfg.get("gap", {}).get("enabled") and (
        s := gap(df, rules_cfg["gap"], prev_close)
    ):
        out.append(s)
    if rules_cfg.get("bounce_fade", {}).get("enabled") and (
        s := bounce_fade(df, rules_cfg["bounce_fade"])
    ):
        out.append(s)
    if rules_cfg.get("breakdown_retest", {}).get("enabled") and (
        s := breakdown_retest(df, rules_cfg["breakdown_retest"])
    ):
        out.append(s)
    return out
