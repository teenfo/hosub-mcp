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


# ---------------- 수집 (raw_items · 커서) ----------------

async def active_watch_for_collect() -> list[dict]:
    """수집 대상 활성 종목 + 소스별 증분 커서."""
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "select id, ticker, name, dart_corp_code, last_collected_at"
            " from tnm_watchlist where is_active and not is_excluded order by ticker")
        return [{"id": r[0], "ticker": r[1], "name": r[2], "dart_corp_code": r[3],
                 "last_collected_at": r[4] or {}} for r in await cur.fetchall()]


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


async def pending_dedup(limit: int = 50) -> list[int]:
    """임베딩 완료·novelty 미판정 항목 id (수집 순)."""
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "select id from tnm_raw_items"
            " where embedding is not null and novelty is null"
            " order by collected_at limit %s", (limit,))
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
    """분류 대기: 신규성 판정 완료(new/follow_up)·분석 미존재 (수집 순)."""
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "select r.id, r.source, r.title, r.body, r.norm_body, r.published_at,"
            " r.novelty, w.name"
            " from tnm_raw_items r join tnm_watchlist w on w.id = r.watchlist_id"
            " where r.novelty in ('new','follow_up')"
            "  and not exists (select 1 from tnm_analyses a where a.raw_item_id = r.id)"
            " order by r.collected_at limit %s", (limit,))
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
                     novelty: str | None = None, limit: int = 100) -> list[dict]:
    """분석 목록 (최신순). 필터는 전부 선택."""
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
        + " order by r.published_at desc, a.id desc limit %s")
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
