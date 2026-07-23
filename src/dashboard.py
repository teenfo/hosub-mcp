"""조회 전용 모니터링 대시보드.

MCP 서버와 같은 프로세스에 라우트로 마운트되어 sysinfo/service_ops/jobs/audit
모듈을 재사용한다. 브라우저 접근은 별도 비밀번호 로그인(세션 쿠키)으로 보호되며,
이는 MCP Bearer 토큰과 완전히 분리된 인증 경계다.
"""

from __future__ import annotations

import hmac
from pathlib import Path

from starlette.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Route

from . import service_ops, sysinfo
from .context import AppContext

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

_SESSION_KEY = "dash_auth"


def _is_authed(request) -> bool:
    return bool(request.session.get(_SESSION_KEY))


def _require_auth_json(request):
    if not _is_authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return None


def build_routes(ctx: AppContext, password: str) -> list[Route]:
    async def index(request):
        if not _is_authed(request):
            return RedirectResponse("/login", status_code=302)
        return FileResponse(STATIC_DIR / "index.html")

    async def login_page(request):
        return FileResponse(STATIC_DIR / "login.html")

    async def login_submit(request):
        form = await request.form()
        supplied = str(form.get("password", ""))
        if password and hmac.compare_digest(supplied, password):
            request.session[_SESSION_KEY] = True
            return RedirectResponse("/", status_code=302)
        return FileResponse(
            STATIC_DIR / "login.html", status_code=401, headers={"X-Login-Failed": "1"}
        )

    async def logout(request):
        request.session.clear()
        return RedirectResponse("/login", status_code=302)

    async def favicon(request):
        # 브라우저 자동 요청. 매칭 라우트가 없으면 MCP 마운트로 흘러가 401 이 되므로
        # 여기서 흡수한다.
        return Response(status_code=204)

    async def api_status(request):
        denied = _require_auth_json(request)
        if denied:
            return denied
        return JSONResponse(sysinfo.collect_status())

    async def api_services(request):
        denied = _require_auth_json(request)
        if denied:
            return denied
        return JSONResponse({"services": service_ops.query_all(ctx.runner, ctx.registry)})

    async def api_jobs(request):
        denied = _require_auth_json(request)
        if denied:
            return denied
        limit = _int_param(request, "limit", 10)
        return JSONResponse({"jobs": [j.to_dict() for j in ctx.jobs.list(limit)]})

    async def api_audit(request):
        denied = _require_auth_json(request)
        if denied:
            return denied
        limit = _int_param(request, "limit", 50)
        return JSONResponse({"audit": ctx.audit.recent(limit)})

    async def static_file(request):
        # 로그인 페이지 자산(스타일·vendor 라이브러리)은 인증 전에도 필요하므로 공개.
        # 그 외 자산(app.js, panels/*)은 로그인 후 로드되므로 세션 필요.
        name = request.path_params["path"]
        public = name in _PUBLIC_ASSETS or name.startswith("vendor/")
        if not _is_authed(request) and not public:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        target = (STATIC_DIR / name).resolve()
        if STATIC_DIR not in target.parents or not target.is_file():
            return Response(status_code=404)
        return FileResponse(target)

    return [
        Route("/", index),
        Route("/login", login_page, methods=["GET"]),
        Route("/login", login_submit, methods=["POST"]),
        Route("/logout", logout),
        Route("/favicon.ico", favicon),
        Route("/api/status", api_status),
        Route("/api/services", api_services),
        Route("/api/jobs", api_jobs),
        Route("/api/audit", api_audit),
        Route("/static/{path:path}", static_file),
    ]


_PUBLIC_ASSETS = {"style.css", "login.js"}


def _int_param(request, name: str, default: int) -> int:
    try:
        return int(request.query_params.get(name, default))
    except (TypeError, ValueError):
        return default
