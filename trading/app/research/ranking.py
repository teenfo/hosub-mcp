"""발굴 랭킹 방식 비교 — "어떤 순서로 줄을 세워야 실제 R 이 나오는가".

이벤트 스터디는 '순위가 맞는가'(IC)만 잰다. 그런데 IC 가 좋아도 손절·목표·
비용을 통과하면 R 이 안 나올 수 있다. 여기서는 각 랭킹 방식으로 상위 N 을
뽑았을 때의 **실현 R** 을 근사해 방식끼리 비교한다.

## 왜 근사인가 (정직하게)

진짜 백테스트는 불가능하다. 1분봉은 196종목뿐이고 발굴 유니버스는 3,907종목이다.
대신 일봉의 고가·저가로 브래킷을 근사한다.

  손절 −s% · 목표 +s×R%  (진입 = t+1 시가)
    fwd_dn_1 ≤ −s     → −1R      ← 둘 다 닿으면 손절 우선(보수적)
    fwd_up_1 ≥ +s×R   → +R
    그 외              → fwd_1 / s  R  (종가 청산)
  비용 cost_pct / s 를 R 단위로 차감

한계: 장중 순서를 모르고, ORB 의 09:15 진입 타이밍도 재현할 수 없다. 다만 이
편향이 **모든 방식에 공통**으로 걸리므로 **방식 간 비교에는 유효**하다.
절대 R 값은 신뢰하지 않는다.

## 왜 walk-forward 인가

잔차 IC 로 가중치를 만들어 같은 구간에서 평가하면 순환 논리다 — IC 가 높게 나온
피처를 골랐으니 당연히 잘 나온다. `composite_wf` 는 **직전 TRAIN_DAYS 일**의
잔차 IC 로만 가중치를 만들어 당일에 적용한다. `composite_is`(전 구간 학습)를
나란히 두는 이유는 그 격차가 곧 **과적합의 크기**이기 때문이다.
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .. import settings
from . import eventstudy
from . import panel as panel_mod

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
OUT_FILE = Path(settings.DATA_DIR) / "ranking.json"

TOP_N = 5              # 하루에 상위 몇 종목을 매매 후보로 볼 것인가
TRAIN_DAYS = 60        # walk-forward 학습창(거래일)
MIN_EVAL_DAYS = 20     # 이보다 적은 날로 평가하지 않는다
SEED = 20260727        # 무작위 대조군 재현성 — 매 실행 결과가 흔들리면 비교가 안 된다
TARGET = "fwd_up_1"    # 합성 가중치를 학습할 목적어(장중 규칙이 먹는 값)


def bracket_r(up: float, dn: float, close: float, stop_pct: float,
              target_r: float, cost_pct: float) -> float:
    """일봉 브래킷 근사 → R. 전부 '익일 시가 대비 %' 입력.

    같은 날 손절·목표가 둘 다 닿으면 어느 쪽이 먼저인지 일봉으로는 알 수 없다.
    **손절 우선**으로 가정한다 — 성적을 부풀리지 않는 쪽이다.
    """
    if stop_pct <= 0 or not np.isfinite(stop_pct):
        return float("nan")
    cost_r = cost_pct / stop_pct
    if not np.isfinite(dn) or not np.isfinite(up) or not np.isfinite(close):
        return float("nan")
    if dn <= -stop_pct:
        return -1.0 - cost_r
    if up >= stop_pct * target_r:
        return target_r - cost_r
    return close / stop_pct - cost_r


def _z(s: pd.Series) -> pd.Series:
    """날짜 안에서의 표준화 — 피처 단위가 달라도 합칠 수 있게."""
    sd = s.std()
    if not sd or not np.isfinite(sd):
        return pd.Series(0.0, index=s.index)
    return (s - s.mean()) / sd


def resid_weights(df: pd.DataFrame, target: str = TARGET) -> dict[str, float]:
    """구간의 잔차 IC 를 가중치로. 변동성으로 설명되는 부분은 이미 빠져 있다."""
    out: dict[str, float] = {}
    for feat in panel_mod.IC_FEATURES:
        if feat == eventstudy.BETA_CONTROL or feat not in df.columns:
            continue
        daily = df.groupby("date").apply(
            eventstudy._resid_spearman, feat, target,
            eventstudy.BETA_CONTROL, include_groups=False).dropna()
        if len(daily) >= 5:
            m = float(daily.mean())
            if np.isfinite(m):
                out[feat] = m
    return out


def _composite(g: pd.DataFrame, weights: dict[str, float]) -> pd.Series:
    """가중 z합성 — 하루치 안에서."""
    total = pd.Series(0.0, index=g.index)
    for feat, w in weights.items():
        if feat in g.columns:
            total = total + w * _z(g[feat])
    return total


def rank_series(g: pd.DataFrame, method: str, weights: dict | None,
                rng: np.random.Generator) -> pd.Series:
    """하루치 프레임 → 높을수록 먼저 뽑히는 점수. 방식마다 정의가 다르다."""
    if method == "score":
        return g["score"].astype(float)
    if method == "atr":
        return g["atr_pct"].astype(float)
    if method in ("composite_is", "composite_wf"):
        return _composite(g, weights or {})
    if method in ("random", "liquid_only"):
        return pd.Series(rng.random(len(g)), index=g.index)
    raise ValueError(f"알 수 없는 랭킹 방식: {method}")


METHODS = ("score", "atr", "composite_is", "composite_wf", "liquid_only", "random")
# liquid_only 와 random 은 둘 다 무작위지만 모집단이 다르다 —
# liquid_only 는 유동성 통과분(= 실제로 살 수 있는 종목)만, random 은 전 종목.
_ALL_UNIVERSE = ("random",)


def wf_weights(df: pd.DataFrame, dates: list, i: int) -> dict[str, float] | None:
    """i번째 날에 쓸 walk-forward 가중치 — **직전 TRAIN_DAYS 일만** 본다.

    별도 함수로 뺀 이유는 테스트 때문이다. "미래를 보지 않는다"는 주장은
    전체 데이터로 계산한 값과 그날까지 잘라낸 데이터로 계산한 값이 **같다**는
    것으로만 증명된다(test_ranking.py). 루프 안에 인라인돼 있으면 그 비교를
    할 수 없다.
    """
    if i < TRAIN_DAYS:
        return None                      # 학습창 미달 — 평가에서 제외한다
    return resid_weights(df[df["date"].isin(dates[i - TRAIN_DAYS:i])])


def evaluate(df: pd.DataFrame, method: str, stop_pct: float, target_r: float,
             cost_pct: float, top_n: int = TOP_N) -> dict:
    """방식 × 손절폭 → 성적. 날짜별 상위 N 을 뽑아 브래킷 R 을 낸다."""
    rng = np.random.default_rng(SEED)
    is_w = resid_weights(df) if method == "composite_is" else None
    dates = sorted(df["date"].unique())
    rs: list[float] = []
    used_days = 0
    for i, day in enumerate(dates):
        g = df[df["date"] == day]
        if method not in _ALL_UNIVERSE:
            g = g[g["liquid"] == 1]          # 실제로 살 수 있는 종목만
        if len(g) < top_n:
            continue
        if method == "composite_wf":
            w = wf_weights(df, dates, i)
            if not w:
                continue                     # 학습창 미달 또는 가중치 미성립
        else:
            w = is_w
        picks = g.loc[rank_series(g, method, w, rng).nlargest(top_n).index]
        used_days += 1
        for r in picks.itertuples():
            v = bracket_r(r.fwd_up_1, r.fwd_dn_1, r.fwd_1, stop_pct, target_r, cost_pct)
            if np.isfinite(v):
                rs.append(v)
    if len(rs) < top_n * MIN_EVAL_DAYS:
        return {"method": method, "stop_pct": stop_pct, "trades": len(rs),
                "days": used_days, "reliable": False}
    arr = np.array(rs, dtype=float)
    mean, std = float(arr.mean()), float(arr.std(ddof=1))
    return {
        "method": method, "stop_pct": stop_pct, "trades": len(arr), "days": used_days,
        "avg_r": round(mean, 4),
        "win_rate": round(float((arr > 0).mean()) * 100, 1),
        "total_r": round(float(arr.sum()), 1),
        "t_stat": eventstudy._t_stat(mean, std, len(arr)),
        "reliable": True,
    }


CAVEATS = [
    "일봉 브래킷 근사다. 장중 순서를 모르므로 손절·목표가 같은 날 둘 다 닿으면 손절 우선으로 가정한다(보수적).",
    "ORB 의 진입 타이밍(09:15 범위 돌파)은 재현할 수 없다 — 전부 익일 시가 진입 근사.",
    "위 두 편향은 모든 방식에 공통이므로 **방식 간 비교에는 유효**하다. 절대 R 값은 신뢰하지 않는다.",
    "composite_is 는 전 구간을 학습에 쓴 in-sample 상한이다. 실전 성적은 composite_wf 로 본다 — "
    "둘의 격차가 곧 과적합의 크기다.",
    "생존 편향(상장폐지 종목 부재)과 표본 191일 하락 편중은 그대로 남는다.",
]


def analyze(df: pd.DataFrame, stops: list[float], target_r: float,
            cost_pct: float, top_n: int = TOP_N) -> dict:
    rows = [evaluate(df, m, s, target_r, cost_pct, top_n)
            for m in METHODS for s in stops]
    ok = [r for r in rows if r.get("reliable")]
    best = max(ok, key=lambda r: r["avg_r"], default=None)
    # 판정은 대조군 대비로 한다 — random 을 못 이기면 랭킹의 존재 이유가 없다
    ctrl = {s: next((r["avg_r"] for r in ok
                     if r["method"] == "random" and r["stop_pct"] == s), None)
            for s in stops}
    for r in ok:
        c = ctrl.get(r["stop_pct"])
        r["vs_random"] = None if c is None else round(r["avg_r"] - c, 4)
    return {
        "rows": rows, "best": best, "methods": list(METHODS), "stops": stops,
        "top_n": top_n, "target_r": target_r, "cost_pct": cost_pct,
        "train_days": TRAIN_DAYS, "learn_target": TARGET,
        "caveats": CAVEATS,
    }


def run_once() -> dict:
    cfg = settings.CONFIG.get("research", {})
    rules = settings.CONFIG.get("rules", {})
    cost = float(cfg.get("cost_pct", 0.28))
    target_r = float(rules.get("orb", {}).get("target_r", 1.5))
    lo = float(rules.get("min_stop_pct", 1.0))
    hi = float(rules.get("max_stop_pct", 2.5))
    stops = [round(x, 2) for x in np.arange(lo, hi + 0.01, 0.5)]
    daily = eventstudy.load_daily()
    if daily.empty:
        return {"ok": False, "error": "일봉 데이터 없음 — 야간 발굴이 먼저 돌아야 합니다"}
    df = eventstudy.build_panel(daily, settings.CONFIG.get("discovery", {}))
    if df.empty or "fwd_dn_1" not in df.columns:
        return {"ok": False, "error": "패널이 비었거나 하방 목적어가 없습니다"}
    df = df.dropna(subset=["fwd_up_1", "fwd_dn_1", "fwd_1"])
    log.info("랭킹 비교: %d행 / %d일", len(df), df["date"].nunique())
    result = analyze(df, stops, target_r, cost) | {
        "ok": True,
        "run_ts": datetime.now(KST).isoformat(timespec="seconds"),
        "rows_used": len(df), "days": int(df["date"].nunique()),
        "symbols": int(df["symbol"].nunique()),
    }
    save(result)
    log.info("랭킹 비교 완료: best=%s", (result.get("best") or {}).get("method"))
    return result


def save(result: dict) -> None:
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(result, ensure_ascii=False, default=str),
                        encoding="utf-8")


def latest() -> dict:
    if not OUT_FILE.exists():
        return {"ok": False, "run_ts": None,
                "error": "아직 실행되지 않았습니다 — '지금 실행'을 누르세요"}
    try:
        return json.loads(OUT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": False, "run_ts": None, "error": "결과 파일을 읽을 수 없습니다"}
