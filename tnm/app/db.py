"""Postgres(psycopg3 async) 풀 + 마이그레이션 러너 + 리포지토리 함수.

- 상태는 전부 DB 컬럼으로 표현한다(파이프라인의 큐 = DB) — 재시작 내성.
- DB 미가동 시에도 서비스는 뜨고(ready=False), init_loop 가 복구를 재시도한다.
- 테스트는 실 DB 를 쓰지 않는다 — 이 모듈의 함수를 monkeypatch 한다.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from . import settings

log = logging.getLogger("tnm.db")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_pool = None          # psycopg_pool.AsyncConnectionPool | None
ready: bool = False
last_error: str = ""


async def init_once() -> bool:
    """풀 생성 + 마이그레이션. 성공 시 ready=True."""
    global _pool, ready, last_error
    if ready:
        return True
    if not settings.DB_DSN:
        last_error = "TNM_DB_DSN 미설정"
        return False
    try:
        from psycopg_pool import AsyncConnectionPool

        if _pool is None:
            _pool = AsyncConnectionPool(
                settings.DB_DSN, min_size=1, max_size=5, open=False,
                kwargs={"autocommit": True},
            )
            await _pool.open(wait=True, timeout=10)
        await _migrate()
        ready = True
        last_error = ""
        log.info("DB 준비 완료 (마이그레이션 적용됨)")
        return True
    except Exception as e:  # noqa: BLE001 — 다음 주기 재시도
        last_error = str(e)
        log.warning("DB 초기화 실패(재시도 예정): %s", e)
        # psycopg_pool 은 open 타임아웃 시 풀을 닫아버려 같은 객체 재사용이 불가 —
        # 실패한 풀은 폐기해 다음 재시도가 새 풀을 만들게 한다.
        if _pool is not None:
            try:
                await _pool.close()
            except Exception:  # noqa: BLE001
                pass
            _pool = None
        return False


async def init_loop() -> None:
    """DB 가 준비될 때까지 30초 주기 재시도 — 서비스 기동을 막지 않는다."""
    while True:
        if await init_once():
            return
        await asyncio.sleep(30)


async def close() -> None:
    global _pool, ready
    if _pool is not None:
        await _pool.close()
        _pool = None
    ready = False


async def _migrate() -> None:
    """migrations/NNN_*.sql 을 파일명 순서로 1회씩 적용 (멱등)."""
    async with _pool.connection() as conn:
        await conn.execute(
            "create table if not exists schema_migrations ("
            " version text primary key, applied_at timestamptz not null default now())"
        )
        cur = await conn.execute("select version from schema_migrations")
        applied = {r[0] for r in await cur.fetchall()}
        for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if f.name in applied:
                continue
            # 파라미터 없는 execute 는 simple protocol — 다중 문장 허용
            await conn.execute(f.read_text())
            await conn.execute(
                "insert into schema_migrations (version) values (%s)", (f.name,))
            log.info("마이그레이션 적용: %s", f.name)


# ---------------- 관심종목 리포지토리 ----------------

_WATCH_COLS = ("id, ticker, name, dart_corp_code, score_threshold, daily_alert_cap,"
               " is_active, origin, is_excluded, last_seen_at, created_at")


def _row_to_watch(r) -> dict:
    keys = [c.strip() for c in _WATCH_COLS.split(",")]
    d = dict(zip(keys, r))
    for ts in ("last_seen_at", "created_at"):
        if d.get(ts) is not None:
            d[ts] = d[ts].isoformat()
    return d


async def list_watch(include_inactive: bool = True) -> list[dict]:
    async with _pool.connection() as conn:
        q = f"select {_WATCH_COLS} from tnm_watchlist"
        if not include_inactive:
            q += " where is_active"
        cur = await conn.execute(q + " order by ticker")
        return [_row_to_watch(r) for r in await cur.fetchall()]


async def add_manual(ticker: str, name: str) -> dict:
    """수동 등록. 이미 있으면 활성화·제외해제하고, 소스에서 이미 사라진(비활성)
    행은 manual 로 승격한다 — 다음 동기화가 사용자의 명시적 등록을 되돌리지 않도록.
    소스에 살아있는 auto 행은 origin 을 유지한다(자동 추적 계속)."""
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "insert into tnm_watchlist (ticker, name, origin, score_threshold, daily_alert_cap)"
            " values (%s, %s, 'manual', %s, %s)"
            " on conflict (ticker) do update set is_active = true, is_excluded = false,"
            " origin = case when tnm_watchlist.is_active and not tnm_watchlist.is_excluded"
            "               then tnm_watchlist.origin else 'manual' end"
            f" returning {_WATCH_COLS}",
            (ticker, name,
             int(settings.ALERTS.get("default_threshold", 60)),
             int(settings.ALERTS.get("default_daily_cap", 5))))
        return _row_to_watch(await cur.fetchone())


async def set_excluded(ticker: str, excluded: bool) -> bool:
    """제외/복원. 제외하면 비활성화까지 — 동기화가 되살리지 않는다."""
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "update tnm_watchlist set is_excluded = %s, is_active = %s where ticker = %s",
            (excluded, not excluded, ticker))
        return cur.rowcount > 0


async def set_alert_settings(ticker: str, threshold: int | None,
                             daily_cap: int | None) -> bool:
    sets, args = [], []
    if threshold is not None:
        sets.append("score_threshold = %s"); args.append(int(threshold))
    if daily_cap is not None:
        sets.append("daily_alert_cap = %s"); args.append(int(daily_cap))
    if not sets:
        return False
    args.append(ticker)
    async with _pool.connection() as conn:
        cur = await conn.execute(
            f"update tnm_watchlist set {', '.join(sets)} where ticker = %s", args)
        return cur.rowcount > 0


async def apply_sync(auto: dict[str, tuple[str, str]],
                     ok_origins: set[str] | tuple[str, ...] = ("trading", "holding"),
                     ) -> dict:
    """자동 동기 반영. auto: ticker -> (name, origin['trading'|'holding']).

    병합 규칙 (계획 4절):
    - 신규 코드: insert(origin, active) — 단 기존 행이 있으면 manual/excluded 존중
    - 기존 auto 행: last_seen_at·origin 갱신·재활성화, 소스에서 사라진 auto 행은 비활성화
    - origin='manual' 행: 절대 수정하지 않음
    - is_excluded=true 행: 되살리지 않음 (UPDATE 의 WHERE 로도 이중 방어 —
      동기화 도중 사용자가 exclude 해도 덮어쓰지 않는다)
    - ok_origins: 이번 동기화에서 소스 조회에 성공한 origin 만 소멸 판정 —
      한쪽 소스(예: 계좌)가 실패하면 그쪽 기존 행은 건드리지 않는다.
    """
    inserted = revived = deactivated = 0
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "select ticker, origin, is_excluded, is_active from tnm_watchlist")
        rows = {r[0]: {"origin": r[1], "is_excluded": r[2], "is_active": r[3]}
                for r in await cur.fetchall()}
        for ticker, (name, origin) in auto.items():
            existing = rows.get(ticker)
            if existing is None:
                await conn.execute(
                    "insert into tnm_watchlist (ticker, name, origin, last_seen_at,"
                    " score_threshold, daily_alert_cap)"
                    " values (%s, %s, %s, now(), %s, %s) on conflict (ticker) do nothing",
                    (ticker, name, origin,
                     int(settings.ALERTS.get("default_threshold", 60)),
                     int(settings.ALERTS.get("default_daily_cap", 5))))
                inserted += 1
                continue
            if existing["origin"] == "manual" or existing["is_excluded"]:
                continue
            # WHERE 조건이 스냅샷-갱신 경합을 막는다: 동기화 도중 exclude/manual
            # 전환된 행은 이 UPDATE 가 건드리지 않는다 (0행 갱신).
            await conn.execute(
                "update tnm_watchlist set last_seen_at = now(), is_active = true,"
                " name = %s, origin = %s"
                " where ticker = %s and not is_excluded and origin <> 'manual'",
                (name, origin, ticker))
            if not existing["is_active"]:
                revived += 1
        # 조회에 성공한 소스(ok_origins)에서 사라진 auto 행만 비활성화
        gone = [t for t, r in rows.items()
                if r["origin"] in ok_origins and not r["is_excluded"]
                and r["is_active"] and t not in auto]
        if gone:
            await conn.execute(
                "update tnm_watchlist set is_active = false"
                " where ticker = any(%s) and not is_excluded and origin <> 'manual'",
                (gone,))
            deactivated = len(gone)
    return {"inserted": inserted, "revived": revived, "deactivated": deactivated,
            "total_auto": len(auto)}


async def fill_corp_codes(mapping: dict[str, str]) -> int:
    """corp_code 미보유 활성 행을 매핑으로 채운다. 반환: 채운 행 수."""
    filled = 0
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "select ticker from tnm_watchlist where is_active and dart_corp_code is null")
        for (ticker,) in await cur.fetchall():
            code = mapping.get(ticker)
            if code:
                await conn.execute(
                    "update tnm_watchlist set dart_corp_code = %s where ticker = %s",
                    (code, ticker))
                filled += 1
    return filled


async def queue_stats() -> dict:
    """상태 화면용 큐 적체량."""
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "select"
            " (select count(*) from tnm_raw_items where embedding is null) as embed_pending,"
            " (select count(*) from tnm_raw_items r where r.embedding is not null"
            "   and not exists (select 1 from tnm_analyses a where a.raw_item_id = r.id))"
            "   as classify_pending,"
            " (select count(*) from tnm_analyses where status = 'llm_failed') as llm_failed,"
            " (select count(*) from tnm_raw_items) as raw_total")
        r = await cur.fetchone()
        return {"embed_pending": r[0], "classify_pending": r[1],
                "llm_failed": r[2], "raw_total": r[3]}
