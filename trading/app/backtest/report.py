"""분봉 축적분에 대한 주기적 백테스트 리포트.

장 마감 후 평일 1회, 분봉이 일정 일수 이상 쌓인 종목 전체를 비용 반영
백테스트하고 결과 스냅샷을 SQLite 에 남긴다. 스냅샷을 날짜별로 보관하므로
승률·손익비 추이(워크포워드적 관찰)를 볼 수 있다.

딥리서치 원칙: 진입기법보다 청산·비용, 그리고 '내 데이터로 검증'. 이 리포트는
규칙의 절대 수익을 약속하지 않으며, 명백히 나쁜 규칙을 걸러내고 exit·비용
민감도를 관찰하는 용도다.
"""
import asyncio
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import settings
from ..data import store
from . import runner

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
DB_PATH = Path(settings.DATA_DIR) / "backtest.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS runs (
            run_ts TEXT NOT NULL, symbol TEXT NOT NULL, days INTEGER,
            trades INTEGER, win_rate REAL, avg_pnl_pct REAL, profit_factor REAL,
            total_return_pct REAL, max_drawdown_pct REAL, by_rule TEXT,
            PRIMARY KEY (run_ts, symbol)
        )"""
    )
    return conn


def aggregate(rows: list[dict]) -> dict:
    """종목별 stats 리스트 → 전체 요약(체결수 가중). 순수 함수(테스트 용이)."""
    active = [r for r in rows if r.get("trades")]
    total_trades = sum(r["trades"] for r in active)
    if not total_trades:
        return {"symbols": len(rows), "with_trades": 0, "trades": 0}
    # 승률·평균손익은 체결수 가중 평균, 규칙별 평균손익은 단순 병합
    win = sum(r["win_rate"] * r["trades"] for r in active) / total_trades
    avg = sum(r["avg_pnl_pct"] * r["trades"] for r in active) / total_trades
    by_rule: dict[str, list] = {}
    for r in active:
        for k, v in (r.get("by_rule") or {}).items():
            by_rule.setdefault(k, []).append(v)
    return {
        "symbols": len(rows),
        "with_trades": len(active),
        "trades": total_trades,
        "win_rate": round(win, 1),
        "avg_pnl_pct": round(avg, 3),
        "by_rule": {k: round(sum(v) / len(v), 3) for k, v in by_rule.items()},
    }


class BacktestReporter:
    def __init__(self) -> None:
        self.running = False
        self.last_run = ""

    def run_once(self, min_days: int | None = None, keep_days: int | None = None) -> dict:
        cfg = settings.CONFIG.get("backtest", {})
        min_days = min_days if min_days is not None else cfg.get("min_days", 3)
        keep_days = keep_days if keep_days is not None else cfg.get("keep_days", 120)
        if self.running:
            return {"ok": False, "error": "이미 실행 중"}
        self.running = True
        try:
            store.prune_minutes(keep_days)
            universe = store.minute_symbols(min_days)
            run_ts = datetime.now(KST).isoformat(timespec="seconds")
            rows: list[dict] = []
            for symbol, days in universe:
                df = store.load_bars(symbol, "1m", limit=200000)
                if df.empty:
                    continue
                sides = ("long",) if settings.RISK.get("long_only") else None
                st = runner.run(symbol, df, sides=sides).stats()
                rows.append({"symbol": symbol, "days": days, **st})
            with _conn() as conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [(run_ts, r["symbol"], r["days"], r.get("trades", 0),
                      r.get("win_rate"), r.get("avg_pnl_pct"), r.get("profit_factor"),
                      r.get("total_return_pct"), r.get("max_drawdown_pct"),
                      json.dumps(r.get("by_rule", {}), ensure_ascii=False)) for r in rows],
                )
            self.last_run = run_ts
            summary = aggregate(rows)
            log.info("백테스트 리포트 %s: %s종목/%s체결", run_ts,
                     summary.get("symbols"), summary.get("trades"))
            return {"ok": True, "run_ts": run_ts, "summary": summary, "symbols": rows}
        finally:
            self.running = False

    def latest(self) -> dict:
        with _conn() as conn:
            row = conn.execute("SELECT MAX(run_ts) AS t FROM runs").fetchone()
            run_ts = row["t"] if row else None
            if not run_ts:
                return {"run_ts": None, "summary": {}, "symbols": [],
                        "last_run": self.last_run, "running": self.running}
            recs = [dict(r) | {"by_rule": json.loads(r["by_rule"] or "{}")}
                    for r in conn.execute(
                        "SELECT * FROM runs WHERE run_ts=? ORDER BY trades DESC, symbol",
                        (run_ts,))]
        return {"run_ts": run_ts, "summary": aggregate(recs), "symbols": recs,
                "last_run": self.last_run, "running": self.running}

    def history(self, limit: int = 20) -> list[dict]:
        """실행별 전체 요약 추이 (최신순)."""
        with _conn() as conn:
            tss = [r["t"] for r in conn.execute(
                "SELECT DISTINCT run_ts AS t FROM runs ORDER BY run_ts DESC LIMIT ?",
                (limit,))]
            out = []
            for ts in tss:
                recs = [dict(r) | {"by_rule": json.loads(r["by_rule"] or "{}")}
                        for r in conn.execute("SELECT * FROM runs WHERE run_ts=?", (ts,))]
                out.append({"run_ts": ts, **aggregate(recs)})
        return out

    async def loop(self) -> None:
        """평일 장 마감 후(run_after) 1회. 재시작해도 당일 중복 실행 안 함."""
        done_for = ""
        while True:
            try:
                cfg = settings.CONFIG.get("backtest", {})
                now = datetime.now(KST)
                today = now.date().isoformat()
                if (
                    cfg.get("report_enabled", True)
                    and now.weekday() < 5
                    and now.strftime("%H:%M") >= cfg.get("run_after", "15:40")
                    and done_for != today
                ):
                    await asyncio.to_thread(self.run_once)
                    done_for = today
            except Exception:  # noqa: BLE001
                log.exception("백테스트 리포트 오류")
            await asyncio.sleep(300)
