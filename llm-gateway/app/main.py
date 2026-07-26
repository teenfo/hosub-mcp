"""공유 LLM 게이트웨이 — HTTP API.

핵심 계약(설계서 4절): **모든 요청은 잡이다.**
/v1/generate 는 항상 같은 응답 형태(job_id 포함)로 ok|pending|failed 를 돌려준다.
호출자는 status 만 보면 되고, pending 이면 같은 job_id 로 폴링하면 된다.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .auth import RateLimiter, authenticate
from .config import RoleConfig, ServiceConfig
from .ollama import BackendError, OllamaClient
from .scheduler import LANES, Scheduler
from .store import (
    MR_APPROVED,
    MR_PENDING,
    MR_PULLING,
    MR_READY,
    MR_REJECTED,
    QUEUED,
    RUNNING,
    Store,
)

log = logging.getLogger("llmgw")

DEFAULT_WAIT = 30
MAX_WAIT = 300
MAX_PROMPT_CHARS = 200_000


def _job_response(job: dict, *, queue_position: int | None = None) -> dict:
    """모든 엔드포인트가 공유하는 단일 응답 형태."""
    status_map = {
        "succeeded": "ok",
        "failed": "failed",
        "cancelled": "cancelled",
        QUEUED: "pending",
        RUNNING: "pending",
    }
    out = {
        "job_id": job["id"],
        "status": status_map.get(job["status"], job["status"]),
        "response": job.get("response"),
        "error": job.get("error"),
        "role": job["role"],
        "model": job["model"],
        "lane": job["lane"],
        "attempts": job["attempts"],
        "metadata": job.get("metadata") or {},
        "created_at": job["created_at"],
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }
    if queue_position is not None:
        out["queue_position"] = queue_position
    return out


def build_app(
    *,
    roles: RoleConfig | None = None,
    services: ServiceConfig | None = None,
    store: Store | None = None,
    client: OllamaClient | None = None,
    scheduler: Scheduler | None = None,
) -> Starlette:
    """앱 조립. 테스트는 가짜 client/store 를 주입할 수 있다."""
    cfg_dir = Path(os.environ.get("LLMGW_CONFIG_DIR", "config"))
    roles = roles or RoleConfig.load(cfg_dir / "roles.yaml")
    services = services or ServiceConfig.load(cfg_dir / "services.yaml")
    store = store or Store(os.environ.get("LLMGW_DB", "/data/llmgw.db"))
    client = client or OllamaClient(roles.backend.base_url, roles.backend.keep_alive)
    scheduler = scheduler or Scheduler(
        store, roles, client,
        max_retries=int(os.environ.get("MAX_RETRIES", 3)),
        auto_install=os.environ.get("AUTO_INSTALL_MODELS", "1") not in ("0", "false"),
        models_refresh=float(os.environ.get("MODELS_REFRESH_SECONDS", 30)),
    )
    limiter = RateLimiter()

    # --- 인증 헬퍼 ---
    def _auth(request: Request):
        svc = authenticate(services, request.headers.get("authorization"))
        if svc is None:
            return None, JSONResponse(
                {"error": "unauthorized"}, status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not limiter.allow(svc):
            return None, JSONResponse(
                {"error": "rate_limited",
                 "detail": f"분당 {svc.rate_limit_per_min}회 제한"},
                status_code=429,
            )
        return svc, None

    # --- 엔드포인트 ---
    async def healthz(request: Request):
        return JSONResponse({"ok": True})

    async def generate(request: Request):
        svc, err = _auth(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            body = {}

        role_name = str(body.get("role") or "").strip()
        prompt = body.get("prompt") or ""
        if not role_name or not isinstance(prompt, str) or not prompt.strip():
            return JSONResponse(
                {"error": "invalid_request", "detail": "role 과 prompt 가 필요합니다"},
                status_code=400,
            )
        role = roles.role(role_name)
        if role is None:
            return JSONResponse(
                {"error": "unknown_role", "known_roles": roles.role_names},
                status_code=404,
            )
        if not svc.may_use(role_name):
            return JSONResponse(
                {"error": "forbidden", "allowed": list(svc.allow_roles)},
                status_code=403,
            )
        limit = role.max_prompt_chars or MAX_PROMPT_CHARS
        if len(prompt) > limit:
            return JSONResponse(
                {"error": "prompt_too_long", "limit": limit, "got": len(prompt)},
                status_code=413,
            )

        # 프롬프트 소유권: 호출자 system 이 우선, 없으면 역할 기본값
        system = body.get("system")
        if system is None:
            system = role.system

        try:
            wait = float(body.get("wait", DEFAULT_WAIT))
        except (TypeError, ValueError):
            wait = DEFAULT_WAIT
        wait = max(0.0, min(wait, MAX_WAIT))

        try:
            priority = int(body.get("priority", 0))
        except (TypeError, ValueError):
            priority = 0

        job = store.create_job(
            service=svc.name, role=role_name, model=role.model, lane=role.lane,
            prompt=prompt, system=system, priority=priority,
            metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
        )
        await scheduler.wait_for(job["id"], wait)
        job = store.get_job(job["id"])
        pos = store.queue_position(job["id"])
        return JSONResponse(_job_response(job, queue_position=pos))

    async def get_job(request: Request):
        svc, err = _auth(request)
        if err:
            return err
        job = store.get_job(request.path_params["job_id"])
        if job is None or job["service"] != svc.name:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return JSONResponse(_job_response(job, queue_position=store.queue_position(job["id"])))

    async def list_jobs(request: Request):
        svc, err = _auth(request)
        if err:
            return err
        status = request.query_params.get("status")
        try:
            limit = int(request.query_params.get("limit", 20))
        except ValueError:
            limit = 20
        jobs = store.list_jobs(service=svc.name, status=status, limit=limit)
        return JSONResponse({"jobs": [_job_response(j) for j in jobs]})

    async def cancel_job(request: Request):
        svc, err = _auth(request)
        if err:
            return err
        ok = store.cancel(request.path_params["job_id"], svc.name)
        if not ok:
            return JSONResponse(
                {"error": "not_cancellable",
                 "detail": "대기 중인 본인 잡만 취소할 수 있습니다"},
                status_code=409,
            )
        return JSONResponse({"status": "cancelled"})

    async def list_roles(request: Request):
        svc, err = _auth(request)
        if err:
            return err
        allowed = [r.to_dict() for r in roles.roles if svc.may_use(r.name)]
        return JSONResponse({"roles": allowed, "service": svc.name})

    # --- 모델 설치 요청 (승인은 hosub = 대시보드/MCP 만) ---
    async def list_model_requests(request: Request):
        svc, err = _auth(request)
        if err:
            return err
        reqs = store.list_model_requests(request.query_params.get("status"))
        live = scheduler.pulling_snapshot()
        for r in reqs:
            if r["model"] in live:
                r["progress"] = live[r["model"]]
        return JSONResponse({
            "requests": reqs,
            "can_decide": svc.admin,
            "available_models": scheduler.available_models,
        })

    async def decide_model_request(request: Request):
        """모델 설치 요청 승인/거부. 모델명에 ':' '/' 가 들어가 body 로 받는다."""
        svc, err = _auth(request)
        if err:
            return err
        if not svc.admin:
            return JSONResponse(
                {"error": "forbidden", "detail": "모델 승인 권한이 없는 서비스입니다"},
                status_code=403,
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        model = str(body.get("model") or "").strip()
        action = str(body.get("action") or "").strip()
        if action not in ("approve", "reject"):
            return JSONResponse(
                {"error": "invalid_request", "detail": "action 은 approve|reject"},
                status_code=400,
            )
        req = store.get_model_request(model)
        if req is None:
            return JSONResponse({"error": "not_found", "model": model}, status_code=404)
        # 진행 중인 설치는 되돌리지 않는다(중간에 끊으면 부분 파일이 남는다)
        if req["status"] in (MR_APPROVED, MR_PULLING):
            return JSONResponse(
                {"error": "in_progress", "detail": "이미 설치가 진행 중입니다",
                 "request": req},
                status_code=409,
            )
        if action == "approve" and req["status"] == MR_READY:
            return JSONResponse({"request": req, "detail": "이미 설치된 모델입니다"})

        if action == "approve":
            # 이전 실패 흔적을 지우고 다시 시도한다
            store.set_model_request_status(model, MR_APPROVED, error="", progress=0)
        else:
            store.set_model_request_status(model, MR_REJECTED)
        log.info("모델 요청 %s → %s (by %s)", model, action, svc.name)
        return JSONResponse({"request": store.get_model_request(model)})

    async def status(request: Request):
        svc, err = _auth(request)
        if err:
            return err
        try:
            models = await client.tags()
            online, backend_error = True, None
        except BackendError as exc:
            models, online, backend_error = [], False, str(exc)

        counts = store.counts_by_status()
        lanes = {
            lane: {
                "queued": counts.get(lane, {}).get(QUEUED, 0),
                "running": counts.get(lane, {}).get(RUNNING, 0),
            }
            for lane in LANES
        }
        role_status = []
        for r in roles.roles:
            if not svc.may_use(r.name):
                continue
            d = r.to_dict()
            d["model_available"] = any(
                m == r.model or m.startswith(r.model + ":") for m in models
            )
            role_status.append(d)
        return JSONResponse({
            "backend": {
                "base_url": roles.backend.base_url,
                "online": online,
                "error": backend_error,
                "models": models,
                "loaded_model": scheduler.loaded_model,
            },
            "lanes": lanes,
            "running": scheduler.running_snapshot(),
            "mem_budget_gb": roles.mem_budget_gb,
            "roles": role_status,
            "usage": store.usage_summary(),
            "model_requests": {
                "pending": len(store.list_model_requests(MR_PENDING)),
                "pulling": scheduler.pulling_snapshot(),
            },
        })

    routes = [
        Route("/healthz", healthz),
        Route("/v1/generate", generate, methods=["POST"]),
        Route("/v1/jobs", list_jobs, methods=["GET"]),
        Route("/v1/jobs/{job_id}", get_job, methods=["GET"]),
        Route("/v1/jobs/{job_id}", cancel_job, methods=["DELETE"]),
        Route("/v1/roles", list_roles),
        Route("/v1/status", status),
        Route("/v1/models/requests", list_model_requests, methods=["GET"]),
        Route("/v1/models/requests", decide_model_request, methods=["POST"]),
    ]

    @asynccontextmanager
    async def lifespan(_app):
        # Starlette 1.x 는 on_startup/on_shutdown 대신 lifespan 만 지원한다.
        await scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop()

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.store = store
    app.state.scheduler = scheduler
    app.state.roles = roles
    app.state.services = services
    return app


def create_app() -> Starlette:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return build_app()
