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
