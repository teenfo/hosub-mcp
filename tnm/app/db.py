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


async def add_discovered(rows: list[dict], tier: str = "other",
                         origin: str = "dart") -> int:
    """발굴 종목을 관심종목에 등록한다.

    `origin` 은 어느 소스가 올렸는지다(기본 'dart'). 소스마다 다르게 둬야
    출처별 상한(count_origin)과 성적을 따로 잴 수 있다.

    `add_manual` 과 다른 점 셋:

    1. **origin='dart'** — 수동 등록과 구분해야 발굴 경로의 성적을 잴 수 있다.
       watchsync 는 origin in ('trading','holding') 만 비활성화하므로 dart 행은
       살아남는다(사라지지 않는다는 뜻이지, trading 감시목록에 나중에 나타나면
       origin 은 trading 으로 덮인다).
    2. **tier 기본값이 'other'** — 기본 'trade' 로 넣으면 종목마다 30분 주기
       RSS 폴링이 붙어 발굴할수록 호출이 선형으로 늘어난다. 발굴 종목은
       12시간 주기로 시작하고, 승격되면 trading 쪽에서 tier 가 올라온다.
    3. **dart_corp_code 를 넣는다** — 이게 비어 있으면 다음 사이클의 공시
       매칭 대상(watch_by_corp_code)에 안 들어가서, 정작 발굴의 근거였던
       공시가 수집되지 않는다. watchsync 의 fill_corp_codes 를 기다리면
       최대 30분이 비는데 그 사이 공시는 커서를 지나가 버린다.

    이미 있는 ticker 는 건드리지 않는다 — 제외 처리한 종목을 발굴이 되살리면
    사용자 의도를 덮는 것이다.
    """
    if not rows:
        return 0
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "insert into tnm_watchlist (ticker, name, origin, tier, dart_corp_code,"
            " score_threshold, daily_alert_cap)"
            " select * from unnest(%s::text[], %s::text[], %s::text[], %s::text[],"
            "                      %s::text[], %s::int[], %s::int[])"
            " on conflict (ticker) do nothing",
            ([r["ticker"] for r in rows],
             [r.get("name") or r["ticker"] for r in rows],
             [origin] * len(rows),
             [tier] * len(rows),
             [r.get("corp_code") or None for r in rows],
             [int(settings.ALERTS.get("default_threshold", 60))] * len(rows),
             [int(settings.ALERTS.get("default_daily_cap", 5))] * len(rows)))
        return cur.rowcount or 0


async def count_origin(origin: str) -> int:
    """해당 출처의 활성 관심종목 수 — 발굴 상한 판정에 쓴다."""
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "select count(*) from tnm_watchlist"
            " where origin = %s and is_active and not is_excluded", (origin,))
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def excluded_tickers() -> set[str]:
    """사용자가 제외한 종목 — 어떤 소스도 되살리면 안 된다.

    `known_tickers` 와 구분해야 한다. 비활성(is_active=false)은 매매 감시목록에서
    빠지며 **자동으로** 된 것이고 사용자의 결정이 아니다. 둘을 같이 취급하면
    한 번 스쳐간 종목은 영원히 다시 못 들어온다.
    """
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "select ticker from tnm_watchlist where is_excluded")
        return {r[0] for r in await cur.fetchall()}


async def revive_for_source(tickers: list[str], tier: str, origin: str) -> int:
    """비활성 종목을 되살려 이 소스가 소유하게 한다. 제외 종목은 건드리지 않는다.

    origin 을 바꾸는 것이 핵심이다. watchsync 는 origin in ('trading','holding')
    행만 비활성화하므로(sync_watch 의 `gone`), origin 을 그대로 두면 30분마다
    **되살리기와 비활성화가 핑퐁**한다. 트레이딩이 놓은 종목을 리서치가 되살렸으면
    그 시점부터 소유자는 리서치다 — add_discovered 가 origin='dart' 행을
    살려 두는 것과 같은 규약.
    """
    if not tickers:
        return 0
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "update tnm_watchlist set is_active = true, tier = %s, origin = %s"
            " where ticker = any(%s) and not is_active and not is_excluded",
            (tier, origin, tickers))
        return max(cur.rowcount, 0)


async def known_tickers() -> set[str]:
    """이미 등록된 ticker 전부(제외·비활성 포함) — 발굴이 되살리지 않게."""
    async with _pool.connection() as conn:
        cur = await conn.execute("select ticker from tnm_watchlist")
        return {r[0] for r in await cur.fetchall()}


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


async def apply_sync(auto: dict[str, tuple[str, ...]],
                     ok_origins: set[str] | tuple[str, ...] = ("trading", "holding"),
                     ) -> dict:
    """자동 동기 반영. auto: ticker -> (name, origin['trading'|'holding'], tier).

    tier 는 뉴스 수집 주기를 가른다(매매 대상은 자주, 수집전용은 드물게).
    생략하면 'trade' — 기존 호출부 호환.

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
        for ticker, vals in auto.items():
            name, origin = vals[0], vals[1]
            tier = vals[2] if len(vals) > 2 else "trade"
            existing = rows.get(ticker)
            if existing is None:
                await conn.execute(
                    "insert into tnm_watchlist (ticker, name, origin, last_seen_at,"
                    " score_threshold, daily_alert_cap, tier)"
                    " values (%s, %s, %s, now(), %s, %s, %s)"
                    " on conflict (ticker) do nothing",
                    (ticker, name, origin,
                     int(settings.ALERTS.get("default_threshold", 60)),
                     int(settings.ALERTS.get("default_daily_cap", 5)), tier))
                inserted += 1
                continue
            if existing["origin"] == "manual" or existing["is_excluded"]:
                continue
            # WHERE 조건이 스냅샷-갱신 경합을 막는다: 동기화 도중 exclude/manual
            # 전환된 행은 이 UPDATE 가 건드리지 않는다 (0행 갱신).
            await conn.execute(
                "update tnm_watchlist set last_seen_at = now(), is_active = true,"
                " name = %s, origin = %s, tier = %s"
                " where ticker = %s and not is_excluded and origin <> 'manual'",
                (name, origin, tier, ticker))
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


# ---------------- 수집 (raw_items · 커서) ----------------

async def active_watch_for_collect() -> list[dict]:
    """수집 대상 활성 종목 + 소스별 증분 커서."""
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "select id, ticker, name, dart_corp_code, last_collected_at, tier,"
            " last_attempt from tnm_watchlist"
            " where is_active and not is_excluded order by ticker")
        return [{"id": r[0], "ticker": r[1], "name": r[2], "dart_corp_code": r[3],
                 "last_collected_at": r[4] or {}, "tier": r[5] or "trade",
                 "last_attempt": r[6] or {}} for r in await cur.fetchall()]


async def get_state(key: str) -> str | None:
    """전역 수집 상태(종목에 매이지 않는 커서). DART 전종목 모드가 쓴다."""
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "select value from tnm_collect_state where key = %s", (key,))
        row = await cur.fetchone()
        return row[0] if row else None


async def set_state(key: str, value: str) -> None:
    async with _pool.connection() as conn:
        await conn.execute(
            "insert into tnm_collect_state (key, value) values (%s, %s)"
            " on conflict (key) do update set value = excluded.value,"
            " updated_at = now()", (key, value))


async def watch_by_corp_code() -> dict[str, dict]:
    """corp_code → 감시종목. 전종목 공시를 우리 종목으로 되돌리는 데 쓴다."""
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "select id, ticker, name, dart_corp_code from tnm_watchlist"
            " where is_active and not is_excluded and dart_corp_code is not null")
        return {r[3]: {"id": r[0], "ticker": r[1], "name": r[2]}
                for r in await cur.fetchall()}


async def mark_attempt(tickers: list[str], source: str) -> None:
    """소스별 '마지막 시도 시각' 기록 — 계층별 주기 판정에 쓴다.
    커서(last_collected_at)와 분리한다: 새 글이 없어도 시도는 있었다."""
    if not tickers:
        return
    async with _pool.connection() as conn:
        await conn.execute(
            "update tnm_watchlist set last_attempt ="
            " last_attempt || jsonb_build_object(%s::text, now()::text)"
            " where ticker = any(%s)", (source, tickers))


async def set_cursor(ticker: str, source: str, cursor: str) -> None:
    """소스별 증분 커서 갱신 — last_collected_at(jsonb) 에 병합."""
    async with _pool.connection() as conn:
        await conn.execute(
            "update tnm_watchlist set last_collected_at ="
            " last_collected_at || jsonb_build_object(%s::text, %s::text)"
            " where ticker = %s", (source, cursor, ticker))


async def insert_raw_items(watchlist_id: int, source: str, rows: list[dict]) -> int:
    """원문 적재. rows: [{source_uid,title,body,url,published_at,content_hash,norm_body}].

    멱등성 2중: (source,source_uid) 충돌 무시 + 동일 종목 내 동일 content_hash
    (완전중복 — FR-04) 기존재 시 스킵. 반환: 실제 삽입 수.
    """
    inserted = 0
    async with _pool.connection() as conn:
        for d in rows:
            cur = await conn.execute(
                "insert into tnm_raw_items (watchlist_id, source, source_uid, title,"
                " body, url, published_at, content_hash, norm_body)"
                " select %s, %s, %s, %s, %s, %s, %s, %s, %s"
                " where not exists (select 1 from tnm_raw_items"
                "   where watchlist_id = %s and content_hash = %s)"
                " on conflict (source, source_uid) do nothing",
                (watchlist_id, source, d["source_uid"], d["title"], d.get("body"),
                 d["url"], d["published_at"], d["content_hash"], d.get("norm_body"),
                 watchlist_id, d["content_hash"]))
            inserted += max(cur.rowcount, 0)
    return inserted


async def watch_ids_by_ticker() -> dict[str, int]:
    """수집 대상 종목의 ticker → watchlist_id. 리포트를 raw_items 로 잇는 데 쓴다."""
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "select ticker, id from tnm_watchlist"
            " where is_active and not is_excluded")
        return {r[0]: r[1] for r in await cur.fetchall()}


# ---------------- 증권사 · 리서치 리포트 ----------------

_BROKER_COLS = ("id, name, kind, enabled, aliases, note, reports,"
                " last_seen_at, created_at")


def _row_to_broker(r) -> dict:
    keys = [c.strip() for c in _BROKER_COLS.split(",")]
    d = dict(zip(keys, r))
    d["aliases"] = list(d.get("aliases") or [])
    for ts in ("last_seen_at", "created_at"):
        if d.get(ts) is not None:
            d[ts] = d[ts].isoformat()
    return d


async def list_brokers(enabled_only: bool = False) -> list[dict]:
    async with _pool.connection() as conn:
        q = f"select {_BROKER_COLS} from tnm_brokers"
        if enabled_only:
            q += " where enabled"
        cur = await conn.execute(q + " order by kind, name")
        return [_row_to_broker(r) for r in await cur.fetchall()]


async def seed_brokers(rows: list[dict]) -> int:
    """시드 등록 — 이미 있는 이름은 **건드리지 않는다**.

    사용자가 끈 증권사를 배포 때마다 시드가 다시 켜면 토글이 무의미해진다.
    """
    if not rows:
        return 0
    # 행마다 별칭 개수가 달라 unnest 로 묶을 수 없다(Postgres 배열은 각 행의
    # 길이가 같아야 한다). 기동 시 1회 수십 행이라 반복 INSERT 로 충분하다.
    added = 0
    async with _pool.connection() as conn:
        for r in rows:
            cur = await conn.execute(
                "insert into tnm_brokers (name, kind, aliases, note)"
                " values (%s, %s, %s::text[], %s) on conflict (name) do nothing",
                (r["name"], r.get("kind") or "domestic",
                 list(r.get("aliases") or []), r.get("note")))
            added += max(cur.rowcount, 0)
    return added


async def add_broker(name: str, kind: str = "domestic",
                     aliases: list[str] | None = None,
                     note: str | None = None) -> dict:
    """수동 추가. 이미 있으면 활성화하고 별칭을 합친다(덮어쓰지 않는다)."""
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "insert into tnm_brokers (name, kind, aliases, note)"
            " values (%s, %s, %s::text[], %s)"
            " on conflict (name) do update set enabled = true,"
            "   aliases = (select array_agg(distinct a) from unnest("
            "     tnm_brokers.aliases || excluded.aliases) a),"
            "   note = coalesce(excluded.note, tnm_brokers.note)"
            f" returning {_BROKER_COLS}",
            (name, kind, list(aliases or []), note))
        return _row_to_broker(await cur.fetchone())


async def set_broker(name: str, *, enabled: bool | None = None,
                     kind: str | None = None,
                     aliases: list[str] | None = None) -> bool:
    sets, args = [], []
    if enabled is not None:
        sets.append("enabled = %s")
        args.append(enabled)
    if kind is not None:
        sets.append("kind = %s")
        args.append(kind)
    if aliases is not None:
        sets.append("aliases = %s::text[]")
        args.append(list(aliases))
    if not sets:
        return False
    args.append(name)
    async with _pool.connection() as conn:
        cur = await conn.execute(
            f"update tnm_brokers set {', '.join(sets)} where name = %s", tuple(args))
        return cur.rowcount > 0


async def delete_broker(name: str) -> bool:
    async with _pool.connection() as conn:
        cur = await conn.execute("delete from tnm_brokers where name = %s", (name,))
        return cur.rowcount > 0


async def known_report_keys(keys: list[tuple[str, str]]) -> set[tuple[str, str]]:
    """이미 적재된 (source, source_uid) 집합.

    목록 페이지는 매 사이클 같은 30건을 다시 준다. 신규만 골라내지 않으면
    상세 조회(리포트당 1콜)와 누적 건수가 매시간 헛돈다.
    """
    if not keys:
        return set()
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "select source, source_uid from tnm_reports"
            " where (source, source_uid) in ("
            "   select * from unnest(%s::text[], %s::text[]))",
            ([k[0] for k in keys], [k[1] for k in keys]))
        return {(r[0], r[1]) for r in await cur.fetchall()}


async def insert_reports(rows: list[dict]) -> int:
    """리포트 적재. (source, source_uid) 충돌은 무시 — 멱등. 반환: 실제 삽입 수."""
    if not rows:
        return 0
    inserted = 0
    async with _pool.connection() as conn:
        for d in rows:
            cur = await conn.execute(
                "insert into tnm_reports (source, source_uid, broker, category,"
                " ticker, stock_name, title, summary, url, pdf_url, analyst,"
                " target_price, opinion, published_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " on conflict (source, source_uid) do nothing",
                (d["source"], d["source_uid"], d["broker"], d["category"],
                 d.get("ticker"), d.get("stock_name"), d["title"], d.get("summary"),
                 d["url"], d.get("pdf_url"), d.get("analyst"),
                 d.get("target_price"), d.get("opinion"), d["published_at"]))
            inserted += max(cur.rowcount, 0)
    return inserted


async def pending_report_ingest(days: int = 7, limit: int = 200,
                                max_attempts: int = 3) -> list[dict]:
    """분석 파이프라인에 아직 못 들어간 리포트 (종목이 붙은 것만).

    적재는 수집과 분리돼 있다. 종목 등록 상한에 걸려 이번 사이클에 못 들어간
    리포트가 다음 사이클에 다시 시도되게 하려면, '적재 대기' 를 **수집 결과가
    아니라 DB 상태**로 표현해야 한다. raw_item_id 가 그 표시다.
    """
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "select id, source, source_uid, broker, ticker, stock_name, title,"
            " summary, url, target_price, opinion, analyst, published_at"
            " from tnm_reports"
            " where raw_item_id is null and ticker is not null"
            "   and ingest_attempts < %s"
            "   and published_at >= now() - make_interval(days => %s)"
            " order by published_at desc limit %s",
            (max_attempts, max(1, days), min(max(limit, 1), 1000)))
        keys = ("id", "source", "source_uid", "broker", "ticker", "stock_name",
                "title", "summary", "url", "target_price", "opinion", "analyst",
                "published_at")
        return [dict(zip(keys, r)) for r in await cur.fetchall()]


async def bump_report_attempts(ids: list[int]) -> int:
    """적재 시도 횟수 +1. 상한에 닿으면 pending 에서 빠져 무한 재시도를 멈춘다."""
    if not ids:
        return 0
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "update tnm_reports set ingest_attempts = ingest_attempts + 1"
            " where id = any(%s)", (ids,))
        return max(cur.rowcount, 0)


async def link_report_items() -> int:
    """리포트 ↔ raw_items 연결. raw_items 의 source_uid 규약은 'source:uid'."""
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "update tnm_reports r set raw_item_id = i.id from tnm_raw_items i"
            " where i.source = 'research'"
            "   and i.source_uid = r.source || ':' || r.source_uid"
            "   and r.raw_item_id is null")
        return max(cur.rowcount, 0)


async def bump_brokers(counts: dict[str, int], seen_at) -> int:
    """증권사별 누적 건수·최종 관측 시각 갱신. 미등록 이름은 조용히 무시된다."""
    if not counts:
        return 0
    names = list(counts)
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "update tnm_brokers b set reports = b.reports + v.n,"
            " last_seen_at = greatest(coalesce(b.last_seen_at, %s), %s)"
            " from (select * from unnest(%s::text[], %s::int[])) as v(name, n)"
            " where b.name = v.name",
            (seen_at, seen_at, names, [counts[n] for n in names]))
        return max(cur.rowcount, 0)


_REPORT_COLS = ("id, source, source_uid, broker, category, ticker, stock_name,"
                " title, summary, url, pdf_url, analyst, target_price, opinion,"
                " published_at, raw_item_id, collected_at")


async def list_reports(broker: str | None = None, category: str | None = None,
                       ticker: str | None = None, days: int = 7,
                       limit: int = 100, offset: int = 0) -> list[dict]:
    where, args = ["published_at >= now() - make_interval(days => %s)"], [max(1, days)]
    if broker:
        where.append("broker = %s")
        args.append(broker)
    if category:
        where.append("category = %s")
        args.append(category)
    if ticker:
        where.append("ticker = %s")
        args.append(ticker)
    args += [min(max(limit, 1), 500), max(offset, 0)]
    async with _pool.connection() as conn:
        cur = await conn.execute(
            f"select {_REPORT_COLS} from tnm_reports where {' and '.join(where)}"
            " order by published_at desc, id desc limit %s offset %s", tuple(args))
        keys = [c.strip() for c in _REPORT_COLS.split(",")]
        out = []
        for r in await cur.fetchall():
            d = dict(zip(keys, r))
            for ts in ("published_at", "collected_at"):
                if d.get(ts) is not None:
                    d[ts] = d[ts].isoformat()
            out.append(d)
        return out


async def report_stats(days: int = 7) -> dict:
    """최근 N일 수집 현황 — 증권사별 건수와 미등록 이름."""
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "select r.broker, count(*), bool_or(b.name is not null)"
            " from tnm_reports r left join tnm_brokers b on b.name = r.broker"
            " where r.published_at >= now() - make_interval(days => %s)"
            " group by r.broker order by count(*) desc", (max(1, days),))
        rows = await cur.fetchall()
    return {"by_broker": [{"broker": r[0], "count": r[1], "registered": r[2]}
                          for r in rows],
            "total": sum(r[1] for r in rows),
            "unregistered": [r[0] for r in rows if not r[2]]}


# ---------------- 임베딩·신규성 워커 (M4 — DB 가 큐) ----------------

async def pending_embeds(limit: int = 8) -> list[dict]:
    """임베딩 대기 항목 (백오프 시각 지난 것만, 수집 순)."""
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "select id, title, norm_body, embed_attempts from tnm_raw_items"
            " where embedding is null and (next_retry_at is null or next_retry_at <= now())"
            " order by collected_at limit %s", (limit,))
        return [{"id": r[0], "title": r[1], "norm_body": r[2], "attempts": r[3]}
                for r in await cur.fetchall()]


async def save_embedding(item_id: int, vec: list[float]) -> None:
    async with _pool.connection() as conn:
        await conn.execute(
            "update tnm_raw_items set embedding = %s::vector,"
            " next_retry_at = null where id = %s",
            ("[" + ",".join(f"{v:.7g}" for v in vec) + "]", item_id))


async def mark_embed_retry(item_ids: list[int], delay_sec: int) -> None:
    """Ollama 불가 — 백오프 후 재시도 (수집은 계속, 큐만 쌓임)."""
    if not item_ids:
        return
    async with _pool.connection() as conn:
        await conn.execute(
            "update tnm_raw_items set embed_attempts = embed_attempts + 1,"
            " next_retry_at = now() + make_interval(secs => %s)"
            " where id = any(%s)", (delay_sec, item_ids))


# 분류 큐 우선순위. LLM 처리량이 파이프라인의 병목이라(실측: 발행→분석 p50
# 110분 중 85분이 이 큐) **순서가 곧 장중 반응 속도**다.
#
#   0  매매 대상 · 최근 발행  — 장중에 지금 벌어지는 일
#   1  매매 대상              — 늦게 들어온 기사라도 매매 대상이 먼저
#   2  그 외                  — 수집전용·관측 종목, 소급 백필
#
# 버킷 안에서는 수집 순(FIFO)이라 같은 급끼리는 공정하다. 백필이 2번에 모여
# 있어도 0·1이 비면 그대로 소화된다 — 굶지 않는다.
_FRESH_HOURS = 6

_CLASSIFY_PRIORITY = (
    " case when w.tier = 'trade' and r.published_at >= now() - interval '%d hours'"
    "        then 0"
    "      when w.tier = 'trade' then 1 else 2 end" % _FRESH_HOURS)


async def pending_dedup(limit: int = 50) -> list[int]:
    """임베딩 완료·novelty 미판정 항목 id.

    분류와 같은 우선순위를 쓴다. 여기는 LLM 을 안 써서 병목이 아니지만, 순서가
    다르면 장중 기사가 분류 큐에 **늦게 도착**해 앞의 우선순위가 무의미해진다.
    """
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "select r.id from tnm_raw_items r"
            " join tnm_watchlist w on w.id = r.watchlist_id"
            " where r.embedding is not null and r.novelty is null"
            f" order by {_CLASSIFY_PRIORITY}, r.collected_at limit %s", (limit,))
        return [r[0] for r in await cur.fetchall()]


async def max_similarity(item_id: int, window_days: int = 7) -> float | None:
    """동일 종목 창 내 '먼저 적재된'(id 작은) 항목과의 최대 코사인 유사도.

    먼저 온 기사가 new, 나중 재탕이 duplicate 가 되도록 비교 방향을 고정한다.
    """
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "select max(1 - (r.embedding <=> t.embedding))"
            " from tnm_raw_items r,"
            "  (select id, watchlist_id, embedding, published_at"
            "   from tnm_raw_items where id = %s) t"
            " where r.watchlist_id = t.watchlist_id and r.id < t.id"
            "  and r.embedding is not null"
            "  and r.published_at >= t.published_at - make_interval(days => %s)",
            (item_id, window_days))
        row = await cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None


async def set_novelty(item_id: int, novelty: str, similarity: float | None) -> None:
    async with _pool.connection() as conn:
        await conn.execute(
            "update tnm_raw_items set novelty = %s, similarity = %s where id = %s",
            (novelty, similarity, item_id))


async def insert_skipped_duplicate(item_id: int, similarity: float | None) -> None:
    """duplicate 판정 → LLM 생략, analyses 즉시 종결 (score 0)."""
    async with _pool.connection() as conn:
        await conn.execute(
            "insert into tnm_analyses (raw_item_id, status, novelty, score, score_detail)"
            " values (%s, 'skipped_duplicate', 'duplicate', 0,"
            "  jsonb_build_object('similarity', %s::numeric))"
            " on conflict (raw_item_id) do nothing", (item_id, similarity))


# ---------------- LLM 분류 (M5) ----------------

async def pending_classify(limit: int = 2) -> list[dict]:
    """분류 대기: 신규성 판정 완료(new/follow_up)·분석 미존재.

    수집 순이 아니라 **우선순위 순**이다. 종전에는 collected_at FIFO 라, 장중에
    매매 대상 종목의 기사가 들어와도 소급 백필 100여 건 뒤에 줄을 섰다.
    """
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "select r.id, r.source, r.title, r.body, r.norm_body, r.published_at,"
            " r.novelty, w.name"
            " from tnm_raw_items r join tnm_watchlist w on w.id = r.watchlist_id"
            " where r.novelty in ('new','follow_up')"
            "  and not exists (select 1 from tnm_analyses a where a.raw_item_id = r.id)"
            f" order by {_CLASSIFY_PRIORITY}, r.collected_at limit %s", (limit,))
        return [{"id": r[0], "source": r[1], "title": r[2], "body": r[3],
                 "norm_body": r[4], "published_at": r[5].isoformat(), "novelty": r[6],
                 "stock_name": r[7]} for r in await cur.fetchall()]


async def insert_analysis(raw_item_id: int, a: dict, novelty: str, score: int,
                          score_detail: dict, model_name: str, latency_ms: int,
                          retries: int, input_hash: str, warn: bool) -> None:
    import json
    async with _pool.connection() as conn:
        await conn.execute(
            "insert into tnm_analyses (raw_item_id, status, category, is_material,"
            " impact_direction, impact_horizon, confidence, novelty, reason, summary,"
            " score, score_detail, model_name, latency_ms, retries, input_hash,"
            " warn_hallucination)"
            " values (%s,'ok',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s)"
            " on conflict (raw_item_id) do nothing",
            (raw_item_id, a["category"], a["is_material"], a["impact_direction"],
             a["impact_horizon"], a["confidence"], novelty, a["reason"], a["summary"],
             score, json.dumps(score_detail, ensure_ascii=False), model_name,
             latency_ms, retries, input_hash, warn))


async def insert_llm_failed(raw_item_id: int, novelty: str, input_hash: str,
                            retries: int, model_name: str) -> None:
    """스키마 검증 최종 실패 — 항목을 버리지 않고 llm_failed 로 적재 (FR-06)."""
    async with _pool.connection() as conn:
        await conn.execute(
            "insert into tnm_analyses (raw_item_id, status, novelty, retries,"
            " input_hash, model_name)"
            " values (%s, 'llm_failed', %s, %s, %s, %s)"
            " on conflict (raw_item_id) do nothing",
            (raw_item_id, novelty, retries, input_hash, model_name))


async def log_llm_call(raw_item_id: int, input_hash: str, model_name: str,
                       latency_ms: int, attempt: int, ok: bool,
                       error: str | None) -> None:
    """비기능 9장: 모든 LLM 호출의 입력해시·모델·지연·재시도 기록."""
    async with _pool.connection() as conn:
        await conn.execute(
            "insert into tnm_llm_calls (raw_item_id, input_hash, model_name,"
            " latency_ms, attempt, ok, error) values (%s,%s,%s,%s,%s,%s,%s)",
            (raw_item_id, input_hash, model_name, latency_ms, attempt, ok, error))


# ---------------- 조회·라벨 (M6 — 대시보드) ----------------

async def list_items(date: str | None = None, ticker: str | None = None,
                     min_score: int | None = None, status: str | None = None,
                     novelty: str | None = None, limit: int = 100,
                     offset: int = 0) -> list[dict]:
    """분석 목록 (최신순). 필터는 전부 선택.

    offset: 페이지 넘김. 화면은 안 쓰지만 **소급 측정이 전량을 읽으려면 필요**하다
    (limit 상한이 500이라 수천 건을 한 번에 못 받는다). 정렬이
    (published_at desc, id desc) 로 결정적이라 페이지 경계에서 누락·중복이 없다.
    """
    where, args = [], []
    if date:
        where.append("(r.published_at at time zone 'Asia/Seoul')::date = %s::date")
        args.append(date)
    if ticker:
        where.append("w.ticker = %s"); args.append(ticker)
    if min_score is not None:
        where.append("coalesce(a.score, 0) >= %s"); args.append(int(min_score))
    if status:
        where.append("a.status = %s"); args.append(status)
    if novelty:
        where.append("a.novelty = %s"); args.append(novelty)
    args.append(min(int(limit), 500))
    args.append(max(0, int(offset)))
    sql = (
        "select a.id, a.status, a.category, a.impact_direction, a.impact_horizon,"
        " a.confidence, a.novelty, a.score, a.warn_hallucination, a.created_at,"
        " r.source, r.title, r.url, r.published_at, w.ticker, w.name,"
        " l.human_verdict"
        " from tnm_analyses a"
        " join tnm_raw_items r on r.id = a.raw_item_id"
        " join tnm_watchlist w on w.id = r.watchlist_id"
        " left join tnm_labels l on l.analysis_id = a.id"
        + (" where " + " and ".join(where) if where else "")
        + " order by r.published_at desc, a.id desc limit %s offset %s")
    keys = ("id", "status", "category", "impact_direction", "impact_horizon",
            "confidence", "novelty", "score", "warn_hallucination", "created_at",
            "source", "title", "url", "published_at", "ticker", "name",
            "human_verdict")
    async with _pool.connection() as conn:
        cur = await conn.execute(sql, args)
        out = []
        for r in await cur.fetchall():
            d = dict(zip(keys, r))
            for ts in ("created_at", "published_at"):
                if d[ts] is not None:
                    d[ts] = d[ts].isoformat()
            if d["confidence"] is not None:
                d["confidence"] = float(d["confidence"])
            out.append(d)
        return out


async def get_item(analysis_id: int) -> dict | None:
    """상세: 판정 전체 + 원문(P5: 링크·본문 보존) + LLM 호출 로그."""
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "select a.id, a.status, a.category, a.is_material, a.impact_direction,"
            " a.impact_horizon, a.confidence, a.novelty, a.reason, a.summary,"
            " a.score, a.score_detail, a.model_name, a.latency_ms, a.retries,"
            " a.warn_hallucination, a.created_at,"
            " r.id, r.source, r.title, r.body, r.url, r.published_at, r.similarity,"
            " w.ticker, w.name, l.human_verdict, l.note"
            " from tnm_analyses a"
            " join tnm_raw_items r on r.id = a.raw_item_id"
            " join tnm_watchlist w on w.id = r.watchlist_id"
            " left join tnm_labels l on l.analysis_id = a.id"
            " where a.id = %s", (analysis_id,))
        row = await cur.fetchone()
        if row is None:
            return None
        keys = ("id", "status", "category", "is_material", "impact_direction",
                "impact_horizon", "confidence", "novelty", "reason", "summary",
                "score", "score_detail", "model_name", "latency_ms", "retries",
                "warn_hallucination", "created_at", "raw_item_id", "source",
                "title", "body", "url", "published_at", "similarity",
                "ticker", "name", "human_verdict", "label_note")
        d = dict(zip(keys, row))
        for ts in ("created_at", "published_at"):
            if d[ts] is not None:
                d[ts] = d[ts].isoformat()
        for num in ("confidence", "similarity"):
            if d[num] is not None:
                d[num] = float(d[num])
        cur = await conn.execute(
            "select model_name, latency_ms, attempt, ok, error, called_at"
            " from tnm_llm_calls where raw_item_id = %s order by id",
            (d["raw_item_id"],))
        d["llm_calls"] = [
            {"model_name": c[0], "latency_ms": c[1], "attempt": c[2], "ok": c[3],
             "error": c[4], "called_at": c[5].isoformat() if c[5] else None}
            for c in await cur.fetchall()]
        return d


async def upsert_label(analysis_id: int, verdict: str, note: str | None) -> bool:
    """사람 정답 라벨 (Shadow 검증용 — FR-10). 재라벨 시 갱신."""
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "insert into tnm_labels (analysis_id, human_verdict, note)"
            " select %s, %s, %s"
            " where exists (select 1 from tnm_analyses where id = %s)"
            " on conflict (analysis_id) do update"
            "  set human_verdict = excluded.human_verdict, note = excluded.note,"
            "      labeled_at = now()",
            (analysis_id, verdict, note, analysis_id))
        return cur.rowcount > 0


# ---------------- Shadow 지표 · 알림 (M7·M8) ----------------

async def label_week_counts(weeks: int = 4) -> list[dict]:
    """주별(발행 주, KST) 라벨 기반 tp/fp/fn 카운트 — metrics.week_metrics 입력."""
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "select to_char(date_trunc('week', r.published_at at time zone 'Asia/Seoul'),"
            "  'YYYY-MM-DD') as week,"
            " count(*) as labeled,"
            " count(*) filter (where l.human_verdict = 'important') as important,"
            " count(*) filter (where a.score >= w.score_threshold"
            "   and l.human_verdict = 'important') as tp,"
            " count(*) filter (where a.score >= w.score_threshold"
            "   and l.human_verdict = 'noise') as fp,"
            " count(*) filter (where a.score < w.score_threshold"
            "   and l.human_verdict = 'important') as fn"
            " from tnm_labels l"
            " join tnm_analyses a on a.id = l.analysis_id and a.status = 'ok'"
            " join tnm_raw_items r on r.id = a.raw_item_id"
            " join tnm_watchlist w on w.id = r.watchlist_id"
            " group by 1 order by 1 desc limit %s", (weeks,))
        keys = ("week", "labeled", "important", "tp", "fp", "fn")
        return [dict(zip(keys, r)) for r in await cur.fetchall()]


async def pending_alerts(limit: int = 50) -> list[dict]:
    """발송 후보: 점수 ≥ 종목 임계값, 정상 판정, 알림 기록 없음 (오래된 순)."""
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "select a.id, a.score, a.category, a.impact_direction, a.impact_horizon,"
            " a.summary, r.title, r.url, w.ticker, w.name"
            " from tnm_analyses a"
            " join tnm_raw_items r on r.id = a.raw_item_id"
            " join tnm_watchlist w on w.id = r.watchlist_id"
            " where a.status = 'ok' and a.score >= w.score_threshold"
            "  and not exists (select 1 from tnm_notifications n"
            "                  where n.analysis_id = a.id)"
            " order by a.created_at limit %s", (limit,))
        keys = ("id", "score", "category", "impact_direction", "impact_horizon",
                "summary", "title", "url", "ticker", "name")
        return [dict(zip(keys, r)) for r in await cur.fetchall()]


async def high_score_recent(min_score: int, hours: int) -> list[dict]:
    """최근 N시간 내 고점수 분석 — 감시목록 편입 후보. 종목당 최고점 1건."""
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "select distinct on (w.ticker) w.ticker, w.name, a.score,"
            " a.impact_direction, a.category, r.title, r.url"
            " from tnm_analyses a"
            " join tnm_raw_items r on r.id = a.raw_item_id"
            " join tnm_watchlist w on w.id = r.watchlist_id"
            " where a.status = 'ok' and a.score >= %s"
            "  and a.created_at >= now() - make_interval(hours => %s)"
            " order by w.ticker, a.score desc", (int(min_score), int(hours)))
        keys = ("ticker", "name", "score", "impact_direction", "category",
                "title", "url")
        return [dict(zip(keys, r)) for r in await cur.fetchall()]


async def notifications_today(ticker: str) -> int:
    """해당 종목의 오늘(KST) 실발송 수 — 일일 상한 판정용 (suppressed 제외)."""
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "select count(*) from tnm_notifications n"
            " join tnm_analyses a on a.id = n.analysis_id"
            " join tnm_raw_items r on r.id = a.raw_item_id"
            " join tnm_watchlist w on w.id = r.watchlist_id"
            " where w.ticker = %s and n.channel = 'slack'"
            "  and (n.sent_at at time zone 'Asia/Seoul')::date ="
            "      (now() at time zone 'Asia/Seoul')::date", (ticker,))
        return int((await cur.fetchone())[0])


async def insert_notification(analysis_id: int, channel: str,
                              is_deferred: bool) -> None:
    async with _pool.connection() as conn:
        await conn.execute(
            "insert into tnm_notifications (analysis_id, channel, is_deferred)"
            " values (%s, %s, %s)", (analysis_id, channel, is_deferred))


async def requeue_failed() -> int:
    """llm_failed 분석행을 지워 분류 워커가 다시 집어가게 한다 (원문은 보존).

    프롬프트·모델을 개선한 뒤 실패분을 재처리할 때 쓴다. 반환: 재큐 건수.
    """
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "delete from tnm_analyses where status = 'llm_failed'")
        return max(cur.rowcount, 0)


async def queue_stats() -> dict:
    """상태 화면용 큐 적체량."""
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "select"
            " (select count(*) from tnm_raw_items where embedding is null) as embed_pending,"
            " (select count(*) from tnm_raw_items"
            "   where embedding is not null and novelty is null) as dedup_pending,"
            " (select count(*) from tnm_raw_items r where r.novelty in ('new','follow_up')"
            "   and not exists (select 1 from tnm_analyses a where a.raw_item_id = r.id))"
            "   as classify_pending,"
            " (select count(*) from tnm_analyses where status = 'llm_failed') as llm_failed,"
            " (select count(*) from tnm_analyses where status = 'skipped_duplicate')"
            "   as duplicates,"
            " (select count(*) from tnm_raw_items) as raw_total")
        r = await cur.fetchone()
        return {"embed_pending": r[0], "dedup_pending": r[1], "classify_pending": r[2],
                "llm_failed": r[3], "duplicates": r[4], "raw_total": r[5]}
