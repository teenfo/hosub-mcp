"""정규화 수급 재현 측정 — 정합 필터(Matched Filter) 원리의 자체 검증.

## 무엇을 재나

기관 매매 기법 문서(2026-08-01)가 인용한 연구의 핵심 주장:

  국내 기관의 순매수는 **시가총액(SMC)** 으로 정규화해야 익일 수익을 예측하고
  (t=9.65), 외국인 순매수는 **거래대금(STV)** 으로 정규화해야 한다(t=16.35).
  주체마다 집행 알고리즘이 달라(기관=운용 한도 비례, 외국인=VWAP 계열 분할)
  정규화 분모를 주체의 행동 양식에 **정합**시켜야 신호가 나온다는 원리다.

논문 수치를 믿고 편입하는 것은 이 저장소의 측정 원칙 위반이다. **우리 유니버스
·우리 기간에서 재현되는지** 를 먼저 잰다 — 발굴 선별에서 측정된 우위는 지금까지
뉴스 카테고리 하나뿐이므로, 재현되면 두 번째 후보가 된다.

## 판정 기준 (미리 못박는다 — 결과를 보고 바꾸지 않는다)

신호 6종(기관·외국인 × 원시·/SMC·/STV) × 목적어 1(익일 시가→종가)의 잔차 IC.

  재현 성립 = ① 외국인/STV 의 잔차 IC > 0 이고 |t| ≥ 본페로니 문턱(6종, α=0.05)
             ② 그 IC 가 외국인 원시·외국인/SMC 보다 클 것 (정규화 '정합'의 우위)
  기관은 /SMC 로 같은 구조를 본다. ①만 서고 ②가 무너지면 "수급 자체는 정보지만
  정합 원리는 재현 안 됨" 으로 따로 보고한다.

잔차는 변동성(atr_pct) 순위에 1차 회귀한 나머지다 — 발굴 3규칙이 베타 프록시로
판명된 전철(이벤트 스터디 2026-07-27)을 밟지 않기 위한 필수 통제다.

## 근사와 한계 (결과에 함께 싣는다)

- 순매수가 **주 수량**(ka10086)이라 금액은 `수량 × 그날 종가` 근사다
- 시가총액은 `현재 상장주식수 프록시(mac/현재가) × 그날 종가` — 기간 내 증자·
  분할을 무시한다. IC 는 순위 기반이라 단위 상수는 소거된다
- STV 는 `종가 × 거래량` 근사(장중 체결가 분포 무시)
- 원 연구와 기간·시장 국면이 다르다. 재현 실패가 곧 원 연구의 반증은 아니다

## 관측·분석 전용

통과해도 소스 편입은 **제안까지만** — 결정은 사용자가 한다.
"""
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from statistics import NormalDist
from zoneinfo import ZoneInfo

import pandas as pd

from .. import settings

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
OUT_FILE = Path(settings.DATA_DIR) / "flowsignal.json"

SIGNALS = ("inst_raw", "inst_smc", "inst_stv",
           "frgn_raw", "frgn_smc", "frgn_stv")
BONF_T = round(NormalDist().inv_cdf(1 - 0.05 / (2 * len(SIGNALS))), 2)  # 2.64

# 백필 진행 상태 — 서비스 프로세스 안에서 도는 비동기 작업의 관측 창
PROGRESS = {"running": False, "done": 0, "total": 0, "fails": 0,
            "caps": 0, "finished_at": None}


def _cfg() -> dict:
    return (settings.CONFIG.get("research", {}) or {}).get("flowsignal", {})


# --------------------------------------------------------------------------
# 백필 — 유동성 유니버스의 수급(ka10086, 100일/콜)·시총(ka10095, 80종목/콜)
# --------------------------------------------------------------------------
def universe() -> list[tuple[str, str]]:
    """야간 배치의 최신 유동성 통과 전량(chart_obs). (code, name)."""
    from ..scout import nightly

    if not Path(nightly.DB_PATH).exists():
        return []
    with sqlite3.connect(nightly.DB_PATH) as conn:
        row = conn.execute("SELECT MAX(date) d FROM chart_obs").fetchone()
        if not row or not row[0]:
            return []
        return [(r[0], r[1] or "") for r in conn.execute(
            "SELECT code, name FROM chart_obs WHERE date=?", (row[0],))]


def record_caps(rows: list[dict]) -> int:
    """ka10095 행 → cap_obs. `mac`(시총)과 현재가만 있으면 된다 —
    상장주식수 프록시 = mac / cur_prc. 순위 IC 라 mac 의 단위는 소거된다."""
    from ..scout import store as scout_store

    kept = []
    for r in rows:
        code = str(r.get("stk_cd", "")).strip()[-6:]
        mac = _num(r.get("mac"))
        price = _num(r.get("cur_prc"))
        if len(code) == 6 and mac > 0 and price > 0:
            kept.append((code, str(r.get("stk_nm", "")).strip(), mac, price,
                         datetime.now(KST).isoformat(timespec="seconds")))
    if not kept:
        return 0
    with sqlite3.connect(scout_store.DB_PATH) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS cap_obs (
                code TEXT PRIMARY KEY, name TEXT,
                mac REAL, price REAL, updated_at TEXT)""")
        conn.executemany(
            "INSERT OR REPLACE INTO cap_obs VALUES (?,?,?,?,?)", kept)
    return len(kept)


def _num(v) -> float:
    try:
        return abs(float(str(v).replace(",", "").replace("+", "").replace("-", "")))
    except (TypeError, ValueError):
        return 0.0


async def backfill(days: int = 100) -> dict:
    """유니버스 전량 수급 소급 + 시총 수집. 서비스 프로세스에서 돈다 —
    공유 클라이언트의 레이트리밋을 그대로 따른다(다른 루프와 예산 공유)."""
    from ..kiwoom import flows as kflows
    from ..kiwoom.client import client
    from ..scout import store as scout_store

    syms = universe()
    PROGRESS.update(running=True, done=0, total=len(syms), fails=0, caps=0,
                    finished_at=None)
    try:
        # ① 시총 — 80종목/콜 배치
        for i in range(0, len(syms), 80):
            chunk = syms[i:i + 80]
            try:
                raw = await client.watch_info([c for c, _n in chunk])
                PROGRESS["caps"] += record_caps(raw.get("atn_stk_infr") or [])
            except Exception:  # noqa: BLE001 - 한 배치 실패가 전체를 막지 않는다
                log.warning("시총 배치 실패 %d~", i, exc_info=True)
        # ② 수급 — 종목당 1콜, 100일치
        for code, name in syms:
            try:
                rows = kflows.parse_daily_price(await client.daily_price(code))
                scout_store.record_flows(code, rows[:days], name)
            except Exception:  # noqa: BLE001
                PROGRESS["fails"] += 1
            PROGRESS["done"] += 1
        return {"ok": True, **{k: PROGRESS[k] for k in
                               ("done", "total", "fails", "caps")}}
    finally:
        PROGRESS["running"] = False
        PROGRESS["finished_at"] = datetime.now(KST).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# 분석 — 일자별 횡단면 순위 IC (변동성 잔차화 포함)
# --------------------------------------------------------------------------
def _resid_corr(x: pd.Series, y: pd.Series, z: pd.Series) -> float | None:
    """x 순위를 z 순위에 회귀한 잔차와 y 순위의 상관 — eventstudy 와 같은 방식."""
    xr, yr, zr = x.rank(), y.rank(), z.rank()
    var = zr.var()
    if not var or pd.isna(var):
        return None
    b = xr.cov(zr) / var
    resid = xr - b * zr
    if resid.std() == 0:
        return None
    c = resid.corr(yr)
    return None if pd.isna(c) else float(c)


def _load_frame(days: int) -> pd.DataFrame:
    from ..data import exclude, store as bars_store
    from ..scout import store as scout_store

    cutoff = (datetime.now(KST).date() - timedelta(days=days * 2)).isoformat()
    with sqlite3.connect(scout_store.DB_PATH) as conn:
        try:
            flows = pd.read_sql_query(
                "SELECT d, code, name, orgn, foreign_ FROM flow_obs "
                "WHERE d >= ? AND (orgn IS NOT NULL OR foreign_ IS NOT NULL)",
                conn, params=(cutoff,))
            caps = pd.read_sql_query(
                "SELECT code, mac, price FROM cap_obs", conn)
        except (pd.errors.DatabaseError, sqlite3.OperationalError):
            return pd.DataFrame()
    if flows.empty or caps.empty:
        return pd.DataFrame()
    # 유니버스 오염 방지 — ETF·스팩은 실전 발굴이 이름으로 제외한다(측정 원칙 5)
    flows = flows[~flows["name"].fillna("").map(exclude.is_excluded)]
    with sqlite3.connect(bars_store.DB_PATH) as conn:
        bars = pd.read_sql_query(
            "SELECT symbol code, substr(ts,1,10) d, open, high, low, close, "
            "volume FROM bars WHERE tf='1d' AND ts >= ?", conn, params=(cutoff,))
    if bars.empty:
        return pd.DataFrame()
    caps["shares"] = caps["mac"] / caps["price"]     # 상장주식수 프록시
    df = flows.merge(bars, on=["code", "d"]).merge(
        caps[["code", "shares"]], on="code")
    df = df.sort_values(["code", "d"])
    g = df.groupby("code", sort=False)
    prev_close = g["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    df["atr_pct"] = (tr.groupby(df["code"]).transform(
        lambda s: s.rolling(14).mean()) / df["close"] * 100)
    # 익일 시가→종가 — 시가 진입이 실행 가능한 목적어(하네스 규약과 동일)
    nxt_open, nxt_close = g["open"].shift(-1), g["close"].shift(-1)
    df["fwd_oc"] = (nxt_close / nxt_open - 1) * 100
    amt_inst = df["orgn"] * df["close"]              # 수량 × 종가 근사
    amt_frgn = df["foreign_"] * df["close"]
    stv = (df["close"] * df["volume"]).replace(0, pd.NA)
    smc = (df["shares"] * df["close"]).replace(0, pd.NA)
    df["inst_raw"], df["frgn_raw"] = amt_inst, amt_frgn
    df["inst_smc"], df["inst_stv"] = amt_inst / smc, amt_inst / stv
    df["frgn_smc"], df["frgn_stv"] = amt_frgn / smc, amt_frgn / stv
    return df.dropna(subset=["fwd_oc", "atr_pct"])


def analyze(days: int | None = None) -> dict:
    cfg = _cfg()
    days = days or int(cfg.get("days", 100))
    min_n = int(cfg.get("min_cross_n", 200))         # 횡단면 최소 표본
    df = _load_frame(days)
    if df.empty:
        return {"ok": False, "error": "표본 없음 — 백필이 먼저다", "days": 0}
    out: dict[str, dict] = {}
    for sig in SIGNALS:
        ics, rics = [], []
        for _d, grp in df.groupby("d"):
            sub = grp.dropna(subset=[sig])
            if len(sub) < min_n:
                continue
            ic = sub[sig].rank().corr(sub["fwd_oc"].rank())
            if not pd.isna(ic):
                ics.append(float(ic))
            ric = _resid_corr(sub[sig], sub["fwd_oc"], sub["atr_pct"])
            if ric is not None:
                rics.append(ric)

        def _t(vals: list[float]) -> tuple[float | None, float | None]:
            if len(vals) < 10:
                return None, None
            s = pd.Series(vals)
            sd = s.std()
            if not sd:
                return round(float(s.mean()), 4), None
            return (round(float(s.mean()), 4),
                    round(float(s.mean() / sd * len(s) ** 0.5), 2))

        ic_m, ic_t = _t(ics)
        ric_m, ric_t = _t(rics)
        out[sig] = {"days": len(ics), "ic": ic_m, "t": ic_t,
                    "resid_ic": ric_m, "resid_t": ric_t}

    def _sig(name):
        return out.get(name, {})

    frgn, inst = _sig("frgn_stv"), _sig("inst_smc")
    verdict = {
        "bonferroni_t": BONF_T,
        # ①: 정합 정규화의 잔차 IC 가 양수이고 문턱 통과
        "frgn_stv_significant": bool(
            (frgn.get("resid_ic") or 0) > 0
            and abs(frgn.get("resid_t") or 0) >= BONF_T),
        "inst_smc_significant": bool(
            (inst.get("resid_ic") or 0) > 0
            and abs(inst.get("resid_t") or 0) >= BONF_T),
        # ②: 정합 분모가 원시·비정합 분모보다 큰가 (잔차 IC 기준)
        "frgn_matched_best": bool(
            (frgn.get("resid_ic") or -9) > (_sig("frgn_raw").get("resid_ic") or -9)
            and (frgn.get("resid_ic") or -9) > (_sig("frgn_smc").get("resid_ic") or -9)),
        "inst_matched_best": bool(
            (inst.get("resid_ic") or -9) > (_sig("inst_raw").get("resid_ic") or -9)
            and (inst.get("resid_ic") or -9) > (_sig("inst_stv").get("resid_ic") or -9)),
    }
    return {"ok": True, "n_rows": int(len(df)),
            "n_symbols": int(df["code"].nunique()),
            "d_range": [str(df["d"].min()), str(df["d"].max())],
            "signals": out, "verdict": verdict}


def run_once() -> dict:
    result = {
        "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
        **analyze(),
    }
    OUT_FILE.write_text(json.dumps(result, ensure_ascii=False, default=str),
                        encoding="utf-8")
    return {"ok": result.get("ok", False), "n_rows": result.get("n_rows", 0),
            "n_symbols": result.get("n_symbols", 0)}


def latest() -> dict:
    if not OUT_FILE.exists():
        return {"generated_at": None}
    try:
        return json.loads(OUT_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"generated_at": None}


async def backfill_task(days: int = 100) -> None:
    """API 가 띄우는 백그라운드 래퍼 — 예외를 삼키되 기록한다."""
    try:
        r = await backfill(days)
        log.info("수급 백필 완료: %s", r)
    except Exception:  # noqa: BLE001
        log.exception("수급 백필 실패")
