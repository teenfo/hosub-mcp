"""매매 규칙(트레이딩 테크닉) → Signal 생성.

모든 규칙은 '오늘의 1분봉 DataFrame(장 시작~현재)' 을 입력으로 받고,
진입가·손절가·목표가가 채워진 Signal 을 반환하거나 None 을 반환한다.
Exit 우선 원칙: stop/target 없는 신호는 존재할 수 없다.

## 새 기법 추가 방법 (레지스트리 패턴)
1. 이 파일에 `def my_rule(df, cfg) -> Signal | None:` 함수를 작성한다.
   (전일 종가가 필요하면 `def my_rule(df, cfg, prev_close)` 시그니처 사용)
2. 함수 위에 `@register("my_rule")` (전일 종가 필요 시
   `@register("my_rule", needs_prev_close=True)`) 데코레이터를 붙인다.
3. config.yaml `rules:` 에 `my_rule: {enabled: true, ...}` 블록을 추가한다.
끝. evaluate_all 이 레지스트리를 순회하므로 다른 코드는 수정할 필요 없다.
공통 안전장치(손절폭 상한 max_stop_pct)와 엔진 게이트(롱 전용·국면·잔고·
리스크 사이징)는 모든 규칙에 자동 적용된다.
"""
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time

import pandas as pd

from . import indicators as ind

# 규칙 레지스트리: 이름 → (함수, 전일종가 필요 여부, 방향). 등록 순서 = 평가 순서.
REGISTRY: dict[str, tuple[Callable, bool, str]] = {}


def register(name: str, needs_prev_close: bool = False, side: str = "both"):
    """규칙 함수를 레지스트리에 등록하는 데코레이터. side: long/short/both."""
    def deco(fn):
        REGISTRY[name] = (fn, needs_prev_close, side)
        return fn
    return deco


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


def _bearish_reversal(df: pd.DataFrame) -> bool:
    """마지막 봉이 하락 반전 캔들인가 — 딥리서치 3단계 '캔들 확인' 신호.
    하락장악형(bearish engulfing) 또는 유성형(shooting star)."""
    if len(df) < 2:
        return False
    p, c = df.iloc[-2], df.iloc[-1]
    rng = (c.high - c.low) or 1e-9
    body = abs(c.close - c.open)
    upper = c.high - max(c.close, c.open)
    lower = min(c.close, c.open) - c.low
    # 하락장악형: 직전 양봉의 몸통을 덮는 음봉
    engulf = (
        c.close < c.open and p.close > p.open
        and c.open >= p.close and c.close <= p.open
    )
    # 유성형: 긴 윗꼬리(몸통 2배↑) + 짧은 아랫꼬리 + 몸통이 캔들 하단부
    star = upper >= 2 * body and lower <= body and body <= rng * 0.4
    return bool(engulf or star)


def _atr_buffer(df: pd.DataFrame, cfg: dict, fallback: float) -> float:
    """저항선 위 손절 여유폭 = atr_stop_mult × ATR (스탑 헌팅 회피).
    딥리서치 6단계: 저항에서 0.5~1.0 ATR 위에 손절. ATR 계산 불가 시 fallback."""
    mult = cfg.get("atr_stop_mult", 0.0)
    if mult <= 0:
        return fallback
    a = ind.atr(df, cfg.get("atr_period", 14)).iloc[-1]
    return float(mult * a) if a and not pd.isna(a) and a > 0 else fallback


def _downtrend_ok(cfg: dict) -> bool:
    """추세 게이트 — 딥리서치 1단계. require_downtrend 이 켜져 있고
    엔진이 주입한 일봉 추세(_daily_downtrend)가 하락이 아니면 진입 차단.
    일봉 정보가 없으면(_daily_downtrend 미주입) 통과시킨다(과차단 방지)."""
    if not cfg.get("require_downtrend"):
        return True
    return bool(cfg.get("_daily_downtrend", True))


@register("orb", side="both")
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


@register("gap", needs_prev_close=True, side="both")
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


@register("momentum", side="long")
def momentum_breakout(df: pd.DataFrame, cfg: dict) -> Signal | None:
    """장중 모멘텀 돌파(롱). 상승 강세(현재가 > VWAP)에서 직전 N봉 고가를 종가가
    돌파하고 마지막 봉이 양봉일 때 진입. 손절은 최근 stop_lookback 봉 저점 - ATR
    여유(타이트), 목표 = target_r × 손절 거리. ORB/gap 이 못 잡는 상시 롱 셋업."""
    look = cfg.get("lookback", 20)
    if len(df) < look + 5:
        return None
    vw = ind.vwap(df)
    last = df.iloc[-1]
    if pd.isna(vw.iloc[-1]) or last.close <= vw.iloc[-1]:   # VWAP 위(장중 강세)만
        return None
    prior_high = float(df["high"].iloc[-(look + 1):-1].max())  # 현재봉 제외 직전 N봉 고가
    if not (last.close > prior_high and last.close > last.open):
        return None
    stop_low = float(df["low"].iloc[-cfg.get("stop_lookback", 5):].min())
    stop = stop_low - _atr_buffer(df, cfg, stop_low * 0.001)
    if stop >= last.close:                                   # 손절이 진입 위면 무효
        return None
    r = cfg.get("target_r", 1.5)
    return Signal("momentum", "long", float(last.close), stop,
                  _target(float(last.close), stop, "long", r),
                  f"{look}봉 고가 {prior_high:,.0f} 돌파", ts=df.index[-1])


@register("pullback", side="long")
def pullback_long(df: pd.DataFrame, cfg: dict) -> Signal | None:
    """눌림목 롱. 상승 추세(현재가 > 초반 평균 & VWAP 근처/위)에서 20봉 이평으로
    되돌린 뒤(near_pct 이내) 반등 양봉이 나오면 진입. 손절은 눌림 저점 - ATR 여유."""
    if len(df) < 40:
        return None
    vw = ind.vwap(df)
    sma20 = ind.sma(df["close"], 20)
    last = df.iloc[-1]
    if pd.isna(sma20.iloc[-1]) or pd.isna(vw.iloc[-1]):
        return None
    uptrend = (last.close > float(df["close"].iloc[:10].mean())
               and last.close >= vw.iloc[-1] * 0.999)
    near_ma = abs(last.close - sma20.iloc[-1]) / sma20.iloc[-1] * 100 <= cfg.get("near_pct", 0.4)
    if not (uptrend and near_ma and last.close > last.open):
        return None
    pull_low = float(df["low"].iloc[-cfg.get("stop_lookback", 5):].min())
    stop = pull_low - _atr_buffer(df, cfg, pull_low * 0.001)
    if stop >= last.close:
        return None
    r = cfg.get("target_r", 1.5)
    return Signal("pullback", "long", float(last.close), stop,
                  _target(float(last.close), stop, "long", r),
                  "상승추세 20MA 눌림 반등", ts=df.index[-1])


@register("vwap_reclaim", side="long")
def vwap_reclaim(df: pd.DataFrame, cfg: dict) -> Signal | None:
    """VWAP 되찾기(롱). 세션 중 VWAP 아래로 밀렸던 가격이 VWAP 위로 복귀
    (직전 봉 이하 → 현재 봉 위) + 양봉 + 거래량 확인 시 진입. 기관 평균단가
    회복 = 수급 전환 신호라는 정석 인트라데이 셋업. 손절은 최근 저점 - ATR."""
    if len(df) < cfg.get("min_bars", 30):
        return None
    vw = ind.vwap(df)
    if pd.isna(vw.iloc[-1]) or pd.isna(vw.iloc[-2]):
        return None
    prev, last = df.iloc[-2], df.iloc[-1]
    crossed = prev.close <= vw.iloc[-2] and last.close > vw.iloc[-1]
    if not (crossed and last.close > last.open):
        return None
    # 세션 중 실제로 VWAP 아래에 머문 시간이 있어야(살짝 스친 게 아니라)
    below_bars = int((df["close"] < vw).iloc[-cfg.get("below_lookback", 15):].sum())
    if below_bars < cfg.get("min_below_bars", 5):
        return None
    if cfg.get("vol_ratio", 0):   # 거래량 확인(옵션): 현재봉 ≥ 세션평균×배수
        if last.volume < df["volume"].mean() * cfg["vol_ratio"]:
            return None
    stop_low = float(df["low"].iloc[-cfg.get("stop_lookback", 5):].min())
    stop = stop_low - _atr_buffer(df, cfg, stop_low * 0.001)
    if stop >= last.close:
        return None
    r = cfg.get("target_r", 1.5)
    return Signal("vwap_reclaim", "long", float(last.close), stop,
                  _target(float(last.close), stop, "long", r),
                  f"VWAP {vw.iloc[-1]:,.0f} 되찾기 (아래 체류 {below_bars}봉)",
                  ts=df.index[-1])


@register("range_break_retest", side="long")
def range_break_retest(df: pd.DataFrame, cfg: dict) -> Signal | None:
    """박스 상단 돌파 후 리테스트 지지(롱). 직전 박스(최근 10봉 제외 N봉) 상단을
    돌파한 뒤, 상단 근처(tolerance)로 되돌아와 지지 양봉이 나오면 진입 —
    돌파 추격보다 유리한 진입가 + 명확한 무효화 지점(상단 하회). breakdown_retest
    의 롱 대칭. 손절은 박스 상단 - ATR 여유."""
    look = cfg.get("range_lookback", 60)
    tol = cfg.get("retest_tolerance_pct", 0.3)
    if len(df) < look + 15:
        return None
    base, recent = df.iloc[:-10], df.iloc[-10:]
    box_top = float(base["high"].iloc[-look:].max())
    broke = (recent["close"] > box_top * (1 + tol / 100)).any()
    last = df.iloc[-1]
    at_top = abs(last.close - box_top) / box_top * 100 <= tol
    if not (broke and at_top and last.close > last.open):
        return None
    stop = box_top - _atr_buffer(df, cfg, box_top * (2 * tol / 100))
    if stop >= last.close:
        return None
    r = cfg.get("target_r", 1.5)
    return Signal("range_break_retest", "long", float(last.close), stop,
                  _target(float(last.close), stop, "long", r),
                  f"박스 상단 {box_top:,.0f} 돌파 후 리테스트 지지", ts=df.index[-1])


@register("bounce_fade", side="short")
def bounce_fade(df: pd.DataFrame, cfg: dict) -> Signal | None:
    """반등 페이드. 조건:
    1) 하락 구조: 현재가가 세션 VWAP 아래 + 세션 저점이 초반 저점보다 낮음(저점 갱신)
    2) 반등: 세션 저점 대비 반등해 VWAP 또는 20봉 SMA 근처(0.3% 이내)까지 접근
    3) 소진: 분봉 RSI 반등 과열권(rsi_hot~rsi_max) + 하락 반전 캔들
    손절 = 반등 고점 + ATR 여유, 목표 = 세션 저점.
    딥리서치 검증 결론(반등 페이드가 하락장 단타의 추세 정렬 방향)을 코드화."""
    look = cfg.get("lookback", 30)
    if len(df) < look + 15:
        return None
    if not _downtrend_ok(cfg):        # 1단계: 추세 게이트(옵션)
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
    r = float(rsi.iloc[-1])
    if r < cfg.get("rsi_hot", 60) or r > cfg.get("rsi_max", 100):
        return None
    # 캔들 확인: 'reversal'(하락장악/유성) 기본, 'any'(단순 음봉) 선택 가능
    if cfg.get("candle_confirm", "reversal") == "reversal":
        if not _bearish_reversal(df):
            return None
    elif last.close >= last.open:
        return None
    # 반등 거래량 소진(옵션): 최근 반등 구간 거래량이 세션 평균 이하일 때만
    if cfg.get("require_volume_dry"):
        if df["volume"].iloc[-3:].mean() > df["volume"].mean():
            return None
    bounce_high = float(df["high"].iloc[-10:].max())
    stop = bounce_high + _atr_buffer(df, cfg, bounce_high * 0.001)
    return Signal("bounce_fade", "short", float(last.close),
                  stop, float(session_low),
                  f"VWAP/20MA 반등 소진 (RSI {r:.0f})", ts=df.index[-1])


@register("breakdown_retest", side="short")
def breakdown_retest(df: pd.DataFrame, cfg: dict) -> Signal | None:
    """지지 붕괴 후 리테스트 실패. 조건:
    1) 지지선: 최근 support_lookback 봉(최근 10봉 제외)의 최저가
    2) 붕괴: 그 뒤 tolerance 를 넘겨 종가 이탈한 봉 존재
    3) 리테스트: 현재가가 지지선 tolerance 이내로 되돌아왔고 마지막 봉이 음봉."""
    look = cfg.get("support_lookback", 60)
    tol = cfg.get("retest_tolerance_pct", 0.3)
    if len(df) < look + 15:
        return None
    if not _downtrend_ok(cfg):        # 1단계: 추세 게이트(옵션)
        return None
    base, recent = df.iloc[:-10], df.iloc[-10:]
    support = float(base["low"].iloc[-look:].min())
    broke = (recent["close"] < support * (1 - tol / 100)).any()
    last = df.iloc[-1]
    back_at_support = abs(last.close - support) / support * 100 <= tol
    if not (broke and back_at_support and last.close < last.open):
        return None
    # 하방 이탈 거래량 확인(옵션·기본 OFF). 딥리서치에서 '평균 120% 필터'는
    # 검증 가능한 출처 없음으로 기각됐으므로 하드코딩하지 않고 선택 사용.
    if cfg.get("require_volume"):
        avg = df["volume"].iloc[-(look + 10):].mean()
        if recent["volume"].max() < avg * cfg.get("vol_confirm_ratio", 1.2):
            return None
    stop = support + _atr_buffer(df, cfg, support * (2 * tol / 100))
    return Signal("breakdown_retest", "short", float(last.close), stop,
                  _target(float(last.close), stop, "short", 1.5),
                  f"지지 {support:,.0f} 붕괴 후 리테스트 실패", ts=df.index[-1])


def evaluate_all(df: pd.DataFrame, rules_cfg: dict,
                 prev_close: float | None = None) -> list[Signal]:
    """레지스트리의 모든 규칙을 순회 평가. config 에 enabled 인 규칙만 실행하고,
    개별 규칙의 예외는 다른 규칙 평가를 막지 않는다(격리)."""
    out: list[Signal] = []
    if df.empty:
        return out
    for name, (fn, needs_prev, _side) in REGISTRY.items():
        cfg = rules_cfg.get(name, {})
        if not cfg.get("enabled"):
            continue
        try:
            s = fn(df, cfg, prev_close) if needs_prev else fn(df, cfg)
        except Exception:  # noqa: BLE001 - 규칙 하나의 버그가 전체 평가를 막지 않게
            import logging
            logging.getLogger(__name__).exception("규칙 %s 평가 오류", name)
            continue
        if s:
            out.append(s)
    # 손절폭 상한 필터: 손절 거리가 과도한(고변동성·와이드스탑) 신호는 버린다.
    # OCI 사례처럼 손절이 6% 떨어진 곳에 잡히는 저품질·휩쏘 거래를 애초에 차단.
    max_stop = rules_cfg.get("max_stop_pct", 0)
    if max_stop:
        out = [s for s in out
               if s.entry and abs(s.entry - s.stop) / s.entry * 100 <= max_stop]
    return out
