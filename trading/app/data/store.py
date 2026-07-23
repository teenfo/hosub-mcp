"""SQLite 시세·주문 저장소. 홈서버에서는 DATA_DIR=/data/trading 권장."""
import sqlite3
from pathlib import Path

import pandas as pd

from .. import settings

DB_PATH = Path(settings.DATA_DIR) / "market.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS bars (
            symbol TEXT NOT NULL,
            tf TEXT NOT NULL,          -- '1m', '1d'
            ts TEXT NOT NULL,          -- ISO8601 (KST)
            open REAL, high REAL, low REAL, close REAL, volume INTEGER,
            PRIMARY KEY (symbol, tf, ts)
        )"""
    )
    return conn


def upsert_bars(symbol: str, tf: str, df: pd.DataFrame) -> int:
    """df: index=ts(datetime), columns=open/high/low/close/volume."""
    if df.empty:
        return 0
    rows = [
        (symbol, tf, ts.isoformat(), float(r.open), float(r.high), float(r.low),
         float(r.close), int(r.volume))
        for ts, r in df.iterrows()
    ]
    with _conn() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?)", rows
        )
    return len(rows)


def minute_symbols(min_days: int = 1) -> list[tuple[str, int]]:
    """분봉이 min_days 일 이상 축적된 종목 목록 → [(symbol, 축적일수)].
    ts 는 ISO 문자열이라 날짜는 substr(1,10)='YYYY-MM-DD' 로 센다."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT symbol, COUNT(DISTINCT substr(ts,1,10)) d FROM bars "
            "WHERE tf='1m' GROUP BY symbol HAVING d>=? ORDER BY d DESC, symbol",
            (min_days,),
        ).fetchall()
    return [(r[0], int(r[1])) for r in rows]


def prune_minutes(keep_days: int) -> int:
    """가장 최근 분봉 날짜 기준 keep_days 를 넘긴 오래된 분봉을 삭제(디스크 방어)."""
    if keep_days <= 0:
        return 0
    from datetime import date, timedelta

    with _conn() as conn:
        row = conn.execute(
            "SELECT MAX(substr(ts,1,10)) FROM bars WHERE tf='1m'"
        ).fetchone()
        if not row or not row[0]:
            return 0
        y, m, d = (int(x) for x in row[0].split("-"))
        cutoff = (date(y, m, d) - timedelta(days=keep_days)).isoformat()
        cur = conn.execute(
            "DELETE FROM bars WHERE tf='1m' AND substr(ts,1,10) < ?", (cutoff,)
        )
        return cur.rowcount or 0


def load_bars(symbol: str, tf: str = "1m", limit: int = 2000) -> pd.DataFrame:
    with _conn() as conn:
        df = pd.read_sql_query(
            "SELECT ts, open, high, low, close, volume FROM bars "
            "WHERE symbol=? AND tf=? ORDER BY ts DESC LIMIT ?",
            conn,
            params=(symbol, tf, limit),
        )
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"])
    return df.set_index("ts").sort_index()
