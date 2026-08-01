"""진입 시간대 분석 — '언제 사는가'가 승률·R 을 가르는가.

## 왜 재나

승률 개선 레버 탐색(2026-08-01)에서 "진입 시간대별 성적은 한 번도 잰 적이
없다"가 확인됐다. 재료는 이미 있다 — 실거래는 케이스 원장(cases)에 진입 시각·
손익·MFE/MAE 가 쌓여 있고, 합성 표본은 저장 1분봉 위에서 백테스트 러너가
진입 시각째 체결을 만들어 낸다.

시장 구조도 시간대 가설을 지지한다: 일중 거래량은 U자형(개장·마감 집중,
점심 급감)이고, 기관 실행 알고리즘(VWAP 계열)이 그 곡선에 비례해 물량을
집행하므로 **유동성·충격 비용·노이즈가 시간대마다 다르다**(기관 매매 기법
문서 2026-08-01). 얇은 시간대의 신호는 같은 규칙이라도 다른 성질일 수 있다.

## 판정 질문 (미리 못박는다)

  어느 진입 시간대(30분 버킷)의 승률·평균 R 이 다른 버킷과 유의하게 다른가.
  다르면 — 그 버킷의 신호를 차단(또는 축소)했을 때 전체 기대값이 오르는가.

주의: 버킷 13개의 다중 비교다. 눈에 띄는 버킷 하나로 결론 내지 않는다 —
본페로니(13개, α=0.05 → |t| 문턱 상향)와 표본 수를 함께 본다. 실거래 표본은
버킷당 수십 건이라 **방향 탐색용**이고, 판정은 합성 표본(수천 건)으로 한다.
두 표본의 결론이 어긋나면 그 사실 자체를 보고한다(합성은 시장가 체결·동시
1포지션 가정이라 실전과 다르다).

## 이 모듈은 관측·분석 전용이다

신호 차단·가중치 어디에도 자동 반영하지 않는다. 결과가 유의하면 제안까지만
하고, 규칙 변경은 사용자가 결정한다.
"""
import json
import logging
import multiprocessing as mp
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import settings

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
OUT_FILE = Path(settings.DATA_DIR) / "timeofday.json"

# 장중 30분 버킷 — 09:00~15:30, 마지막 버킷은 15:00~15:30
SESSION_START = 9 * 60          # 분 단위
SESSION_END = 15 * 60 + 30
BUCKET_MIN = 30


def _cfg() -> dict:
    return (settings.CONFIG.get("research", {}) or {}).get("timeofday", {})


def bucket_of(ts) -> str | None:
    """진입 시각 → 버킷 라벨("09:00" 등). 장외·파싱 불가면 None.

    naive 값은 KST 로 간주한다 — 원장(opened)과 분봉 인덱스가 KST 문자열이다.
    """
    if ts is None:
        return None
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            return None
    if getattr(ts, "tzinfo", None) is not None:
        ts = ts.astimezone(KST)
    minutes = ts.hour * 60 + ts.minute
    if not (SESSION_START <= minutes < SESSION_END):
        return None
    start = SESSION_START + ((minutes - SESSION_START) // BUCKET_MIN) * BUCKET_MIN
    return f"{start // 60:02d}:{start % 60:02d}"


def buckets() -> list[str]:
    return [f"{m // 60:02d}:{m % 60:02d}"
            for m in range(SESSION_START, SESSION_END, BUCKET_MIN)]


def _agg(rows: list[dict], val_key: str) -> dict:
    """버킷별 n·승률·평균값. rows: {"bucket", "win", val_key, ...}."""
    out: dict[str, dict] = {}
    for b in buckets():
        sub = [r for r in rows if r["bucket"] == b]
        if not sub:
            continue
        vals = [r[val_key] for r in sub if r.get(val_key) is not None]
        wins = len([r for r in sub if r["win"]])
        out[b] = {
            "n": len(sub),
            "win_rate": round(wins / len(sub) * 100, 1),
            f"avg_{val_key}": round(sum(vals) / len(vals), 4) if vals else None,
        }
    return out


# --------------------------------------------------------------------------
# 실거래 — 케이스 원장 (연구 DB). 표본이 작아 방향 탐색용.
# --------------------------------------------------------------------------
def live_stats() -> dict:
    from . import cases

    if not Path(cases.DB_PATH).exists():
        return {"n": 0}
    with sqlite3.connect(cases.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        try:
            recs = [dict(r) for r in conn.execute(
                "SELECT opened, rule, pnl_pct, pnl_krw, mfe_pct, mae_pct "
                "FROM cases WHERE pnl_krw IS NOT NULL")]
        except sqlite3.OperationalError:
            return {"n": 0}
    rows = []
    for r in recs:
        b = bucket_of(r.get("opened"))
        if not b:
            continue
        # 승패는 실손익 부호 — 대사 반영분은 pnl_krw 가 곧 실제다(ledger.outcome 규약)
        rows.append({"bucket": b, "win": (r["pnl_krw"] or 0) > 0,
                     "pnl_pct": r.get("pnl_pct"), "rule": r.get("rule"),
                     "mae_pct": r.get("mae_pct")})
    agg = _agg(rows, "pnl_pct")
    # MAE 도 같이 — "밀리기 직전에 들어간다"(케이스 원장 실측)가 특정 시간대에
    # 몰려 있는지 볼 수 있는 유일한 실거래 축이다
    for b, st in agg.items():
        maes = [r["mae_pct"] for r in rows if r["bucket"] == b
                and r.get("mae_pct") is not None]
        st["avg_mae_pct"] = round(sum(maes) / len(maes), 4) if maes else None
    return {"n": len(rows), "buckets": agg}


# --------------------------------------------------------------------------
# 합성 — 저장 1분봉 전 종목 백테스트. 판정용 큰 표본.
# --------------------------------------------------------------------------
def _symbol_trades(args: tuple[str, int]) -> list[dict]:
    """한 종목의 체결 목록 → 시각 버킷 행. **워커 진입점 — 모듈 최상위여야 한다.**"""
    symbol, max_bars = args
    from ..backtest import runner
    from ..data import store

    df = store.load_bars(symbol, "1m", limit=max_bars)
    if df.empty:
        return []
    sides = ("long",) if settings.RISK.get("long_only") else None
    costs = settings.COSTS
    out = []
    for t in runner.run(symbol, df, sides=sides).trades:
        if t.exit is None:
            continue
        b = bucket_of(t.entry_ts.to_pydatetime())
        if not b:
            continue
        r = t.r_multiple(costs)
        out.append({"bucket": b, "rule": t.rule, "win": r > 0, "r": round(r, 4),
                    "exit": t.exit_reason})
    return out


def synthetic_stats() -> dict:
    from ..backtest.report import _workers
    from ..data import store

    cfg = _cfg()
    max_bars = int(cfg.get("max_bars", 4500))
    min_days = int(cfg.get("min_days", 3))
    symbols = [s for s, _d in store.minute_symbols(min_days)]
    if not symbols:
        return {"n": 0, "symbols": 0}
    workers = _workers(int(cfg.get("workers", 0)))
    args = [(s, max_bars) for s in symbols]
    if workers <= 1 or len(args) <= 1:
        results = list(map(_symbol_trades, args))
    else:
        ctx = mp.get_context("spawn")   # fork 는 부모의 SQLite 핸들을 물려받는다
        with ctx.Pool(workers) as pool:
            results = pool.map(_symbol_trades, args, chunksize=4)
    rows = [r for sub in results for r in sub]
    agg = _agg(rows, "r")
    for b, st in agg.items():
        sub = [r for r in rows if r["bucket"] == b]
        # eod(시간 청산) 비중 — 추종 이득의 실체가 '끌던 것을 일찍 끊기'였다는
        # 실측(2026-07-30)과 이어 읽기 위한 축
        st["eod_share"] = round(
            len([r for r in sub if r["exit"] == "eod"]) / len(sub) * 100, 1)
    # 규칙 × 버킷 — 표본 있는 조합만 (규칙별 시간대 성격이 다를 수 있다)
    by_rule: dict[str, dict] = {}
    for rule in sorted({r["rule"] for r in rows}):
        rsub = [r for r in rows if r["rule"] == rule]
        by_rule[rule] = {b: st for b, st in _agg(rsub, "r").items()
                         if st["n"] >= int(cfg.get("min_cell", 30))}
    return {"n": len(rows), "symbols": len(symbols), "buckets": agg,
            "by_rule": by_rule}


def run_once() -> dict:
    """전체 분석 → timeofday.json. 무겁다(합성 = 전 종목 백테스트) — offload 로 돈다."""
    result = {
        "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "live": live_stats(),
        "synthetic": synthetic_stats(),
    }
    OUT_FILE.write_text(json.dumps(result, ensure_ascii=False, default=str),
                        encoding="utf-8")
    return {"ok": True, "live_n": result["live"].get("n", 0),
            "synthetic_n": result["synthetic"].get("n", 0),
            "symbols": result["synthetic"].get("symbols", 0)}


def latest() -> dict:
    if not OUT_FILE.exists():
        return {"generated_at": None}
    try:
        return json.loads(OUT_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"generated_at": None}
