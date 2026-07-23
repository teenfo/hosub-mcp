"""종목 마스터 (코드↔명) — 종목명으로 감시목록에 추가할 수 있게 한다.

키움엔 종목명 검색 전용 API 가 없어, 전종목 리스트(ka10099)를 받아 로컬에
캐시하고 이름/코드로 조회한다. 야간 발굴이 매일 갱신하며, 비어 있으면
추가 요청 시 지연 갱신한다.
"""
import logging
import sqlite3
from pathlib import Path

from .. import settings

log = logging.getLogger(__name__)
DB_PATH = Path(settings.DATA_DIR) / "trading.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS symbol_master (code TEXT PRIMARY KEY, name TEXT)"
    )
    return conn


def upsert(entries: list[dict]) -> int:
    rows = [
        (e["code"], e.get("name") or e["code"])
        for e in entries
        if str(e.get("code", "")).isdigit() and len(str(e["code"])) == 6
    ]
    if not rows:
        return 0
    with _conn() as conn:
        conn.executemany(
            "INSERT INTO symbol_master VALUES (?,?) "
            "ON CONFLICT(code) DO UPDATE SET name=excluded.name",
            rows,
        )
    return len(rows)


def count() -> int:
    with _conn() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM symbol_master").fetchone()["c"]


def name_of(code: str) -> str | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT name FROM symbol_master WHERE code=?", (code,)
        ).fetchone()
    return row["name"] if row else None


def resolve(query: str, limit: int = 20) -> list[dict]:
    """코드(6자리) 또는 종목명으로 후보를 찾는다.
    이름은 공백 무시 완전일치 우선, 없으면 부분일치."""
    q = query.strip()
    with _conn() as conn:
        if q.isdigit() and len(q) == 6:
            row = conn.execute(
                "SELECT code, name FROM symbol_master WHERE code=?", (q,)
            ).fetchone()
            return [dict(row)] if row else [{"code": q, "name": q}]
        norm = q.replace(" ", "")
        exact = conn.execute(
            "SELECT code, name FROM symbol_master "
            "WHERE REPLACE(name,' ','')=? COLLATE NOCASE",
            (norm,),
        ).fetchall()
        if exact:
            return [dict(r) for r in exact]
        like = conn.execute(
            "SELECT code, name FROM symbol_master "
            "WHERE REPLACE(name,' ','') LIKE ? COLLATE NOCASE "
            "ORDER BY LENGTH(name) LIMIT ?",
            (f"%{norm}%", limit),
        ).fetchall()
    return [dict(r) for r in like]


async def refresh() -> int:
    """ka10099 전종목 리스트로 마스터를 갱신. 반환: 종목 수."""
    from ..discovery import parse_stock_list  # 지연 임포트 (순환 방지)
    from ..kiwoom.client import client

    try:
        raw = await client.stock_list("000")  # 000 = 전체(코스피+코스닥)
    except Exception as e:  # noqa: BLE001
        log.warning("종목 마스터 갱신 실패: %s", e)
        return 0
    entries = parse_stock_list(raw)
    n = upsert(entries)
    log.info("종목 마스터 갱신: %d 종목", n)
    return n
