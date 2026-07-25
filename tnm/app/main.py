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

from . import db, settings
from .collect import CollectRunner
from .watch import WatchSync

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("tnm")

watchsync = WatchSync()
collector = CollectRunner()


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [
        asyncio.create_task(db.init_loop()),
        asyncio.create_task(watchsync.loop()),
        asyncio.create_task(collector.dart_loop()),
        asyncio.create_task(collector.news_loop()),
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


# ---------------- 수집 ----------------

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
