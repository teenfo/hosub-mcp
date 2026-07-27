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
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from .auth import RateLimiter, authenticate
from .catalog import Catalog
from .config import (
    OVERRIDABLE_FIELDS,
    ROLE_NAME_RE,
    ConfigError,
    RoleConfig,
    ServiceConfig,
    model_matches,
    validate_model_name,
    validate_role_fields,
)
from .notify import SlackNotifier
from .ollama import BackendError, InputTooLong, OllamaClient
from .scheduler import LANES, Scheduler
from .store import (
    MR_APPROVED,
    MR_PENDING,
    MR_PULLING,
    MR_READY,
    MR_REJECTED,
    QUEUED,
    RUNNING,
    SUCCEEDED,
    Store,
)

log = logging.getLogger("llmgw")

DEFAULT_WAIT = 30
MAX_WAIT = 300
MAX_PROMPT_CHARS = 200_000
# 임베딩 한 번에 보낼 수 있는 텍스트 개수
MAX_EMBED_BATCH = 256

# 통합 가이드를 게이트웨이가 직접 서빙한다. 소비 프로젝트(roxlogy 등)는 별도
# 레포에 있어 hosub-mcp 저장소를 못 볼 수 있다 — 토큰만 있으면 curl 한 번으로
# 최신 계약을 가져갈 수 있게 한다. 컨테이너에서는 /app/docs.
DOCS_DIR = Path(
    os.environ.get("LLMGW_DOCS_DIR") or Path(__file__).resolve().parent.parent / "docs"
)


async def _json_body(request: Request) -> dict:
    """본문을 매핑으로만 받는다.

    유효한 JSON 이라도 null·[] 이면 body.get(...) 이 AttributeError 를 내
    500 이 된다. 문서화된 400 을 주려면 매핑인지 먼저 확인해야 한다.
    """
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


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
    # roles.yaml 은 기본값, DB 의 런타임 오버라이드가 그 위에 얹힌다.
    # 주입된 테스트 픽스처도 같은 경로를 타게 여기서 한 번만 적용한다.
    roles.apply_overrides(store.list_role_overrides())
    catalog = Catalog(cfg_dir / "catalog.yaml")
    roles.set_catalog_sizes(catalog.sizes())
    client = client or OllamaClient(roles.backend.base_url, roles.backend.keep_alive)
    scheduler = scheduler or Scheduler(
        store, roles, client,
        max_retries=int(os.environ.get("MAX_RETRIES", 3)),
        auto_install=os.environ.get("AUTO_INSTALL_MODELS", "1") not in ("0", "false"),
        models_refresh=float(os.environ.get("MODELS_REFRESH_SECONDS", 30)),
        retention_days=int(os.environ.get("JOB_RETENTION_DAYS", 30)),
        notifier=SlackNotifier(),
    )
    limiter = RateLimiter()

    # --- 인증 헬퍼 ---
    def _client_ip(request: Request) -> str:
        # Caddy 뒤에 있으므로 request.client 는 프록시 주소다
        fwd = request.headers.get("x-forwarded-for", "")
        return fwd.split(",")[0].strip() or (
            request.client.host if request.client else "?"
        )

    def _auth(request: Request):
        svc = authenticate(services, request.headers.get("authorization"))
        if svc is None:
            # /llm/v1/* 이 공개돼 있으므로 누가 두드리는지 보이게 남긴다.
            # (토큰 값은 절대 로그에 남기지 않는다)
            log.warning("인증 실패: %s %s from %s",
                        request.method, request.url.path, _client_ip(request))
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

    def _require_admin(request: Request):
        """관리 엔드포인트 게이트.

        Caddy 가 /v1/admin/* 을 공개 경로에서 404 로 막지만 프록시만 믿지 않는다
        (설정 실수·직접 접근이 있을 수 있다). 두 겹 다 있어야 한다.
        """
        svc, err = _auth(request)
        if err:
            return None, err
        if not svc.admin:
            log.warning("관리 API 거부: %s %s (service=%s)",
                        request.method, request.url.path, svc.name)
            return None, JSONResponse(
                {"error": "forbidden", "detail": "관리 권한이 없는 서비스입니다"},
                status_code=403,
            )
        return svc, None

    def _reload_overrides() -> list[dict]:
        """DB → 유효 역할 맵. 쓰기 직후 항상 이 한 경로로만 반영한다."""
        return roles.apply_overrides(store.list_role_overrides())

    def _sync_catalog_sizes() -> None:
        """카탈로그는 git pull 로 바뀐다(재빌드 없음) — 크기 추정에 즉시 반영한다.

        기동 시 한 번만 읽으면 새로 추가한 모델이 크기 게이트에서 20GB 로 가정돼
        멀쩡한 설치가 거부된다. Catalog 는 mtime 캐시라 매번 불러도 싸다.
        """
        roles.set_catalog_sizes(catalog.sizes())

    # --- 엔드포인트 ---
    async def healthz(request: Request):
        return JSONResponse({"ok": True})

    async def generate(request: Request):
        svc, err = _auth(request)
        if err:
            return err
        body = await _json_body(request)

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
        if role.kind != "generate":
            return JSONResponse(
                {"error": "wrong_role_kind",
                 "detail": f"'{role_name}' 은 임베딩용 역할입니다. /v1/embed 를 쓰세요."},
                status_code=400,
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

        # 모델뿐 아니라 options·timeout 도 지금 값으로 못박는다. 역할의 모델을
        # 런타임에 바꿀 수 있으므로, 큐에 남아 있던 잡이 "옛 모델 + 새 옵션" 으로
        # 도는 일이 없어야 한다.
        job = store.create_job(
            service=svc.name, role=role_name, model=role.model, lane=role.lane,
            prompt=prompt, system=system, priority=priority,
            metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
            options=dict(role.options), timeout=role.timeout,
        )
        await scheduler.wait_for(job["id"], wait)
        job = store.get_job(job["id"])
        pos = store.queue_position(job["id"])
        return JSONResponse(_job_response(job, queue_position=pos))

    async def embed(request: Request):
        """텍스트 임베딩. **이 엔드포인트만 잡이 아니다.**

        임베딩은 밀리초 단위로 끝나고 보통 RAG 조회 같은 즉시 응답 경로에서 쓴다.
        2레인 큐에 태우면 3분짜리 32b 생성 뒤에서 기다리게 되어 쓸모가 없어진다.
        대신 인증·역할 제한·레이트리밋·사용량 집계는 생성과 똑같이 적용된다.

        임베딩 모델은 작아야 한다(bge-m3 = 1.2GB). 큰 모델을 큐 밖에서 돌리면
        메모리 예산 가드를 우회하게 되므로, roles.yaml 에 kind: embed 로 등록할 때
        크기를 확인한다.
        """
        svc, err = _auth(request)
        if err:
            return err
        body = await _json_body(request)

        role_name = str(body.get("role") or "").strip()
        role = roles.role(role_name)
        if not role_name or role is None:
            return JSONResponse(
                {"error": "unknown_role",
                 "known_roles": [r.name for r in roles.roles if r.kind == "embed"]},
                status_code=404,
            )
        if role.kind != "embed":
            return JSONResponse(
                {"error": "wrong_role_kind",
                 "detail": f"'{role_name}' 은 생성용 역할입니다. /v1/generate 를 쓰세요.",
                 "embed_roles": [r.name for r in roles.roles if r.kind == "embed"]},
                status_code=400,
            )
        if not svc.may_use(role_name):
            return JSONResponse(
                {"error": "forbidden", "allowed": list(svc.allow_roles)}, status_code=403
            )

        raw = body.get("input")
        inputs = [raw] if isinstance(raw, str) else raw
        if (not isinstance(inputs, list) or not inputs
                or not all(isinstance(t, str) for t in inputs)):
            return JSONResponse(
                {"error": "invalid_request",
                 "detail": "input 은 문자열 또는 문자열 배열이어야 합니다"},
                status_code=400,
            )
        if len(inputs) > MAX_EMBED_BATCH:
            return JSONResponse(
                {"error": "batch_too_large", "limit": MAX_EMBED_BATCH, "got": len(inputs)},
                status_code=413,
            )
        total_chars = sum(len(t) for t in inputs)
        limit = role.max_prompt_chars or MAX_PROMPT_CHARS
        if total_chars > limit:
            return JSONResponse(
                {"error": "input_too_long", "limit": limit, "got": total_chars},
                status_code=413,
            )

        # 모델이 없으면 생성 잡과 같은 설치 요청 흐름을 탄다. 임베딩은 잡을 만들지
        # 않으므로 스케줄러의 미설치 탐지(_triage_missing)가 못 본다 — 여기서 건다.
        if not scheduler.model_ready(role.model):
            req = store.ensure_model_request(
                role.model, requested_by=svc.name,
                roles=roles.roles_using(role.model),
                est_size_gb=roles.model_size_gb(role.model),
            )
            return JSONResponse({
                "status": "failed", "retryable": True,
                "error": f"모델 '{role.model}' 이 아직 설치되지 않았습니다 "
                         f"(설치 요청 상태: {req['status']}).",
                "hint": "hosub 대시보드 LLM 페이지에서 승인하면 자동 설치됩니다.",
                "model_request": req["status"],
            }, status_code=503)

        try:
            result = await client.embed(
                model=role.model, inputs=inputs, timeout=role.timeout
            )
        except InputTooLong as exc:
            return JSONResponse(
                {"status": "failed", "error": str(exc), "retryable": False},
                status_code=413,
            )
        except BackendError as exc:
            store.record_usage(service=svc.name, role=role_name, model=role.model,
                               eval_count=None, duration_ms=None, status="failed")
            # 재시도 가능(맥 재부팅 등)이면 503 — 호출자가 나중에 다시 하면 된다
            return JSONResponse(
                {"status": "failed", "error": str(exc), "retryable": exc.retryable},
                status_code=503 if exc.retryable else 502,
            )

        store.record_usage(
            service=svc.name, role=role_name, model=role.model,
            eval_count=result.prompt_eval_count, duration_ms=result.duration_ms,
            status=SUCCEEDED,
        )
        return JSONResponse({
            "status": "ok",
            "model": role.model,
            "embeddings": result.embeddings,
            "count": len(result.embeddings),
            "dimensions": len(result.embeddings[0]) if result.embeddings else 0,
            "duration_ms": result.duration_ms,
        })

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
        job_id = request.path_params["job_id"]
        ok = store.cancel(job_id, svc.name)
        if ok:
            # 같은 잡을 wait_for 로 기다리는 요청이 있으면 깨운다. 안 그러면 취소된
            # 잡을 최대 MAX_WAIT(300초) 동안 붙잡고 있고 이벤트도 안 치워진다.
            scheduler.notify_cancelled(job_id)
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

    async def integration_doc(request: Request):
        """소비 프로젝트용 통합 가이드를 마크다운으로 돌려준다.

        경로 파라미터를 두지 않는다 — 파일 하나만 노출하므로 경로 탐색 여지가 없다.
        인증을 요구하는 이유: 공개 경로(/llm/v1/*)에 놓이므로, 스캐너에게 역할·모델·
        내부 구조를 알려줄 이유가 없다. 소비자는 어차피 토큰을 받는다.
        """
        _svc, err = _auth(request)
        if err:
            return err
        path = DOCS_DIR / "integration.md"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("통합 가이드를 읽을 수 없음 %s: %s", path, exc)
            return JSONResponse(
                {"error": "not_found",
                 "detail": f"통합 가이드가 없습니다({path}). "
                           "이미지를 다시 빌드했는지 확인하세요."},
                status_code=404,
            )
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")

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
        body = await _json_body(request)
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

    # --- 관리 API (/v1/admin/*) ---
    #
    # Caddy 는 이 접두사를 공개 경로에서 404 로 잘라낸다 — 즉 127.0.0.1:8603 으로만
    # 닿는다. 토큰 하나로 "모든 역할을 엉뚱한 모델로" 가 가능해지는 것을 구조로 막는다.
    async def admin_list_roles(request: Request):
        _svc, err = _require_admin(request)
        if err:
            return err
        views = []
        for name in roles.role_names:
            v = roles.role_view(name)
            v["model_ready"] = scheduler.model_ready(v["model"])
            v["est_size_gb"] = roles.model_size_gb(v["model"])
            v["yaml_snippet"] = roles.yaml_snippet(name)
            views.append(v)
        return JSONResponse({
            "roles": views,
            "overrides": roles.override_summary(),
            "overridable_fields": list(OVERRIDABLE_FIELDS),
            "lanes": list(LANES),
        })

    async def admin_set_role(request: Request):
        """역할 오버라이드 저장(모델 교체 등). 없는 역할이면 신규 생성(origin=db)."""
        svc, err = _require_admin(request)
        if err:
            return err
        body = await _json_body(request)
        name = str(body.get("role") or "").strip()
        if not name:
            return JSONResponse(
                {"error": "invalid_request", "detail": "role 이 필요합니다"},
                status_code=400,
            )
        fields = body.get("fields")
        base = roles.base_role(name)
        existing = roles.role(name)
        # roles.yaml 역할이면 부분 덮어쓰기, 없는 이름이면 새 역할(model 필수)
        origin = "yaml" if base is not None else "db"
        if origin == "db" and not ROLE_NAME_RE.match(name):
            return JSONResponse(
                {"error": "invalid_role_name",
                 "detail": "새 역할 이름은 소문자·숫자·밑줄 2~32자여야 합니다"},
                status_code=400,
            )
        if origin == "db" and existing is not None and not roles.is_db_role(name):
            # 이론상 도달 불가지만, 이름 충돌로 YAML 역할을 덮어쓰는 일은 막는다
            return JSONResponse({"error": "conflict", "role": name}, status_code=409)
        try:
            clean = validate_role_fields(
                fields, require_model=(origin == "db" and existing is None),
                allow_kind=(origin == "db"),
            )
            if origin == "db" and existing is not None:
                # kind 는 생성 시에만 정한다 — 나중에 generate→embed 로 바뀌면
                # 그 역할이 큐와 메모리 예산을 우회하는 동기 경로로 넘어간다.
                clean.pop("kind", None)
        except ConfigError as exc:
            return JSONResponse(
                {"error": "invalid_fields", "detail": str(exc)}, status_code=400
            )

        # **부분 수정은 병합이다.** 보낸 필드만 저장하면 model 을 바꾼 뒤 timeout 만
        # 다시 바꿨을 때 모델 교체가 조용히 사라진다.
        prev = next((o for o in store.list_role_overrides() if o["role"] == name), None)
        merged = dict((prev or {}).get("fields") or {})
        merged.update(clean)

        if origin == "yaml":
            # 기본값과 같아진 필드는 오버라이드가 아니다. 남겨 두면
            # overridden_fields 와 드리프트 배지가 "바뀐 게 있다"고 거짓말한다.
            merged = {k: v for k, v in merged.items()
                      if v != getattr(base, k, object())}

        if origin == "yaml" and not merged:
            # 전부 기본값으로 되돌아왔다 = 오버라이드 없음
            store.delete_role_override(name)
        else:
            store.set_role_override(
                name, origin=origin, fields=merged,
                note=(str(body.get("note")) if body.get("note") else None),
                updated_by=svc.name,
            )
        clean = merged
        invalid = _reload_overrides()
        view = roles.role_view(name)
        store.record_audit(actor=svc.name, action="role_override", target=name,
                           detail={"origin": origin, "fields": clean})
        log.info("역할 오버라이드 %s ← %s (by %s)", name, clean, svc.name)
        out = {"role": view, "overrides": roles.override_summary(),
               "invalid": invalid}
        # 바꿨는데 아무 일도 안 일어나는 상황을 막는다 — 미설치면 UI 가 설치를 권한다
        if view and not scheduler.model_ready(view["model"]):
            out["install_required"] = {
                "model": view["model"],
                "est_size_gb": roles.model_size_gb(view["model"]),
            }
        return JSONResponse(out)

    async def admin_revert_role(request: Request):
        """오버라이드 제거 → roles.yaml 기본값 복귀. db 역할이면 역할 자체가 사라진다."""
        svc, err = _require_admin(request)
        if err:
            return err
        name = str(request.query_params.get("role") or "").strip()
        if not roles.has_override(name):
            return JSONResponse(
                {"error": "not_found", "detail": "오버라이드가 없습니다", "role": name},
                status_code=404,
            )
        was_db = roles.is_db_role(name)
        store.delete_role_override(name)
        _reload_overrides()
        store.record_audit(actor=svc.name,
                           action="role_delete" if was_db else "role_revert",
                           target=name, detail={"origin": "db" if was_db else "yaml"})
        log.info("역할 오버라이드 해제 %s (by %s)", name, svc.name)
        return JSONResponse({
            "role": roles.role_view(name),
            "removed": was_db,
            "overrides": roles.override_summary(),
        })

    def _model_delete_blockers(model: str) -> list[dict]:
        """지우면 무언가 깨지는 이유들. 비어 있어야 삭제할 수 있다.

        **force 플래그를 두지 않는다.** 역할이 그 모델을 가리키는 채로 지우면
        다음 _models_loop 틱이 곧바로 설치 요청을 다시 만들고 Slack 을 울린다.
        순서는 "역할을 먼저 바꾸고 → 지운다" 여야 한다.
        """
        out: list[dict] = []
        using = roles.roles_using(model, loose=True)
        if using:
            embeds = [n for n in using
                      if (r := roles.role(n)) is not None and r.kind == "embed"]
            out.append({
                "kind": "roles", "detail": using,
                "message": f"이 모델을 쓰는 역할이 있습니다: {', '.join(using)}. "
                           "먼저 역할의 모델을 바꾸세요.",
                # 임베딩 역할은 잡 큐가 아니라 동기 경로다 — 지우는 순간 소비자가
                # pending 이 아니라 **즉시 503** 을 받는다. 따로 경고한다.
                "embed_roles": embeds,
            })
        queued = {m: c for m, c in store.queued_job_models().items()
                  if model_matches(model, m)}
        if queued:
            out.append({"kind": "queued_jobs", "detail": queued,
                        "message": f"대기 중인 잡 {sum(queued.values())}건이 이 모델을 씁니다."})
        running = [lane for lane, v in scheduler.running_snapshot().items()
                   if model_matches(model, v["model"])]
        if running:
            out.append({"kind": "running_jobs", "detail": running,
                        "message": f"지금 실행 중입니다({', '.join(running)} 레인)."})
        req = store.get_model_request(model)
        if req and req["status"] in (MR_APPROVED, MR_PULLING):
            out.append({"kind": "installing", "detail": req["status"],
                        "message": "설치가 진행 중입니다. 끝난 뒤에 지우세요."})
        return out

    async def admin_list_models(request: Request):
        """맥에 설치된 모델 + 크기 + 쓰는 역할 + 최근 사용 + 삭제 가능 여부."""
        _svc, err = _require_admin(request)
        if err:
            return err
        try:
            detail = await client.tags_detailed()
            online, backend_error = True, None
        except BackendError as exc:
            detail, online, backend_error = scheduler.models_detail, False, str(exc)

        usage = {u["model"]: u for u in store.usage_by_model()}
        items = []
        for d in detail:
            name = d["name"]
            u = next((v for k, v in usage.items() if model_matches(name, k)), None)
            items.append({
                **d,
                "roles": roles.roles_using(name, loose=True),
                "est_size_gb": roles.model_size_gb(name),
                "calls_30d": (u or {}).get("calls", 0),
                "last_used": (u or {}).get("last_used"),
                "blockers": _model_delete_blockers(name),
            })
        items.sort(key=lambda i: (i["size_gb"] or 0), reverse=True)
        total = sum(i["size_gb"] or 0 for i in items)
        return JSONResponse({
            "models": items,
            "total_size_gb": round(total, 2),
            "backend": {"online": online, "error": backend_error,
                        "base_url": roles.backend.base_url},
            "usage_by_model": store.usage_by_model(),
            "usage_days": 30,
        })

    async def admin_delete_model(request: Request):
        svc, err = _require_admin(request)
        if err:
            return err
        model = str(request.query_params.get("model") or "").strip()
        if not model:
            return JSONResponse(
                {"error": "invalid_request", "detail": "model 이 필요합니다"},
                status_code=400,
            )
        blockers = _model_delete_blockers(model)
        if blockers:
            store.record_audit(actor=svc.name, action="model_delete", target=model,
                               detail={"blockers": blockers}, outcome="blocked")
            return JSONResponse(
                {"error": "in_use", "blockers": blockers,
                 "detail": "; ".join(b["message"] for b in blockers)},
                status_code=409,
            )
        freed = next((d["size_gb"] for d in scheduler.models_detail
                      if model_matches(model, d["name"])), None)
        try:
            existed = await client.delete(model)
        except BackendError as exc:
            store.record_audit(actor=svc.name, action="model_delete", target=model,
                               detail={"error": str(exc)}, outcome="failed")
            return JSONResponse(
                {"error": "backend_error", "detail": str(exc),
                 "retryable": exc.retryable},
                status_code=503 if exc.retryable else 502,
            )

        # 설치 요청 행을 남기면 _triage_missing 이 ready→pending 으로 되살리고
        # Slack 을 쏜다. rejected 도 답이 아니다(이후 잡이 거짓 사유로 하드 실패).
        store.delete_model_request(model)
        scheduler.forget_model(model)          # /v1/status 가 30초간 거짓말하지 않도록
        scheduler.notifier.forget(f"model:{model}")   # 재설치 때 알림이 억제되지 않도록
        await scheduler.notifier.model_deleted(model, freed_gb=freed, actor=svc.name)
        store.record_audit(actor=svc.name, action="model_delete", target=model,
                           detail={"existed": existed, "freed_gb": freed})
        log.info("모델 삭제 %s (by %s, ~%sGB)", model, svc.name, freed)
        return JSONResponse({"status": "deleted", "model": model,
                             "existed": existed, "freed_gb": freed})

    async def admin_catalog(request: Request):
        _svc, err = _require_admin(request)
        if err:
            return err
        _sync_catalog_sizes()
        found = catalog.search(request.query_params.get("q") or "",
                               kind=request.query_params.get("kind"))
        installed = scheduler.available_models or []
        return JSONResponse({
            "models": found,
            "installed": installed,
            "mem_budget_gb": roles.mem_budget_gb,
            "error": catalog.error,
        })

    async def admin_install_model(request: Request):
        """운영자가 먼저 설치한다(지금까지는 외부 서비스 요청 때만 가능했다).

        새 pull 코드를 만들지 않는다 — 기존 승인 파이프라인에 approved 로 밀어
        넣으면 _models_loop 이 2초 안에 집어간다. 동시성·진행률·실패 처리 그대로.
        """
        svc, err = _require_admin(request)
        if err:
            return err
        body = await _json_body(request)
        try:
            model = validate_model_name(body.get("model"))
        except ConfigError as exc:
            return JSONResponse(
                {"error": "invalid_model", "detail": str(exc)}, status_code=400
            )

        _sync_catalog_sizes()
        # 크기 게이트: _pick 이 사후에 잡을 몰살하는 대신 **설치 전에** 거부한다.
        # 크기를 모르는 모델은 20GB 로 가정되므로 이 문이 없으면 임의 설치가
        # 곧바로 사고가 된다.
        est = roles.model_size_gb(model)
        if est > roles.mem_budget_gb:
            return JSONResponse({
                "error": "too_large", "est_size_gb": est,
                "mem_budget_gb": roles.mem_budget_gb,
                "detail": f"추정 {est}GB 는 메모리 예산 {roles.mem_budget_gb}GB 를 "
                          "넘어 설치해도 실행할 수 없습니다.",
            }, status_code=400)

        if scheduler.model_ready(model):
            return JSONResponse({"status": "already_installed", "model": model})
        req = store.get_model_request(model)
        if req and req["status"] in (MR_APPROVED, MR_PULLING):
            return JSONResponse(
                {"error": "in_progress", "request": req}, status_code=409
            )
        store.ensure_model_request(
            model, requested_by=svc.name, roles=roles.roles_using(model, loose=True),
            est_size_gb=est,
        )
        store.set_model_request_size(model, est)
        store.set_model_request_status(model, MR_APPROVED, error="", progress=0)
        store.record_audit(actor=svc.name, action="model_install", target=model,
                           detail={"est_size_gb": est})
        log.info("모델 설치 요청 %s (~%sGB, by %s)", model, est, svc.name)
        return JSONResponse({"status": "approved",
                             "request": store.get_model_request(model)})

    async def admin_audit(request: Request):
        _svc, err = _require_admin(request)
        if err:
            return err
        try:
            limit = int(request.query_params.get("limit", 50))
        except ValueError:
            limit = 50
        return JSONResponse({"audit": store.list_audit(limit)})

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
            # 0 보다 크면 프로덕션이 roles.yaml 과 다르다는 뜻 — 대시보드가 배너를 띄운다.
            # 이게 없으면 반년 뒤 레포와 실제 동작이 왜 다른지 아무도 설명 못 한다.
            "overrides": roles.override_summary(),
            "usage": store.usage_summary(),
            "model_requests": {
                "pending": len(store.list_model_requests(MR_PENDING)),
                "pulling": scheduler.pulling_snapshot(),
            },
        })

    routes = [
        Route("/healthz", healthz),
        Route("/v1/generate", generate, methods=["POST"]),
        Route("/v1/embed", embed, methods=["POST"]),
        Route("/v1/jobs", list_jobs, methods=["GET"]),
        Route("/v1/jobs/{job_id}", get_job, methods=["GET"]),
        Route("/v1/jobs/{job_id}", cancel_job, methods=["DELETE"]),
        Route("/v1/roles", list_roles),
        Route("/v1/status", status),
        Route("/v1/models/requests", list_model_requests, methods=["GET"]),
        Route("/v1/models/requests", decide_model_request, methods=["POST"]),
        Route("/v1/integration", integration_doc, methods=["GET"]),
        # 관리 전용 — Caddy 가 공개 경로에서 404 로 잘라낸다(deploy/Caddyfile)
        Route("/v1/admin/roles", admin_list_roles, methods=["GET"]),
        Route("/v1/admin/roles", admin_set_role, methods=["POST"]),
        Route("/v1/admin/roles", admin_revert_role, methods=["DELETE"]),
        Route("/v1/admin/models", admin_list_models, methods=["GET"]),
        Route("/v1/admin/models", admin_delete_model, methods=["DELETE"]),
        Route("/v1/admin/models/install", admin_install_model, methods=["POST"]),
        Route("/v1/admin/catalog", admin_catalog, methods=["GET"]),
        Route("/v1/admin/audit", admin_audit, methods=["GET"]),
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
