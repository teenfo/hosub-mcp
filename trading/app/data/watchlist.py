"""감시목록 영속화 (SQLite). config.yaml 의 watchlist 는 최초 1회 시드로만 쓰인다.

source 구분:
  seed   — config.yaml 초기 종목
  manual — **사람이 직접** 추가 (대시보드 입력·발굴 '감시 추가' 버튼)
  news   — TNM 뉴스·공시 자동 편입. manual 과 나눈 이유: seed/manual 은 정리
           경로가 건드리지 않는데, 사람이 넣은 적 없는 종목까지 그 보호를
           받으면 어떤 경로로도 지워지지 않는다(실측 42종목 영구 잔류)
  auto   — 야간 발굴이 자동 편입한 종목. 다음 발굴 때 새 상위 종목으로 교체된다.
  gainer — 급등률 상위 실시간 편입(스캔마다 전량 교체)
  active — 거래대금 상위 실시간 편입(스캔마다 전량 교체). '시장이 돈을 넣고 있는'
           종목이라 급등률 상위보다 유동성이 안전하다.
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


# `replace_auto` / `replace_scanned` / `replace_gainers` / `replace_active` 는
# 2026-08-01 완전 통합에서 **삭제**됐다. 감시목록 쓰기는 발굴 엔진(scout, full)의
# `apply_decisions` 경로(add/remove/set_mode)와 사용자 수동 조작만 남는다.
# 전량 교체(DELETE+INSERT) 계열이 만들던 문제들 — 60초마다 편입·이탈 반복,
# source 귀속 영구 고정, 조회 1회 실패 시 tier 증발 — 이 통로와 함께 사라졌다.
# 보유 종목 보호는 엔진의 protected(promote.plan)가 담당한다.


async def notify() -> None:
    if notifier is not None:
        try:
            await notifier()
        except Exception:  # noqa: BLE001 - 재구독 실패는 다음 재접속에서 복구
            log.exception("감시목록 변경 알림 실패")
