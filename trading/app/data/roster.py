"""수집 로스터 — 트레이딩 감시목록과 분리된 '분봉 수집 대상' 집합.

감시목록이 자동 발굴로 회전해도, 한 번이라도 감시된 종목은 유예기간 동안
로스터에 남아 분봉 백필을 계속 받는다. 그래서 나중에 그 종목을 다시 감시목록에
넣어도 축적 데이터에 큰 구멍이 생기지 않는다(백테스트 표본 연속성 확보).

last_watched: 마지막으로 감시목록에 있던 시각(UTC). 유예기간을 넘기면 로스터에서
제거되고, 과거 분봉은 store 의 일반 보관정책(keep_days)에 따라 정리된다.
"""
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .. import settings

DB_PATH = Path(settings.DATA_DIR) / "trading.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS collection_roster (
            code TEXT PRIMARY KEY, name TEXT, added TEXT, last_watched TEXT
        )"""
    )
    return conn


def touch(watchlist: dict[str, str]) -> None:
    """현재 감시목록 종목의 last_watched 를 now 로 갱신(신규는 추가)."""
    if not watchlist:
        return
    now = datetime.now(UTC).isoformat()
    with _conn() as conn:
        conn.executemany(
            "INSERT INTO collection_roster (code, name, added, last_watched) "
            "VALUES (?,?,?,?) ON CONFLICT(code) DO UPDATE SET "
            "last_watched=excluded.last_watched, name=excluded.name",
            [(c, n or c, now, now) for c, n in watchlist.items()],
        )


def active(retention_days: int) -> dict[str, str]:
    """유예기간 내(최근 retention_days 일 안에 감시된) 로스터 종목 {code: name}."""
    cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT code, name FROM collection_roster WHERE last_watched >= ? "
            "ORDER BY last_watched DESC",
            (cutoff,),
        ).fetchall()
    return {r["code"]: r["name"] for r in rows}


def entries() -> list[dict]:
    with _conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM collection_roster ORDER BY last_watched DESC"
        )]


def prune(retention_days: int) -> int:
    """유예기간을 넘긴 로스터 항목 제거(수집 중단). 반환: 삭제 수."""
    cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM collection_roster WHERE last_watched < ?", (cutoff,)
        )
        return cur.rowcount or 0
