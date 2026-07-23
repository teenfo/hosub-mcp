"""실거래 성과 로그 — 승인·발주된 주문의 체결/청산/실현손익을 추적한다.

딥리서치 검증의 가장 강한 긍정 발견은 '실력의 지속성은 실재한다'였다. 그걸
확인하는 유일한 방법이 내 실제 체결을 비용 포함해 측정하는 것이므로, 제안·발주
상태만 남기던 orders 에 더해 여기서 **진입가·청산가·실현손익·슬리피지**를 남긴다.

포지션은 신호 종목의 가격 흐름으로 추적한다(숏은 인버스 ETF로 집행하지만
손익은 백테스터와 동일하게 '신호 종목의 역방향 수익률'로 근사 — 일관성 유지).
장중 30초 주기로 손절/목표 터치를 확인하고, 장 마감에 미청산분을 종가로 정리한다.
"""
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import settings
from ..data import store

KST = ZoneInfo("Asia/Seoul")
DB_PATH = Path(settings.DATA_DIR) / "trading.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS positions (
            id TEXT PRIMARY KEY,
            opened TEXT NOT NULL, symbol TEXT, name TEXT, rule TEXT, side TEXT,
            qty INTEGER, model_entry REAL, entry REAL, stop REAL, target REAL,
            closed TEXT, exit REAL, exit_reason TEXT,
            pnl_pct REAL, pnl_krw REAL, slippage_pct REAL,
            status TEXT NOT NULL          -- open / closed
        )"""
    )
    return conn


def latest_price(symbol: str) -> float | None:
    """최근 1분봉 종가(체결가 근사)."""
    df = store.load_bars(symbol, "1m", limit=1)
    return None if df.empty else float(df["close"].iloc[-1])


def _net_pnl_pct(side: str, entry: float, exit_px: float) -> float:
    """비용(수수료 왕복·거래세·슬리피지) 반영 실현손익률. 백테스터와 동일 공식."""
    c = settings.COSTS
    raw = (exit_px - entry) / entry * 100
    if side == "short":
        raw = -raw
    return raw - c.get("commission_pct", 0.015) * 2 - c.get("sell_tax_pct", 0.15) \
        - c.get("slippage_bp", 5) / 100 * 2


def open_position(order: dict, fill: float | None = None) -> None:
    """발주 성공 주문을 오픈 포지션으로 기록. fill 미지정 시 최신가→모델가 순."""
    symbol = order["symbol"]
    model_entry = float(order["entry"])
    entry = float(fill if fill else (latest_price(symbol) or model_entry))
    name = settings.WATCHLIST.get(symbol) or symbol
    slippage = (entry - model_entry) / model_entry * 100 if model_entry else 0.0
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO positions VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order["id"], datetime.now(KST).isoformat(timespec="seconds"),
             symbol, name, order["rule"], order["side"], int(order["qty"]),
             model_entry, entry, float(order["stop"]), float(order["target"]),
             None, None, None, None, None, round(slippage, 4), "open"),
        )


def _close(conn: sqlite3.Connection, row: sqlite3.Row, exit_px: float, reason: str) -> None:
    net = _net_pnl_pct(row["side"], row["entry"], exit_px)
    pnl_krw = row["qty"] * row["entry"] * net / 100
    conn.execute(
        "UPDATE positions SET closed=?, exit=?, exit_reason=?, pnl_pct=?, pnl_krw=?, "
        "status='closed' WHERE id=?",
        (datetime.now(KST).isoformat(timespec="seconds"), round(exit_px, 2), reason,
         round(net, 4), round(pnl_krw, 1), row["id"]),
    )


def close_position(pos_id: str, exit_px: float, reason: str = "manual") -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM positions WHERE id=? AND status='open'", (pos_id,)
        ).fetchone()
        if not row:
            return False
        _close(conn, row, exit_px, reason)
    return True


def monitor(price_of) -> int:
    """오픈 포지션의 손절/목표 터치를 확인해 청산. price_of(symbol)->float|None.
    반환: 이번에 청산된 건수."""
    closed = 0
    with _conn() as conn:
        for row in conn.execute("SELECT * FROM positions WHERE status='open'").fetchall():
            p = price_of(row["symbol"])
            if p is None:
                continue
            if row["side"] == "long":
                hit = "stop" if p <= row["stop"] else ("target" if p >= row["target"] else None)
                px = row["stop"] if hit == "stop" else row["target"]
            else:
                hit = "stop" if p >= row["stop"] else ("target" if p <= row["target"] else None)
                px = row["stop"] if hit == "stop" else row["target"]
            if hit:
                _close(conn, row, float(px), hit)
                closed += 1
    return closed


def force_close_eod(price_of) -> int:
    """장 마감 미청산 포지션을 현재가로 정리(reason=eod)."""
    closed = 0
    with _conn() as conn:
        for row in conn.execute("SELECT * FROM positions WHERE status='open'").fetchall():
            p = price_of(row["symbol"]) or row["entry"]
            _close(conn, row, float(p), "eod")
            closed += 1
    return closed


def positions(status: str | None = None, limit: int = 100) -> list[dict]:
    with _conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM positions WHERE status=? ORDER BY opened DESC LIMIT ?",
                (status, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM positions ORDER BY opened DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def _agg(rows: list[sqlite3.Row]) -> dict:
    if not rows:
        return {"trades": 0}
    pnls = [r["pnl_pct"] for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    return {
        "trades": len(rows),
        "win_rate": round(len(wins) / len(rows) * 100, 1),
        "expectancy_pct": round(sum(pnls) / len(pnls), 3),   # 건당 기대값
        "total_pnl_krw": round(sum(r["pnl_krw"] for r in rows), 0),
        "profit_factor": round(sum(wins) / abs(sum(losses)), 2)
        if losses and sum(losses) != 0 else (float("inf") if wins else 0.0),
        "avg_slippage_pct": round(
            sum(r["slippage_pct"] or 0 for r in rows) / len(rows), 4),
    }


def stats() -> dict:
    """청산 완료 포지션 집계: 전체 + 규칙별. 실현손익·기대값·슬리피지."""
    with _conn() as conn:
        closed = conn.execute("SELECT * FROM positions WHERE status='closed'").fetchall()
        open_n = conn.execute(
            "SELECT COUNT(*) c FROM positions WHERE status='open'").fetchone()["c"]
    by_rule = {}
    for r in {row["rule"] for row in closed}:
        by_rule[r] = _agg([row for row in closed if row["rule"] == r])
    return {"overall": _agg(closed), "by_rule": by_rule, "open_count": int(open_n)}
