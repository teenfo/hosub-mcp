"""VPIN 회피 게이트 반사실 — "전일 고VPIN 종목에 진입하지 않았다면 나았나".

## 왜 (측정 원장 2026-08-28 절 ②)

관측 4종 판정에서 VPIN 만 통과했다: 일별 상위 20% 종목의 **익일 초과수익**
−0.70% vs 나머지 +0.30%(t=−3.56). 그러나 그것은 '종목의 다음 날' 이지
**우리 거래의 다음 날** 이 아니다. 게이트로 쓰려면 실제 진입(규칙이 고른 시각·
가격·손절)에서도 고VPIN 이 나빴는지 따로 재야 한다. 이 모듈이 그 반사실이다.
사용자 결정(2026-09-28): 추천안대로 반사실 측정 선행.

## 사전 확정 기준 (구현 시점 2026-09-28 — 결과를 본 뒤 바꾸면 폐기)

- 표본: 청산 완료 포지션(status='closed'), rule 이 external·residual 이 아닌
  것, 롱만(현물 계좌). real·mock 합산(시장 관측 기반 가설이라 계좌 무관 —
  단 env 별 분할을 병기한다).
- R = (청산가 − 진입가) / (진입가 − **원본 손절**). 원본 손절(`stop`)을 쓴다 —
  데스크가 옮긴 `stop_live` 는 결과에 오염된 값이다.
- 제외: 계획 손절폭 < 0.3%(R 폭주, 8/28 강건성 기록), |청산/진입 − 1| > 30%
  (모의 체결가 오염 — OCI 79,979 → 2,800,000 등). R 은 ±5 로 클립.
- 고VPIN 정의: 진입일 **이전 마지막 관측일**의 vpin_obs 횡단면에서 상위 20%
  (그날 관측 ≥ 20종목일 때만 — 아니면 그 거래는 '미측정' 으로 빼고 0 으로
  세지 않는다).
- **통과**: 고VPIN n ≥ 30 · 나머지 n ≥ 30 이고, 평균 R 차이(고 − 나머지) < 0
  이며 Welch t ≤ −2.0. 통과 시 **회피 게이트를 shadow(기록만)로 먼저** 제안 —
  신호는 전부 기록, 발주만 게이트(불변 조건 3·4).
- 표본 미달이면 연기 보고, 미달이 아니고 기준 미충족이면 미채택(관측은 계속).
- 참고(판정 무관): 상위 10%·30% 민감도, env 분할.
"""
from __future__ import annotations

import math
from bisect import bisect_left
from statistics import mean, variance

MIN_PLAN_PCT = 0.3
MAX_MOVE = 0.30
R_CLIP = 5.0
MIN_DAY_OBS = 20
MIN_N = 30
T_PASS = -2.0
EXCLUDED_RULES = frozenset({"external", "residual"})


def trade_r(p: dict) -> float | None:
    """원본 손절 기준 R. 측정 불가(손절 없음·얇은 손절·오염 체결)는 None."""
    try:
        entry, exit_, stop = float(p["entry"]), float(p["exit"]), float(p["stop"])
    except (KeyError, TypeError, ValueError):
        return None
    if entry <= 0 or exit_ <= 0 or stop <= 0 or stop >= entry:
        return None
    if (entry - stop) / entry * 100 < MIN_PLAN_PCT:
        return None
    if abs(exit_ / entry - 1) > MAX_MOVE:
        return None
    r = (exit_ - entry) / (entry - stop)
    return max(-R_CLIP, min(R_CLIP, r))


def high_sets(vpin_rows: list[dict], top_pct: float) -> dict[str, set[str]]:
    """{관측일: 그날 상위 top_pct% 종목 집합}. 관측 < MIN_DAY_OBS 인 날은 뺀다."""
    by_day: dict[str, list[tuple[float, str]]] = {}
    for r in vpin_rows:
        v = r.get("vpin")
        if v is None:
            continue
        by_day.setdefault(str(r["d"]), []).append((float(v), str(r["code"])))
    out: dict[str, set[str]] = {}
    for d, vals in by_day.items():
        if len(vals) < MIN_DAY_OBS:
            continue
        vals.sort(reverse=True)
        k = max(1, int(round(len(vals) * top_pct / 100)))
        cut = vals[k - 1][0]
        out[d] = {c for v, c in vals if v >= cut}
    return out


def _welch(a: list[float], b: list[float]) -> float | None:
    if len(a) < 2 or len(b) < 2:
        return None
    se = math.sqrt(variance(a) / len(a) + variance(b) / len(b))
    return (mean(a) - mean(b)) / se if se > 0 else None


def evaluate(positions: list[dict], vpin_rows: list[dict],
             top_pct: float = 20.0) -> dict:
    """포지션 × 전일 VPIN 상위 여부 → 두 집단의 R 비교."""
    highs = high_sets(vpin_rows, top_pct)
    days = sorted(highs)
    obs_codes: dict[str, set[str]] = {}
    for r in vpin_rows:
        obs_codes.setdefault(str(r["d"]), set()).add(str(r["code"]))
    hi: list[float] = []
    rest: list[float] = []
    by_env: dict[str, dict[str, list[float]]] = {}
    skipped = {"rule_or_side": 0, "r_unmeasurable": 0, "no_prior_vpin": 0}
    for p in positions:
        if p.get("rule") in EXCLUDED_RULES or p.get("side") != "long":
            skipped["rule_or_side"] += 1
            continue
        r = trade_r(p)
        if r is None:
            skipped["r_unmeasurable"] += 1
            continue
        day = str(p.get("opened") or "")[:10]
        i = bisect_left(days, day) - 1          # 진입일 **이전** 마지막 관측일
        if i < 0 or p["symbol"] not in obs_codes.get(days[i], set()):
            skipped["no_prior_vpin"] += 1       # 못 잰 것은 0 으로 세지 않는다
            continue
        is_hi = p["symbol"] in highs[days[i]]
        (hi if is_hi else rest).append(r)
        env = by_env.setdefault(str(p.get("env") or "real"), {"hi": [], "rest": []})
        env["hi" if is_hi else "rest"].append(r)

    def _summ(xs: list[float]) -> dict:
        return {"n": len(xs), "avg_r": round(mean(xs), 4) if xs else None}

    t = _welch(hi, rest)
    diff = (mean(hi) - mean(rest)) if hi and rest else None
    if len(hi) < MIN_N or len(rest) < MIN_N:
        verdict = "연기(표본 미달)"
    elif diff is not None and diff < 0 and t is not None and t <= T_PASS:
        verdict = "통과 — shadow 회피 게이트 제안"
    else:
        verdict = "미채택"
    kept = rest
    return {
        "top_pct": top_pct,
        "high": _summ(hi), "rest": _summ(rest),
        "diff_r": round(diff, 4) if diff is not None else None,
        "t": round(t, 2) if t is not None else None,
        "all_avg_r": round(mean(hi + rest), 4) if hi or rest else None,
        "avoid_avg_r": round(mean(kept), 4) if kept else None,
        "by_env": {e: {"high": _summ(v["hi"]), "rest": _summ(v["rest"])}
                   for e, v in sorted(by_env.items())},
        "skipped": skipped,
        "verdict": verdict,
    }


def run_once() -> dict:
    """서버 실행 — 원장 두 곳을 읽기만 한다(쓰기 없음, API 콜 0)."""
    from ..scout import store
    from ..trade import ledger

    with ledger._conn() as conn:
        positions = [dict(r) for r in conn.execute(
            "SELECT * FROM positions WHERE status='closed'")]
    rows = store.vpin_rows(days=400)
    main = evaluate(positions, rows, 20.0)
    main["sensitivity"] = {str(k): {kk: evaluate(positions, rows, k)[kk]
                                    for kk in ("high", "rest", "diff_r", "t")}
                           for k in (10.0, 30.0)}
    main["vpin_days"] = len({r["d"] for r in rows})
    return main
