"""조회 전용 모니터링 대시보드 (멀티 페이지).

MCP 서버와 같은 프로세스에 라우트로 마운트되어 sysinfo/service_ops/jobs/audit
모듈을 재사용한다. 브라우저 접근은 별도 비밀번호 로그인(세션 쿠키)으로 보호되며,
이는 MCP Bearer 토큰과 완전히 분리된 인증 경계다.

기본 대시보드 외에 데일리 브리핑·Docker·날씨 등 개인 기능 페이지를 제공한다.
새 기능 데이터는 여기 /api/* 로 추가하고 static/pages/ 에 페이지를 붙인다.
"""

from __future__ import annotations

import hmac
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
from starlette.concurrency import run_in_threadpool
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

# 데일리 브리핑 디렉터리 (Claude 가 MCP write_file 로 날짜별 파일을 쓴다).
# 파일명은 날짜(YYYY-MM-DD.html / .md). 예: html/morning-brief/2026-07-23.html
BRIEFING_DIR = os.environ.get("HOSUB_BRIEFING_DIR", "html/morning-brief")

_BRIEFING_EXTS = (".html", ".htm", ".md")
_SCRIPT_RE = re.compile(r"(?is)<script.*?</script>")


def _list_briefings() -> list[tuple[str, Path]]:
    """브리핑 파일을 (이름, 경로) 목록으로, 이름 내림차순(최신 먼저) 반환."""
    d = Path(BRIEFING_DIR)
    if not d.is_dir():
        return []
    items = [
        (f.stem, f)
        for f in d.iterdir()
        if f.is_file() and f.suffix.lower() in _BRIEFING_EXTS
    ]
    items.sort(key=lambda x: x[0], reverse=True)
    return items
# 날씨 위치 (기본 서울). "위도,경도" 형식으로 HOSUB_WEATHER_LATLON 설정 가능.
_WEATHER_LATLON = os.environ.get("HOSUB_WEATHER_LATLON", "37.5665,126.9780")
WEATHER_LABEL = os.environ.get("HOSUB_WEATHER_LABEL", "서울")


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
        return Response(status_code=204)

    async def api_status(request):
        if (d := _require_auth_json(request)):
            return d
        return JSONResponse(sysinfo.collect_status())

    async def api_services(request):
        if (d := _require_auth_json(request)):
            return d
        return JSONResponse({"services": service_ops.query_all(ctx.runner, ctx.registry)})

    async def api_jobs(request):
        if (d := _require_auth_json(request)):
            return d
        limit = _int_param(request, "limit", 10)
        return JSONResponse({"jobs": [j.to_dict() for j in ctx.jobs.list(limit)]})

    async def api_audit(request):
        if (d := _require_auth_json(request)):
            return d
        limit = _int_param(request, "limit", 50)
        return JSONResponse({"audit": ctx.audit.recent(limit)})

    async def api_briefing(request):
        if (d := _require_auth_json(request)):
            return d
        items = _list_briefings()
        dates = [name for name, _ in items]
        if not items:
            return JSONResponse(
                {
                    "exists": False,
                    "dates": [],
                    "content": "",
                    "hint": f"Claude 가 write_file 로 {BRIEFING_DIR}/<날짜>.html 에 브리핑을 "
                    "쓰면 여기에 표시됩니다.",
                }
            )
        # 요청한 날짜(있으면) 또는 최신
        want = request.query_params.get("date")
        chosen = next((p for name, p in items if name == want), items[0][1])
        try:
            raw = chosen.read_text(encoding="utf-8")[:200_000]
            mtime = chosen.stat().st_mtime
        except OSError as exc:
            return JSONResponse({"exists": False, "dates": dates, "content": "", "error": str(exc)})
        fmt = "md" if chosen.suffix.lower() == ".md" else "html"
        content = raw if fmt == "md" else _SCRIPT_RE.sub("", raw)  # HTML 은 script 제거
        return JSONResponse(
            {
                "exists": True,
                "date": chosen.stem,
                "dates": dates,
                "format": fmt,
                "content": content,
                "updated_at": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
            }
        )

    async def api_docker(request):
        if (d := _require_auth_json(request)):
            return d
        # docker ps 를 러너로 실행 (블로킹이므로 스레드풀)
        fmt = (
            '{"id":"{{.ID}}","name":"{{.Names}}","image":"{{.Image}}",'
            '"status":"{{.Status}}","state":"{{.State}}","ports":"{{.Ports}}"}'
        )
        res = await run_in_threadpool(
            ctx.runner.run,
            ["docker", "ps", "-a", "--no-trunc", "--format", fmt],
            timeout=15,
        )
        if not res.ok:
            return JSONResponse(
                {
                    "ok": False,
                    "containers": [],
                    "error": (res.stderr or res.stdout or "docker 실행 실패").strip(),
                }
            )
        containers = []
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                containers.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return JSONResponse({"ok": True, "containers": containers})

    async def api_weather(request):
        if (d := _require_auth_json(request)):
            return d
        try:
            lat, lon = _WEATHER_LATLON.split(",")
            url = (
                "https://api.open-meteo.com/v1/forecast"
                f"?latitude={lat.strip()}&longitude={lon.strip()}"
                "&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m,apparent_temperature"
                "&daily=temperature_2m_max,temperature_2m_min,weather_code"
                "&timezone=auto&forecast_days=4"
            )
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(url)
                r.raise_for_status()
                data = r.json()
        except Exception as exc:  # 네트워크/파싱 실패 시 graceful
            return JSONResponse({"ok": False, "label": WEATHER_LABEL, "error": str(exc)})
        return JSONResponse({"ok": True, "label": WEATHER_LABEL, "data": data})

    async def static_file(request):
        # 로그인 페이지 자산(스타일·vendor 라이브러리)은 인증 전에도 필요하므로 공개.
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
        Route("/api/briefing", api_briefing),
        Route("/api/docker", api_docker),
        Route("/api/weather", api_weather),
        Route("/static/{path:path}", static_file),
    ]


_PUBLIC_ASSETS = {"style.css", "login.js"}


def _int_param(request, name: str, default: int) -> int:
    try:
        return int(request.query_params.get(name, default))
    except (TypeError, ValueError):
        return default
