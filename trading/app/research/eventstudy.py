"""발굴 점수 이벤트 스터디 — '이 규칙이 실제로 익일 수익률과 상관이 있는가'.

머신러닝을 붙이기 전에 답해야 할 질문이다. 상관이 없으면 어떤 모델을 얹어도
소용없고, 있으면 그때 정교화가 의미를 갖는다.

측정에서 지키는 것:
- **미래 참조 없음** — 피처는 t일 종가까지, 수익률은 t+1 시가 이후.
- **잡을 수 있는 구간만** — 발굴 배치는 t일 17:30 에 도니 진입은 빨라야 t+1
  시가다. t 종가→t+1 시가 갭은 따로 재서 착각을 막는다.
- **시장 효과 제거** — 점수 3점이 상승장에 몰려 있으면 원시 수익률은 규칙이
  아니라 시장을 재는 것이다. 날짜별 횡단면 평균을 빼 초과수익으로 본다.
- **비용 반영** — 왕복 비용을 뺀 순수익도 함께 낸다.

한계(결과에 함께 싣는다):
- 생존 편향: 종목 리스트가 현재 상장분이라 상장폐지 종목이 빠져 있다.
  실제 성적은 여기 수치보다 나쁠 수 있다.
- 표본 기간이 일봉 보관분(약 1년)뿐이라 국면이 한두 개밖에 안 들어간다.
"""
import json
import logging
import math
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .. import settings
from ..data import store
from . import panel as panel_mod

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
OUT_FILE = Path(settings.DATA_DIR) / "event_study.json"
MIN_BUCKET = 30       # 이보다 적은 표본의 버킷은 수치를 믿지 않는다


def _t_stat(mean: float, std: float, n: int) -> float:
    """평균이 0과 다른지의 t 통계량. |t|>2 면 우연으로 보기 어렵다."""
    if n < 2 or std <= 0 or not math.isfinite(std):
        return 0.0
    return round(mean / (std / math.sqrt(n)), 2)


def load_daily() -> pd.DataFrame:
    """일봉 전체를 한 번에 읽는다(종목별 3,941회 질의 대신 1회)."""
    with sqlite3.connect(store.DB_PATH) as conn:
        df = pd.read_sql_query(
            "SELECT symbol, ts, open, high, low, close, volume FROM bars "
            "WHERE tf='1d' ORDER BY symbol, ts", conn)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"])
    return df


def build_panel(daily: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """종목별 피처 패널 + 익일 수익률을 하나의 긴 표로."""
    frames = []
    for code, g in daily.groupby("symbol", sort=False):
        p = panel_mod.panel(g.set_index("ts"), cfg)
        if p.empty:
            continue
        p = panel_mod.forward(p)
        p["symbol"] = code
        frames.append(p)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames).reset_index().rename(columns={"index": "ts", "ts": "date"})
    out["date"] = pd.to_datetime(out["date"]).dt.date.astype(str)
    return out.dropna(subset=["fwd_1"])


def _buckets(df: pd.DataFrame, cost_pct: float) -> list[dict]:
    """점수별 성적. excess 는 같은 날 전체 평균을 뺀 초과수익."""
    rows = []
    for score, g in df.groupby("score"):
        n = len(g)
        mean = float(g["fwd_1"].mean())
        exc = float(g["excess_1"].mean())
        rows.append({
            "score": int(score),
            "n": n,
            "mean_pct": round(mean, 3),
            "excess_pct": round(exc, 3),
            "net_pct": round(mean - cost_pct, 3),
            "win_rate": round(float((g["fwd_1"] > 0).mean()) * 100, 1),
            "median_pct": round(float(g["fwd_1"].median()), 3),
            "t_stat": _t_stat(exc, float(g["excess_1"].std()), n),
            "gap_pct": round(float(g["gap_pct"].mean()), 3),
            "fwd_3_pct": round(float(g["fwd_3"].mean()), 3) if g["fwd_3"].notna().any() else None,
            "fwd_5_pct": round(float(g["fwd_5"].mean()), 3) if g["fwd_5"].notna().any() else None,
            "reliable": n >= MIN_BUCKET,
        })
    return sorted(rows, key=lambda r: r["score"])


MIN_CROSS_SECTION = 20   # 이보다 종목이 적은 날은 순위상관이 의미 없다


def _spearman(g: pd.DataFrame, feat: str) -> float:
    """하루치 순위상관. 순위로 바꾼 뒤 피어슨 = 스피어만이라 scipy 가 필요 없다."""
    if len(g) < MIN_CROSS_SECTION or g[feat].nunique() < 2:
        return float("nan")
    return g[feat].rank().corr(g["fwd_1"].rank())


def _ic(df: pd.DataFrame) -> list[dict]:
    """날짜별 순위상관(Spearman IC)의 평균 — 퀀트에서 쓰는 표준 예측력 지표.

    하루하루 '피처 순위와 익일 수익률 순위가 얼마나 같은 방향인가' 를 재고,
    그 값들의 평균이 0에서 유의하게 떨어져 있는지 본다. 시장 전체가 오르내린
    효과는 순위로 보기 때문에 자동으로 빠진다.
    """
    out = []
    for feat in panel_mod.IC_FEATURES:
        if feat not in df.columns:
            continue
        daily_ic = df.groupby("date").apply(_spearman, feat, include_groups=False)
        daily_ic = daily_ic.dropna()
        n = len(daily_ic)
        if n < 5:
            continue
        mean = float(daily_ic.mean())
        std = float(daily_ic.std())
        out.append({
            "feature": feat,
            "mean_ic": round(mean, 4),
            "std_ic": round(std, 4),
            "t_stat": _t_stat(mean, std, n),
            "days": n,
            "hit_rate": round(float((daily_ic > 0).mean()) * 100, 1),
        })
    return sorted(out, key=lambda r: -abs(r["t_stat"]))


def analyze(df: pd.DataFrame, cost_pct: float) -> dict:
    """긴 표 → 버킷 성적 + IC. 순수 함수(테스트 용이)."""
    if df.empty:
        return {"rows": 0}
    # 시장 효과 제거 — 같은 날 전 종목 평균을 뺀다
    df = df.copy()
    df["excess_1"] = df["fwd_1"] - df.groupby("date")["fwd_1"].transform("mean")
    liq = df[df["liquid"] == 1]
    return {
        "rows": len(df),
        "symbols": int(df["symbol"].nunique()),
        "days": int(df["date"].nunique()),
        "date_from": df["date"].min(),
        "date_to": df["date"].max(),
        "cost_pct": cost_pct,
        "market_mean_pct": round(float(df["fwd_1"].mean()), 3),
        "buckets": _buckets(df, cost_pct),
        "buckets_liquid": _buckets(liq, cost_pct) if len(liq) else [],
        "liquid_rows": len(liq),
        "ic": _ic(df),
        "ic_liquid": _ic(liq) if len(liq) else [],
    }


CAVEATS = [
    "생존 편향 — 종목 리스트가 현재 상장분이라 상장폐지 종목이 빠져 있다. 실제 성적은 이 수치보다 나쁠 수 있다.",
    "표본 기간이 일봉 보관분(약 1년)이라 시장 국면이 한두 개밖에 들어가지 않는다.",
    "수익률은 t+1 시가 진입 기준이다. t 종가→t+1 시가 갭(gap_pct)은 배치가 끝난 뒤 열리므로 잡을 수 없다.",
    f"표본 {MIN_BUCKET}건 미만 버킷은 reliable=false — 수치를 믿지 않는다.",
]


def run_once() -> dict:
    """전 구간 이벤트 스터디 실행 → 결과 저장. CPU 무거워 자식 프로세스로 돈다."""
    cfg = settings.CONFIG.get("discovery", {})
    cost = float(settings.CONFIG.get("research", {}).get(
        "cost_pct", settings.COSTS.get("round_trip_pct", 0.28)))
    daily = load_daily()
    if daily.empty:
        return {"ok": False, "error": "일봉 데이터 없음 — 야간 발굴이 먼저 돌아야 합니다"}
    log.info("이벤트 스터디: 일봉 %d행 / %d종목", len(daily), daily["symbol"].nunique())
    df = build_panel(daily, cfg)
    if df.empty:
        return {"ok": False, "error": "피처 패널이 비었습니다(60일 미만 종목뿐)"}
    result = analyze(df, cost)
    result |= {
        "ok": True,
        "run_ts": datetime.now(KST).isoformat(timespec="seconds"),
        "caveats": CAVEATS,
    }
    save(result)
    log.info("이벤트 스터디 완료: %d행 / %d일 / %d종목",
             result["rows"], result["days"], result["symbols"])
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
