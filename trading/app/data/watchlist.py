"""감시목록 영속화 (SQLite). config.yaml 의 watchlist 는 최초 1회 시드로만 쓰인다.

source 구분:
  seed   — config.yaml 초기 종목
  manual — 사용자가 직접 추가 (대시보드 입력·스캐너/발굴 '감시 추가' 버튼)
  auto   — 야간 발굴이 자동 편입한 종목. 다음 발굴 때 새 상위 종목으로 교체된다.
변경은 항상 settings.WATCHLIST(런타임)와 DB 에 함께 반영되고, main 이 등록한
notifier 코루틴으로 WS 재구독을 트리거한다.
"""
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from .. import settings

log = logging.getLogger(__name__)
DB_PATH = Path(settings.DATA_DIR) / "trading.db"

# main 이 설정: 감시목록 변경 후 호출할 코루틴 함수 (WS 재구독)
notifier: Callable[[], Awaitable[None]] | None = None


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS watchlist (
            code TEXT PRIMARY KEY, name TEXT, source TEXT, added TEXT
        )"""
    )
    # 수집전용 플래그(기존 DB 호환 — 없을 때만 추가). 1이면 데이터만 모으고 매매 제외.
    try:
        conn.execute("ALTER TABLE watchlist ADD COLUMN collect_only INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    return conn


def _rebuild_runtime(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT code, name, collect_only FROM watchlist ORDER BY added"
    ).fetchall()
    settings.WATCHLIST.clear()
    settings.WATCHLIST.update({r["code"]: r["name"] for r in rows})
    settings.COLLECT_ONLY.clear()
    settings.COLLECT_ONLY.update(r["code"] for r in rows if r["collect_only"])


def init() -> None:
    """앱 시작 시 호출. DB 가 비어 있으면 config.yaml 로 시드하고,
    이후에는 DB 를 단일 기준으로 settings.WATCHLIST 를 재구성한다."""
    with _conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM watchlist").fetchone()["c"]
        if count == 0 and settings.WATCHLIST:
            now = datetime.now(UTC).isoformat()
            conn.executemany(
                "INSERT OR IGNORE INTO watchlist (code, name, source, added) "
                "VALUES (?,?,?,?)",
                [(c, n, "seed", now) for c, n in settings.WATCHLIST.items()],
            )
        _rebuild_runtime(conn)
    log.info("감시목록 로드: %d 종목", len(settings.WATCHLIST))


def entries() -> list[dict]:
    with _conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM watchlist ORDER BY added"
        )]


def add(code: str, name: str, source: str = "manual") -> None:
    now = datetime.now(UTC).isoformat()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO watchlist (code, name, source, added) VALUES (?,?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET name=excluded.name",
            (code, name or code, source, now),
        )
        _rebuild_runtime(conn)


def set_mode(code: str, collect_only: bool) -> bool:
    """종목의 매매/수집전용 모드 전환. collect_only=True 면 데이터만 수집(매매 제외)."""
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE watchlist SET collect_only=? WHERE code=?",
            (1 if collect_only else 0, code),
        )
        _rebuild_runtime(conn)
    return bool(cur.rowcount)


def remove(code: str) -> bool:
    with _conn() as conn:
        cur = conn.execute("DELETE FROM watchlist WHERE code=?", (code,))
        _rebuild_runtime(conn)
    return bool(cur.rowcount)


def replace_auto(picks: list[dict]) -> None:
    """auto 항목을 새 발굴 상위로 교체. seed/manual 항목은 건드리지 않는다."""
    codes = [p["code"] for p in picks]
    now = datetime.now(UTC).isoformat()
    with _conn() as conn:
        if codes:
            ph = ",".join("?" * len(codes))
            conn.execute(
                f"DELETE FROM watchlist WHERE source='auto' AND code NOT IN ({ph})",
                codes,
            )
        else:
            conn.execute("DELETE FROM watchlist WHERE source='auto'")
        conn.executemany(
            "INSERT OR IGNORE INTO watchlist (code, name, source, added) "
            "VALUES (?,?,?,?)",
            [(p["code"], p.get("name") or p["code"], "auto", now) for p in picks],
        )
        _rebuild_runtime(conn)
    log.info("발굴 자동 편입: %s", codes)


def replace_gainers(picks: list[dict]) -> None:
    """KOSPI 급등주 자동편입 — source='gainer' 항목을 새 목록으로 교체한다.
    seed/manual/auto 로 이미 감시 중인 종목은 건드리지 않는다(중복 편입 방지).
    각 pick 의 collect_only 로 매매/수집전용 tier 를 지정(고가주는 수집전용)."""
    now = datetime.now(UTC).isoformat()
    with _conn() as conn:
        conn.execute("DELETE FROM watchlist WHERE source='gainer'")   # 전량 교체
        existing = {r["code"] for r in conn.execute("SELECT code FROM watchlist")}
        for p in picks:
            if p["code"] in existing:
                continue   # 이미 다른 소스로 감시 중 → 유지
            conn.execute(
                "INSERT INTO watchlist (code, name, source, added, collect_only) "
                "VALUES (?,?,?,?,?)",
                (p["code"], p.get("name") or p["code"], "gainer", now,
                 1 if p.get("collect_only") else 0),
            )
        _rebuild_runtime(conn)
    log.info("급등주 자동편입: %d종목", len(picks))


async def notify() -> None:
    if notifier is not None:
        try:
            await notifier()
        except Exception:  # noqa: BLE001 - 재구독 실패는 다음 재접속에서 복구
            log.exception("감시목록 변경 알림 실패")
