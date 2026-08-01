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
from statistics import NormalDist
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


def load_daily(exclude_etf: bool = True) -> pd.DataFrame:
    """일봉 전체를 한 번에 읽는다(종목별 3,941회 질의 대신 1회).

    ## ETF/ETN/리츠는 기본 제외한다 — 발굴과 같은 잣대(data/exclude.py)

    이 필터가 없던 대가를 하루에 두 번 치렀다(2026-08-01).

    1. 조건식 백테스트의 "무작위 대비 -0.135R vs -0.304R 우위" — 통과 표본의
       **87.4%가 금리형·머니마켓 ETF** 였다. 매일 0.01%씩 오르는 현금성 상품이라
       항상 신고가·항상 이평 위·항상 정배열이다. 우위의 실체는 시장 불참이었다.
    2. 발굴 점수 재측정 — 반대 방향으로 왜곡했다. ETF 포함이면 점수 2점 이상이
       무작위 대비 +0.007(t=+0.67)로 무해해 보이고, 빼면 -0.135(t=-8.33)다.

    발굴은 이름 기반으로 ETF 를 제외하는데(`data/exclude.py`) 하네스는
    전종목을 썼다 — **유니버스가 발굴과 다르면 측정이 발굴에 이식되지 않는다.**
    """
    with sqlite3.connect(store.DB_PATH) as conn:
        df = pd.read_sql_query(
            "SELECT symbol, ts, open, high, low, close, volume FROM bars "
            "WHERE tf='1d' ORDER BY symbol, ts", conn)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"])
    if exclude_etf:
        from ..data.exclude import is_excluded
        from ..data.symbols import DB_PATH as SYM_DB

        cfg = settings.CONFIG.get("nightly", {})
        try:
            with sqlite3.connect(SYM_DB) as conn:
                names = dict(conn.execute("SELECT code, name FROM symbol_master"))
        except sqlite3.Error:
            names = {}
        if names:
            bad = {c for c, n in names.items() if is_excluded(n or "", cfg)}
            df = df[~df["symbol"].isin(bad)]
        else:
            log.warning("종목 마스터가 비어 ETF 를 못 가른다 — 전종목으로 진행 "
                        "(결과 해석 시 ETF 오염 주의)")
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


BETA_CONTROL = "atr_pct"   # 잔차화 대조 축 — 변동성이 곧 베타 프록시다


def _spearman(g: pd.DataFrame, feat: str, target: str = "fwd_1") -> float:
    """하루치 순위상관. 순위로 바꾼 뒤 피어슨 = 스피어만이라 scipy 가 필요 없다."""
    if len(g) < MIN_CROSS_SECTION or g[feat].nunique() < 2:
        return float("nan")
    if target not in g.columns or g[target].nunique() < 2:
        return float("nan")
    return g[feat].rank().corr(g[target].rank())


def _resid_spearman(g: pd.DataFrame, feat: str, target: str = "fwd_1",
                    control: str = BETA_CONTROL) -> float:
    """변동성으로 설명되는 부분을 뺀 **잔차**의 순위상관.

    횡단면 demeaning(_spearman 이 날짜 내 순위를 쓰는 것)은 그날 시장 '수준'만
    제거한다. 베타는 수익률의 레벨이 아니라 **종목의 성질**이라 그대로 남는다.
    실측 2026-07-27: 7피처 전부가 익일 상승일/하락일에 부호를 뒤집었다 —
    피처 집합 전체가 단일 요인에 실려 있다는 뜻이다.

    피처 순위를 대조축(변동성) 순위에 1차 회귀시킨 잔차를 쓰면 '변동성으로
    설명되지 않는 부분' 만 남는다. 회귀계수는 b = cov(x,z)/var(z) — scipy 도
    numpy polyfit 도 필요 없다(_spearman 이 scipy 를 피한 것과 같은 방식).
    """
    if feat == control:
        return float("nan")        # 자기 자신에 회귀시키면 잔차가 0이다
    if len(g) < MIN_CROSS_SECTION or control not in g.columns:
        return float("nan")
    x, z = g[feat].rank(), g[control].rank()
    if x.nunique() < 2 or z.nunique() < 2:
        return float("nan")
    if target not in g.columns or g[target].nunique() < 2:
        return float("nan")
    var_z = float(z.var())
    if not var_z or not math.isfinite(var_z):
        return float("nan")
    resid = x - (float(x.cov(z)) / var_z) * z
    if resid.nunique() < 2:
        return float("nan")
    return resid.corr(g[target].rank())


def bonferroni_t(k: int, alpha: float = 0.05) -> float:
    """k개를 동시에 검정할 때의 t 문턱. 7피처 α=0.05 → 2.69.

    피처를 여러 개 훑으면 그중 하나쯤은 우연히 |t|>2 를 넘는다. 문턱을 올리지
    않으면 '살아남은 피처'가 발견이 아니라 다중검정의 산물이 된다.
    """
    return round(NormalDist().inv_cdf(1 - alpha / (2 * max(1, k))), 2)


def _ic(df: pd.DataFrame, target: str = "fwd_1") -> list[dict]:
    """날짜별 순위상관(Spearman IC)의 평균 — 퀀트에서 쓰는 표준 예측력 지표.

    하루하루 '피처 순위와 결과 순위가 얼마나 같은 방향인가' 를 재고, 그 값들의
    평균이 0에서 유의하게 떨어져 있는지 본다. 시장 전체가 오르내린 효과는
    순위로 보기 때문에 자동으로 빠진다 — 다만 **베타는 빠지지 않으므로**
    변동성 잔차 IC(resid_ic)를 함께 낸다.
    """
    out = []
    for feat in panel_mod.IC_FEATURES:
        if feat not in df.columns:
            continue
        daily_ic = df.groupby("date").apply(
            _spearman, feat, target, include_groups=False).dropna()
        n = len(daily_ic)
        if n < 5:
            continue
        mean, std = float(daily_ic.mean()), float(daily_ic.std())
        row = {
            "feature": feat,
            "mean_ic": round(mean, 4),
            "std_ic": round(std, 4),
            "t_stat": _t_stat(mean, std, n),
            "days": n,
            "hit_rate": round(float((daily_ic > 0).mean()) * 100, 1),
            "is_control": feat == BETA_CONTROL,
        }
        rd = df.groupby("date").apply(
            _resid_spearman, feat, target, BETA_CONTROL,
            include_groups=False).dropna()
        if len(rd) >= 5:
            rm, rs = float(rd.mean()), float(rd.std())
            row |= {"resid_ic": round(rm, 4), "resid_t": _t_stat(rm, rs, len(rd)),
                    "resid_days": len(rd)}
        out.append(row)
    return sorted(out, key=lambda r: -abs(r["t_stat"]))


TREND_DAYS = 20        # 사전 국면 판정에 쓰는 직전 시장 추세 구간
MIN_REGIME_DAYS = 20   # 이보다 짧은 국면은 비교 대상으로 삼지 않는다


def market_daily(df: pd.DataFrame) -> pd.Series:
    """날짜별 시장 수익률(전 종목 횡단면 평균 일간 등락률)."""
    if "ret_1d" not in df.columns:
        return pd.Series(dtype=float)
    return df.groupby("date")["ret_1d"].mean().sort_index()


def label_regimes(df: pd.DataFrame) -> pd.DataFrame:
    """두 가지 국면 라벨을 붙인다 — 목적이 다르다.

    trend  : 직전 TREND_DAYS 일 시장 누적 등락의 부호. **그날 이미 알 수 있는**
             정보라 실전 적용이 가능하다.
    fwd_dir: 익일 시장이 실제로 올랐는가. 사전에 알 수 없으므로 매매에는 못
             쓰지만, 어떤 피처의 예측력이 **알파인지 단순 저베타 효과인지**를
             가르는 데 필요하다. 저베타라면 시장이 오르는 날 부호가 뒤집힌다.
    """
    out = df.copy()
    m = market_daily(df)
    trail = m.rolling(TREND_DAYS).sum()
    out["trend"] = out["date"].map(
        trail.apply(lambda x: "상승추세" if x > 0 else "하락추세" if x < 0 else None))
    fwd_mkt = df.groupby("date")["fwd_1"].mean()
    out["fwd_dir"] = out["date"].map(
        fwd_mkt.apply(lambda x: "익일 상승" if x > 0 else "익일 하락"))
    return out


def _split(df: pd.DataFrame, col: str, cost_pct: float) -> dict:
    """국면별 재측정 — 매수 가능 종목만 본다(판단에 쓰이는 표본)."""
    out = {}
    for label, g in df.dropna(subset=[col]).groupby(col):
        days = int(g["date"].nunique())
        if days < MIN_REGIME_DAYS:
            continue
        out[str(label)] = {
            "days": days,
            "rows": len(g),
            "market_pct": round(float(g["fwd_1"].mean()), 3),
            "buckets": _buckets(g, cost_pct),
            "ic": _ic(g),
        }
    return out


def analyze(df: pd.DataFrame, cost_pct: float) -> dict:
    """긴 표 → 버킷 성적 + IC. 순수 함수(테스트 용이)."""
    if df.empty:
        return {"rows": 0}
    # 시장 효과 제거 — 같은 날 전 종목 평균을 뺀다
    df = df.copy()
    df["excess_1"] = df["fwd_1"] - df.groupby("date")["fwd_1"].transform("mean")
    df = label_regimes(df)
    liq = df[df["liquid"] == 1]
    # 국면 분리는 매수 가능 표본에서만 — 못 사는 종목의 성적은 판단에 쓰지 않는다
    base = liq if len(liq) >= MIN_BUCKET else df
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
        # 목적어별 IC — 발굴이 방향을 공급해야 하는지 변동폭을 공급해야 하는지가
        # 여기서 갈린다. 판단에 쓰이는 매수 가능 표본 기준.
        "ic_by_target": {
            t: _ic(base, t) for t in panel_mod.TARGETS if t in base.columns
        },
        "targets": panel_mod.TARGETS,
        "beta_control": BETA_CONTROL,
        # 피처를 여러 개 훑으면 하나쯤은 우연히 |t|>2 를 넘는다
        "bonferroni_t": bonferroni_t(len(panel_mod.IC_FEATURES)),
        # 사전에 알 수 있는 국면 — 실전 적용 가능
        "by_trend": _split(base, "trend", cost_pct),
        # 사후 분해 — 매매에는 못 쓰지만 알파/베타를 가른다
        "by_fwd_dir": _split(base, "fwd_dir", cost_pct),
    }


CAVEATS = [
    "유니버스는 ETF/ETN/리츠 제외(발굴과 같은 이름 기반 잣대)다. 포함하면 측정이 두 방향으로 왜곡된다 — "
    "실측 2026-08-01: 조건식 우위의 87%가 금리형 ETF 였고, 점수의 해로움은 ETF 가 가려 무해해 보였다.",
    "생존 편향 — 종목 리스트가 현재 상장분이라 상장폐지 종목이 빠져 있다. 실제 성적은 이 수치보다 나쁠 수 있다.",
    "표본 기간이 일봉 보관분(약 1년)이라 시장 국면이 한두 개밖에 들어가지 않는다.",
    "수익률은 t+1 시가 진입 기준이다. t 종가→t+1 시가 갭(gap_pct)은 배치가 끝난 뒤 열리므로 잡을 수 없다.",
    f"표본 {MIN_BUCKET}건 미만 버킷은 reliable=false — 수치를 믿지 않는다.",
    "'익일 상승/하락' 분해는 사전에 알 수 없는 정보다. 매매 규칙으로 쓸 수 없고, "
    "어떤 피처가 알파인지 단순 저베타 효과인지 가르는 용도로만 본다 — "
    "저베타라면 시장이 오르는 날 부호가 뒤집힌다.",
]


def run_once() -> dict:
    """전 구간 이벤트 스터디 실행 → 결과 저장. CPU 무거워 자식 프로세스로 돈다."""
    cfg = settings.CONFIG.get("nightly", {})
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
