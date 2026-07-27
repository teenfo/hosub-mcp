"""TNM(Trading News Monitor) API 서버 — 127.0.0.1:8602.

대시보드 프록시(X-Internal-Token)로만 접근한다. DB 미가동·트레이딩 다운·
Mac(Ollama) 다운 상태에서도 기동은 유지되고 각 루프가 스킵/재시도한다.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import db, metrics, ollama, settings
from .collect import CollectRunner
from .notify.loop import Notifier
from .pipeline.workers import ClassifyWorker, DedupWorker, EmbedWorker
from . import logsafe
from .promote import promoter
from .watch import WatchSync

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
# httpx 가 요청 URL 전체를 INFO 로 찍는데 DART 는 API 키를 쿼리로 받는다 —
# 가리지 않으면 journald 에 키가 그대로 남는다(실측 2026-07-27).
logsafe.install()
log = logging.getLogger("tnm")

watchsync = WatchSync()
collector = CollectRunner()
embedder = EmbedWorker()
deduper = DedupWorker()
classifier = ClassifyWorker()
notifier = Notifier()


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [
        asyncio.create_task(db.init_loop()),
        asyncio.create_task(watchsync.loop()),
        asyncio.create_task(collector.dart_loop()),
        asyncio.create_task(collector.news_loop()),
        asyncio.create_task(embedder.loop()),
        asyncio.create_task(deduper.loop()),
        asyncio.create_task(classifier.loop()),
        asyncio.create_task(notifier.loop()),
        asyncio.create_task(promoter.loop()),
    ]
    yield
    for t in tasks:
        t.cancel()
    await db.close()


app = FastAPI(title="hosub-tnm", lifespan=lifespan)


def _authed(request: Request) -> bool:
    token = request.headers.get("X-Internal-Token", "")
    return bool(settings.INTERNAL_TOKEN) and hmac.compare_digest(
        token, settings.INTERNAL_TOKEN)


def require_auth(request: Request) -> None:
    if not _authed(request):
        raise HTTPException(401, "인증 필요")


def _require_db() -> None:
    if not db.ready:
        raise HTTPException(503, f"DB 미준비: {db.last_error or '초기화 중'}")


@app.get("/api/status")
async def api_status(_=Depends(require_auth)):
    out = {
        "ok": True,
        "db_ready": db.ready,
        "db_error": db.last_error,
        "watch_sync": watchsync.status(),
        "collect": collector.status(),
        "embed": embedder.status(),
        "dedup": deduper.status(),
        "classify": classifier.status(),
        "notify": notifier.status(),
        "promote": promoter.status(),
        "ollama": await ollama.reachable(),
        "shadow_mode": bool(settings.ALERTS.get("shadow_mode", True)),
        "dart_enabled": bool(settings.DART_API_KEY),
        "naver_enabled": bool(settings.NAVER_CLIENT_ID and settings.NAVER_CLIENT_SECRET),
    }
    if db.ready:
        try:
            out["queue"] = await db.queue_stats()
        except Exception as e:  # noqa: BLE001
            out["queue_error"] = str(e)
    return out


# ---------------- 관심종목 ----------------

@app.get("/api/watch")
async def api_watch_list(_=Depends(require_auth)):
    _require_db()
    return {"entries": await db.list_watch()}


@app.post("/api/watch")
async def api_watch_add(payload: dict, _=Depends(require_auth)):
    _require_db()
    ticker = str(payload.get("ticker", "")).strip()
    if not (ticker.isdigit() and len(ticker) == 6):
        return JSONResponse({"ok": False, "error": "6자리 종목코드가 필요합니다"}, 400)
    name = str(payload.get("name", "")).strip() or ticker
    row = await db.add_manual(ticker, name)
    return {"ok": True, "entry": row}


@app.post("/api/watch/sync")
async def api_watch_sync(_=Depends(require_auth)):
    _require_db()
    result = await watchsync.run_once()
    return {"ok": "skipped" not in result, **result}


@app.post("/api/watch/{ticker}/exclude")
async def api_watch_exclude(ticker: str, _=Depends(require_auth)):
    _require_db()
    if not await db.set_excluded(ticker, True):
        return JSONResponse({"ok": False, "error": "종목 없음"}, 404)
    return {"ok": True}


@app.post("/api/watch/{ticker}/include")
async def api_watch_include(ticker: str, _=Depends(require_auth)):
    _require_db()
    if not await db.set_excluded(ticker, False):
        return JSONResponse({"ok": False, "error": "종목 없음"}, 404)
    return {"ok": True}


@app.post("/api/watch/{ticker}/settings")
async def api_watch_settings(ticker: str, payload: dict, _=Depends(require_auth)):
    _require_db()
    try:
        threshold = payload.get("score_threshold")
        cap = payload.get("daily_alert_cap")
        if threshold is not None and not 0 <= int(threshold) <= 100:
            raise ValueError("score_threshold 는 0~100")
        if cap is not None and not 0 <= int(cap) <= 100:
            raise ValueError("daily_alert_cap 은 0~100")
    except (TypeError, ValueError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, 400)
    if not await db.set_alert_settings(ticker, threshold, cap):
        return JSONResponse({"ok": False, "error": "종목 없음 또는 변경 값 없음"}, 404)
    return {"ok": True}


# ---------------- 조회·라벨 ----------------

@app.get("/api/items")
async def api_items(date: str | None = None, ticker: str | None = None,
                    min_score: int | None = None, status: str | None = None,
                    novelty: str | None = None, limit: int = 100,
                    offset: int = 0, _=Depends(require_auth)):
    _require_db()
    return {"items": await db.list_items(date, ticker, min_score, status,
                                         novelty, limit, offset)}


@app.get("/api/items/{analysis_id}")
async def api_item_detail(analysis_id: int, _=Depends(require_auth)):
    _require_db()
    item = await db.get_item(analysis_id)
    if item is None:
        return JSONResponse({"ok": False, "error": "항목 없음"}, 404)
    return {"ok": True, "item": item}


@app.post("/api/items/{analysis_id}/label")
async def api_item_label(analysis_id: int, payload: dict, _=Depends(require_auth)):
    _require_db()
    verdict = str(payload.get("verdict", "")).strip()
    if verdict not in ("important", "noise"):
        return JSONResponse({"ok": False, "error": "verdict 는 important|noise"}, 400)
    note = str(payload.get("note", "")).strip() or None
    if not await db.upsert_label(analysis_id, verdict, note):
        return JSONResponse({"ok": False, "error": "항목 없음"}, 404)
    return {"ok": True}


# ---------------- Shadow 지표 · 알림 ----------------

@app.get("/api/metrics")
async def api_metrics(weeks: int = 4, _=Depends(require_auth)):
    """주별 정밀도/재현율 (Shadow 전환 판정 — 재현율 0.9·정밀도 0.6)."""
    _require_db()
    return {"weeks": await metrics.weekly(db, min(max(weeks, 1), 12)),
            "targets": {"recall": metrics.TARGET_RECALL,
                        "precision": metrics.TARGET_PRECISION}}


@app.post("/api/notify/test")
async def api_notify_test(_=Depends(require_auth)):
    """슬랙 테스트 발송 — 토큰·채널 연결 확인용 (Shadow 여부와 무관하게 1회 발송)."""
    from .notify import slack

    ok, err = await slack.post("✅ TNM 슬랙 연동 테스트 — 이 메시지가 보이면 연결 성공입니다.")
    status = 200 if ok else 502
    return JSONResponse({"ok": ok, "error": err or None}, status)


@app.post("/api/reclassify_failed")
async def api_reclassify_failed(_=Depends(require_auth)):
    """분류 실패(llm_failed) 항목을 재큐 — 프롬프트·모델 개선 후 재처리용."""
    _require_db()
    n = await db.requeue_failed()
    log.info("분류 실패 재큐: %d건", n)
    return {"ok": True, "requeued": n}


# ---------------- 수집 ----------------

@app.post("/api/promote/run")
async def api_promote_run(_=Depends(require_auth)):
    """고점수 종목 감시목록 편입 수동 트리거(수집전용으로만 편입)."""
    _require_db()
    result = await promoter.run_once()
    return {"ok": "skipped" not in result and not result.get("errors"), **result}


@app.post("/api/collect/run")
async def api_collect_run(_=Depends(require_auth)):
    """공시·뉴스 수집 수동 트리거 — 백그라운드 시작 후 즉시 반환."""
    _require_db()
    if collector.running:
        return JSONResponse({"ok": False, "error": "이미 실행 중"}, 409)
    asyncio.create_task(collector.run_once("dart"))
    asyncio.create_task(collector.run_once("news"))
    return {"ok": True, "message": "수집 시작 — /api/status 의 collect 로 결과 확인"}


# ---------------- 설정 ----------------

@app.get("/api/settings")
async def api_settings(_=Depends(require_auth)):
    return settings.masked()


@app.post("/api/settings")
async def api_settings_save(payload: dict, _=Depends(require_auth)):
    allowed = {"dart_api_key", "naver_client_id", "naver_client_secret",
               "ollama_url", "ollama_fallback_url", "slack_bot_token", "slack_channel"}
    kv = {k: v for k, v in payload.items() if k in allowed and isinstance(v, str)}
    try:
        settings.save_keys(**kv)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, 400)
    return {"ok": True, **settings.masked()}
