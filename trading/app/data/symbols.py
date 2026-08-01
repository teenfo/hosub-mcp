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


def parse_stock_list(raw: dict) -> list[dict]:
    """ka10099 응답에서 종목 배열 추출. 실제 응답은 배열 키 'list',
    필드 code/name (문서의 stk_infr/stk_cd/stk_nm 과 다름 — 실호출 검증됨).
    두 형식 모두 수용한다."""
    items = None
    for v in raw.values():
        if isinstance(v, list) and v and isinstance(v[0], dict) and (
            "code" in v[0] or "stk_cd" in v[0]
        ):
            items = v
            break
    out = []
    for it in items or []:
        code = str(it.get("code") or it.get("stk_cd") or "").lstrip("A_")
        name = it.get("name") or it.get("stk_nm") or it.get("list_nm") or ""
        if code.isdigit() and len(code) == 6:
            out.append({"code": code, "name": name})
    return out


async def refresh() -> int:
    """ka10099 전종목 리스트로 마스터를 갱신. 반환: 종목 수."""
    from ..kiwoom.client import client

    entries: list[dict] = []
    for mkt in ("0", "10"):  # 0=코스피, 10=코스닥
        try:
            entries += parse_stock_list(await client.stock_list(mkt))
        except Exception as e:  # noqa: BLE001
            log.warning("종목 마스터 갱신 실패 (mrkt=%s): %s", mkt, e)
    n = upsert(entries)
    log.info("종목 마스터 갱신: %d 종목", n)
    return n
