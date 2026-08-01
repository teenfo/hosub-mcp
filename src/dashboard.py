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

from . import gateway, jw, service_ops, sysinfo
from .context import AppContext

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

_SESSION_KEY = "dash_auth"

# 날짜별 문서 디렉터리 (Claude 가 MCP write_file 로 YYYY-MM-DD.html/.md 를 쓴다).
# 데일리 브리핑과 야간 분석 리포트는 별개 자료이므로 디렉터리를 분리한다.
BRIEFING_DIR = os.environ.get("HOSUB_BRIEFING_DIR", "html/morning-brief")
NIGHT_REPORT_DIR = os.environ.get("HOSUB_NIGHT_REPORT_DIR", "html/night-report")

_BRIEFING_EXTS = (".html", ".htm", ".md")
_SCRIPT_RE = re.compile(r"(?is)<script.*?</script>")


def _list_dated(dir_path: str) -> list[tuple[str, Path]]:
    """날짜 문서를 (이름, 경로) 목록으로, 이름 내림차순(최신 먼저) 반환."""
    d = Path(dir_path)
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

# 트레이딩 서비스(trading/) 프록시. 같은 호스트의 별도 프로세스로 떠 있고,
# 대시보드 세션 인증을 통과한 요청만 내부 토큰을 붙여 전달한다.
# HOSUB_TRADING_TOKEN 은 trading 쪽 INTERNAL_TOKEN 과 같은 값이어야 한다.
TRADING_URL = os.environ.get("HOSUB_TRADING_URL", "http://127.0.0.1:8600")
TRADING_TOKEN = os.environ.get("HOSUB_TRADING_TOKEN", "")

_TRADING_GET_RE = re.compile(
    r"^(status|signals|orders|settings|account|account/realized"
    r"|scanner|nightly|watchlist"
    r"|prices|rules|performance|risk|journal|journal/history"
    r"|bars/\d{6}(/dates)?|trades/\d{6}|backtest/\d{6}|stock/\d{6}"
    r"|backtest/coverage|backtest/report/(latest|history)|backtest/sweep/latest"
    r"|research/(event-study|ranking|news-impact|trailing|trailing/real|timeofday)|scout|regime/history"
    r"|cases|premarket|flows|kelly|profile|vpin)$"
)
_TRADING_POST_RE = re.compile(
    r"^(orders/[0-9a-f]{12}/(approve|reject)|settings|watchlist(/remove|/mode)?"
    r"|rules/[a-z_]{1,30}/toggle"
    r"|nightly/run|symbols/refresh|backtest/report/run|backtest/sweep/run|risk"
    r"|journal/run|guard/override(/clear)?"
    r"|research/(event-study|ranking|news-impact|trailing|timeofday)/run"
    r"|scout/(mode|run)|account/reconcile"
    r"|positions/[0-9a-f]{12}/(close|void)|desk|cases/build|flows/run"
    r"|bars-obs/run)$"
)
# 백테스트는 오래 걸린다(전 종목 리포트 수십 초, 종목별 재생도 봉이 쌓일수록·
# 스윕과 겹칠수록 수 초~수십 초) — 이 경로들만 프록시 타임아웃을 늘린다.
_TRADING_SLOW_RE = re.compile(
    r"^(backtest/(report/run|\d{6})|journal/run|cases/build|flows/run"
    r"|research/(event-study|ranking|news-impact|trailing|timeofday)/run)$")

# TNM(뉴스·공시 모니터링) 서비스 프록시 — trading 과 동일 패턴.
TNM_URL = os.environ.get("HOSUB_TNM_URL", "http://127.0.0.1:8602")
TNM_TOKEN = os.environ.get("HOSUB_TNM_TOKEN", "")

_TNM_GET_RE = re.compile(
    r"^(status|items|items/\d+|watch|settings|metrics|brokers|reports)$"
)
# 증권사명은 한글·영문이 섞이고 공백도 들어간다(예: '유안타 리서치'). 화면이
# encodeURIComponent 로 보내고 starlette 가 풀어 주므로 여기서는 '/' 만 막는다.
_TNM_POST_RE = re.compile(
    r"^(items/\d+/label|watch|watch/sync|watch/\d{6}/(exclude|include|settings)"
    r"|settings|collect/run|notify/test|reclassify_failed"
    r"|brokers|brokers/[^/]+|brokers/[^/]+/delete|research/run)$"
)
# 리서치 1회 수집은 목록 9콜 + 상세 최대 20콜 + 해외 24콜이라 기본 15초를 넘긴다.
_TNM_SLOW_RE = re.compile(r"^(research/run|collect/run)$")


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
        return FileResponse(STATIC_DIR / "index.html", headers=_NO_CACHE)

    async def login_page(request):
        return FileResponse(STATIC_DIR / "login.html", headers=_NO_CACHE)

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

    def _dated_response(dir_path: str, want: str | None, label: str):
        items = _list_dated(dir_path)
        dates = [name for name, _ in items]
        if not items:
            return JSONResponse({
                "exists": False, "dates": [], "content": "",
                "hint": f"Claude 가 write_file 로 {dir_path}/<날짜>.html 에 {label} 를 "
                "쓰면 여기에 표시됩니다.",
            })
        chosen = next((p for name, p in items if name == want), items[0][1])
        try:
            raw = chosen.read_text(encoding="utf-8")[:200_000]
            mtime = chosen.stat().st_mtime
        except OSError as exc:
            return JSONResponse({"exists": False, "dates": dates, "content": "", "error": str(exc)})
        fmt = "md" if chosen.suffix.lower() == ".md" else "html"
        content = raw if fmt == "md" else _SCRIPT_RE.sub("", raw)  # HTML 은 script 제거
        return JSONResponse({
            "exists": True, "date": chosen.stem, "dates": dates, "format": fmt,
            "content": content,
            "updated_at": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
        })

    async def api_briefing(request):
        if (d := _require_auth_json(request)):
            return d
        return _dated_response(BRIEFING_DIR, request.query_params.get("date"), "브리핑")

    async def api_night_report(request):
        if (d := _require_auth_json(request)):
            return d
        return _dated_response(NIGHT_REPORT_DIR, request.query_params.get("date"), "야간 분석 리포트")

    async def api_llm_status(request):
        """게이트웨이 상태 — 백엔드·보유 모델·레인 큐·메모리 예산·사용량."""
        if (d := _require_auth_json(request)):
            return d
        # 게이트웨이 HTTP 조회는 블로킹이라 스레드풀로
        return JSONResponse(await run_in_threadpool(gateway.status))

    async def api_llm_generate(request):
        if (d := _require_auth_json(request)):
            return d
        try:
            body = await request.json()
        except Exception:
            body = {}
        prompt = (body.get("prompt") or "").strip()
        role = body.get("role") or "general"
        if not prompt:
            return JSONResponse({"status": "rejected", "reason": "prompt 가 비어 있습니다."},
                                status_code=400)
        # system 을 그대로 넘긴다(프롬프트는 호출자 소유).
        # ⚠️ 게이트웨이는 system 이 **None 일 때만** 역할 기본값을 쓴다 — 빈 문자열은
        #    "시스템 프롬프트 없음" 이라는 다른 의미다. 프런트가 키를 생략하면
        #    역할 기본값, ""를 보내면 의도적으로 비우기.
        system = body.get("system")
        if system is not None and not isinstance(system, str):
            system = None
        result = await run_in_threadpool(gateway.generate, role, prompt, system=system)
        ctx.audit.log(
            tool="llm_generate", params={"role": role, "prompt_chars": len(prompt),
                                         "system": bool(system), "via": "dashboard"},
            risk="low", outcome=result.get("status", "error"),
            result_summary=(result.get("response") or result.get("error") or "")[:300],
        )
        return JSONResponse(result)

    async def api_llm_job(request):
        """pending 으로 돌아온 생성 요청의 결과 수령."""
        if (d := _require_auth_json(request)):
            return d
        return JSONResponse(
            await run_in_threadpool(gateway.get_job, request.path_params["job_id"])
        )

    async def api_llm_models(request):
        """게이트웨이의 모델 설치 요청 목록 (승인 대기 포함)."""
        if (d := _require_auth_json(request)):
            return d
        result = await run_in_threadpool(
            gateway.list_model_requests, request.query_params.get("status")
        )
        return JSONResponse(result)

    async def api_llm_models_decide(request):
        """모델 설치 승인/거부 — 조회 전용 대시보드의 의도된 예외.

        경계는 세 겹이다: (1) 게이트웨이가 admin 토큰(hosub)만 통과시키고,
        (2) 관리 계열 경로는 Caddy 가 공개 노출에서 잘라내며,
        (3) 모든 변경이 대시보드 감사와 게이트웨이 admin_audit 양쪽에 남는다.
        """
        if (d := _require_auth_json(request)):
            return d
        try:
            body = await request.json()
        except Exception:
            body = {}
        model = (body.get("model") or "").strip()
        action = (body.get("action") or "").strip()
        if not model:
            return JSONResponse({"status": "rejected", "reason": "model 이 필요합니다."},
                                status_code=400)
        result = await run_in_threadpool(gateway.decide_model_request, model, action)
        ctx.audit.log(
            tool="llm_decide_model",
            params={"model": model, "action": action, "via": "dashboard"},
            risk="medium", outcome=result.get("status", "error"),
            result_summary=str(result.get("request") or result.get("error") or "")[:300],
        )
        return JSONResponse(result)

    # --- 역할 운영 (게이트웨이 /v1/admin/roles) ---
    async def api_llm_roles(request):
        """역할별 유효값 + roles.yaml 기본값 대비 차이 + 붙여 넣을 스니펫."""
        if (d := _require_auth_json(request)):
            return d
        return JSONResponse(await run_in_threadpool(gateway.list_admin_roles))

    async def api_llm_role_save(request):
        if (d := _require_auth_json(request)):
            return d
        try:
            body = await request.json()
        except Exception:
            body = {}
        role = (body.get("role") or "").strip()
        fields = body.get("fields")
        if not role or not isinstance(fields, dict) or not fields:
            return JSONResponse(
                {"status": "rejected", "reason": "role 과 fields 가 필요합니다."},
                status_code=400)
        result = await run_in_threadpool(
            gateway.set_role_override, role, fields, note=body.get("note"))
        ctx.audit.log(
            tool="llm_set_role", params={"role": role, "fields": fields,
                                         "via": "dashboard"},
            # 새 역할 생성은 계약 추가라 등급을 올린다
            risk="high" if result.get("role", {}).get("origin") == "db" else "medium",
            outcome=result.get("status", "error"),
            result_summary=str(result.get("detail") or result.get("error") or "")[:300],
        )
        return JSONResponse(result)

    async def api_llm_role_revert(request):
        """오버라이드 해제 — DB 전용 역할이면 역할 자체가 사라진다(high)."""
        if (d := _require_auth_json(request)):
            return d
        role = (request.query_params.get("role") or "").strip()
        if not role:
            return JSONResponse({"status": "rejected", "reason": "role 이 필요합니다."},
                                status_code=400)
        result = await run_in_threadpool(gateway.revert_role, role)
        ctx.audit.log(
            tool="llm_revert_role", params={"role": role, "via": "dashboard"},
            risk="high" if result.get("removed") else "medium",
            outcome=result.get("status", "error"),
            result_summary=str(result.get("error") or "")[:300],
        )
        return JSONResponse(result)

    # --- 모델 A/B 비교 ---
    async def api_llm_compare(request):
        if (d := _require_auth_json(request)):
            return d
        try:
            body = await request.json()
        except Exception:
            body = {}
        prompt = (body.get("prompt") or "").strip()
        models = body.get("models")
        if not prompt or not isinstance(models, list) or len(models) != 2:
            return JSONResponse(
                {"status": "rejected", "reason": "prompt 와 모델 2개가 필요합니다."},
                status_code=400)
        result = await run_in_threadpool(
            gateway.compare_models, prompt, models, system=body.get("system"))
        ctx.audit.log(
            tool="llm_compare", params={"models": models, "via": "dashboard"},
            risk="low", outcome=result.get("status", "error"),
            result_summary=str(result.get("detail") or result.get("error") or "")[:300],
        )
        return JSONResponse(result)

    async def api_llm_compare_get(request):
        if (d := _require_auth_json(request)):
            return d
        return JSONResponse(await run_in_threadpool(
            gateway.get_comparison, request.path_params["run_id"]))

    async def api_llm_integration(request):
        """소비자용 통합 가이드 원문.

        브라우저가 게이트웨이를 직접 부르면 소비자 토큰이 노출되므로 여기서
        프록시한다(게이트웨이의 /v1/integration 은 인증을 요구한다).
        """
        if (d := _require_auth_json(request)):
            return d
        return JSONResponse(await run_in_threadpool(gateway.integration_doc))

    async def api_llm_meta(request):
        """기계가 읽는 계약 — 역할·한도·오류코드·클라이언트 해시.

        화면에서 값어치 있는 것은 client.files.python 의 sha256 이다. 정본
        (llm-gateway/client/llmgw.py)과 사본 둘(trading·tnm)을 비교하는 장치가
        레포에 없으므로, 이 해시가 사본이 뒤처졌는지 확인하는 유일한 관측점이다.
        """
        if (d := _require_auth_json(request)):
            return d
        return JSONResponse(await run_in_threadpool(gateway.meta))

    async def api_llm_openapi(request):
        """OpenAPI 스펙 원문(JSON·YAML). 브라우저가 Blob 으로 저장한다."""
        if (d := _require_auth_json(request)):
            return d
        fmt = request.query_params.get("fmt", "json")
        return JSONResponse(await run_in_threadpool(gateway.openapi, fmt))

    async def api_llm_client(request):
        """파이썬 클라이언트·목 서버 원본.

        integration_doc 과 같은 방식으로 문자열을 JSON 에 담아 넘긴다 — 이 모듈의
        게이트웨이 호출은 전부 dict 를 돌려준다는 규약(src/gateway.py 맨 위)을
        지키려면 이 길뿐이다. 저장은 브라우저에서 Blob 으로 한다.
        """
        if (d := _require_auth_json(request)):
            return d
        name = request.query_params.get("name", "llmgw.py")
        return JSONResponse(await run_in_threadpool(gateway.client_file, name))

    # --- 소비자 토큰 (게이트웨이 /v1/admin/services) ---
    async def api_llm_services(request):
        """등록된 소비자와 토큰 상태. 값은 마스킹된 것만 온다."""
        if (d := _require_auth_json(request)):
            return d
        try:
            days = int(request.query_params.get("days", 7))
        except ValueError:
            days = 7
        return JSONResponse(await run_in_threadpool(gateway.list_services, days))

    async def api_llm_token_reveal(request):
        """소비자 토큰 전체 값 1건.

        ⚠️ 조회 전용 대시보드가 **비밀을 화면에 내보내는 유일한 지점**이다.
        이전 원칙("토큰 값은 표시하지 않는다", PR #219)을 뒤집는 것이므로 경계를
        좁게 잡는다: 한 번에 한 서비스만, POST 로만(링크·프리페치로 새지 않게),
        그리고 누가 언제 무엇을 열었는지 대시보드 감사와 게이트웨이 admin_audit
        양쪽에 남는다. 근거는 docs/requests/llm-gateway-service.md 7-4절.
        """
        if (d := _require_auth_json(request)):
            return d
        try:
            body = await request.json()
        except Exception:
            body = {}
        name = (body.get("service") or "").strip()
        if not name:
            return JSONResponse({"status": "rejected", "reason": "service 가 필요합니다."},
                                status_code=400)
        result = await run_in_threadpool(gateway.reveal_token, name)
        ctx.audit.log(
            tool="llm_token_reveal",
            params={"service": name, "via": "dashboard"},
            risk="high",
            outcome=result.get("status", "error"),
            # 값은 절대 남기지 않는다 — 감사 로그가 비밀 저장소가 되면 안 된다
            result_summary=str(result.get("error") or "revealed")[:200],
        )
        return JSONResponse(result)

    # --- jw-mcp (별개 프로세스, 127.0.0.1:8604) ---
    #
    # jw-mcp 의 /api/dash/* 는 루프백 전용이다 — Caddy 가 /jw/* 만 공개하고
    # 그 경로는 라우팅하지 않는다(공인에서 404). 브라우저가 직접 못 부르므로
    # 여기서 서버사이드로 프록시한다. trading/tnm 과 같은 구조다.
    async def api_jw_summary(request):
        if (d := _require_auth_json(request)):
            return d
        return JSONResponse(await run_in_threadpool(jw.summary))

    async def api_jw_users(request):
        if (d := _require_auth_json(request)):
            return d
        return JSONResponse(await run_in_threadpool(jw.users))

    async def api_jw_usage(request):
        if (d := _require_auth_json(request)):
            return d
        try:
            days = int(request.query_params.get("days", 7))
        except ValueError:
            days = 7
        return JSONResponse(await run_in_threadpool(jw.usage, days))

    async def api_jw_calls(request):
        if (d := _require_auth_json(request)):
            return d
        try:
            limit = int(request.query_params.get("limit", 50))
        except ValueError:
            limit = 50
        return JSONResponse(await run_in_threadpool(jw.calls, limit))

    async def api_jw_user_status(request):
        """사용자 승인·비활성화 — 조회 전용 대시보드의 두 번째 의도된 예외.

        경계는 세 겹이다: (1) jw-mcp 의 /api/dash/* 는 루프백 전용이라 이
        프로세스에서만 닿고, (2) 대시보드 자체가 비밀번호 세션 뒤에 있으며,
        (3) 모든 변경이 감사 로그에 남는다.

        ⚠️ active 가 아닌 값으로 바꾸면 그 사용자의 OAuth 토큰이 즉시 전부
        폐기된다 — 커넥터를 다시 연결해야 살아난다. 되돌릴 수 없는 쪽이므로
        화면에서 확인 단계를 거치고, 여기서도 감사에 risk 를 남긴다.
        """
        if (d := _require_auth_json(request)):
            return d
        try:
            body = await request.json()
        except Exception:
            body = {}
        user_id = (body.get("user_id") or "").strip()
        status = (body.get("status") or "").strip()
        if not user_id or not status:
            return JSONResponse(
                {"status": "rejected", "reason": "user_id 와 status 가 필요합니다."},
                status_code=400)
        result = await run_in_threadpool(jw.set_user_status, user_id, status)
        ctx.audit.log(
            tool="jw_set_user_status",
            # 이메일·표시이름은 남기지 않는다 — user_id 로 충분히 추적된다.
            params={"user_id": user_id, "status": status, "via": "dashboard"},
            # 토큰 폐기가 따라오고 사용자가 재연결해야 하므로 high 다.
            risk="high" if status != "active" else "medium",
            outcome=result.get("status", "error"),
            result_summary=str(result.get("error") or result.get("user") or "")[:300],
        )
        return JSONResponse(result)

    # --- 모델 운영 (게이트웨이 /v1/admin/*) ---
    async def api_llm_installed(request):
        """맥에 설치된 모델 + 용량 + 쓰는 역할 + 최근 사용 + 삭제 차단 사유."""
        if (d := _require_auth_json(request)):
            return d
        return JSONResponse(await run_in_threadpool(gateway.list_installed_models))

    async def api_llm_catalog(request):
        if (d := _require_auth_json(request)):
            return d
        return JSONResponse(await run_in_threadpool(
            gateway.search_catalog,
            request.query_params.get("q") or "",
            request.query_params.get("kind"),
        ))

    async def api_llm_model_install(request):
        if (d := _require_auth_json(request)):
            return d
        try:
            body = await request.json()
        except Exception:
            body = {}
        model = (body.get("model") or "").strip()
        if not model:
            return JSONResponse({"status": "rejected", "reason": "model 이 필요합니다."},
                                status_code=400)
        result = await run_in_threadpool(gateway.install_model, model)
        ctx.audit.log(
            tool="llm_install_model", params={"model": model, "via": "dashboard"},
            risk="medium", outcome=result.get("status", "error"),
            result_summary=str(result.get("detail") or result.get("error") or "")[:300],
        )
        return JSONResponse(result)

    async def api_llm_model_delete(request):
        """맥에서 모델을 지운다 — 되돌리려면 수십 GB 를 다시 받아야 하므로 high."""
        if (d := _require_auth_json(request)):
            return d
        model = (request.query_params.get("model") or "").strip()
        if not model:
            return JSONResponse({"status": "rejected", "reason": "model 이 필요합니다."},
                                status_code=400)
        result = await run_in_threadpool(gateway.delete_model, model)
        ctx.audit.log(
            tool="llm_delete_model", params={"model": model, "via": "dashboard"},
            risk="high", outcome=result.get("status", "error"),
            result_summary=str(result.get("error") or result.get("freed_gb") or "")[:300],
        )
        return JSONResponse(result)

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

    async def api_trading(request):
        if (d := _require_auth_json(request)):
            return d
        path = request.path_params["path"]
        allowed = (
            _TRADING_GET_RE.match(path)
            if request.method == "GET"
            else _TRADING_POST_RE.match(path)
        )
        if not allowed:
            return JSONResponse({"error": "not allowed"}, status_code=404)
        try:
            body = await request.body()
            headers = {"X-Internal-Token": TRADING_TOKEN}
            if body:
                headers["Content-Type"] = request.headers.get(
                    "content-type", "application/json"
                )
            timeout = 180.0 if _TRADING_SLOW_RE.match(path) else 15.0
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.request(
                    request.method,
                    f"{TRADING_URL}/api/{path}",
                    params=dict(request.query_params),
                    content=body,
                    headers=headers,
                )
            return JSONResponse(resp.json(), status_code=resp.status_code)
        except Exception as exc:  # 서비스 다운 시 graceful
            return JSONResponse(
                {"error": f"trading 서비스 연결 실패: {exc}"}, status_code=502
            )

    async def api_tnm(request):
        if (d := _require_auth_json(request)):
            return d
        path = request.path_params["path"]
        allowed = (
            _TNM_GET_RE.match(path)
            if request.method == "GET"
            else _TNM_POST_RE.match(path)
        )
        if not allowed:
            return JSONResponse({"error": "not allowed"}, status_code=404)
        try:
            body = await request.body()
            headers = {"X-Internal-Token": TNM_TOKEN}
            if body:
                headers["Content-Type"] = request.headers.get(
                    "content-type", "application/json"
                )
            timeout = 180.0 if _TNM_SLOW_RE.match(path) else 15.0
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.request(
                    request.method,
                    f"{TNM_URL}/api/{path}",
                    params=dict(request.query_params),
                    content=body,
                    headers=headers,
                )
            return JSONResponse(resp.json(), status_code=resp.status_code)
        except Exception as exc:  # 서비스 다운 시 graceful
            return JSONResponse(
                {"error": f"TNM 서비스 연결 실패: {exc}"}, status_code=502
            )

    async def static_file(request):
        # 로그인 페이지 자산(스타일·vendor 라이브러리)은 인증 전에도 필요하므로 공개.
        name = request.path_params["path"]
        public = name in _PUBLIC_ASSETS or name.startswith("vendor/")
        if not _is_authed(request) and not public:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        target = (STATIC_DIR / name).resolve()
        if STATIC_DIR not in target.parents or not target.is_file():
            return Response(status_code=404)
        return FileResponse(target, headers=_cache_headers(name))

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
        Route("/api/night-report", api_night_report),
        Route("/api/llm/status", api_llm_status),
        Route("/api/llm/generate", api_llm_generate, methods=["POST"]),
        Route("/api/llm/jobs/{job_id}", api_llm_job),
        Route("/api/llm/models", api_llm_models),
        Route("/api/llm/models/decide", api_llm_models_decide, methods=["POST"]),
        Route("/api/llm/roles", api_llm_roles),
        Route("/api/llm/roles", api_llm_role_save, methods=["POST"]),
        Route("/api/llm/roles", api_llm_role_revert, methods=["DELETE"]),
        Route("/api/llm/compare", api_llm_compare, methods=["POST"]),
        Route("/api/llm/compare/{run_id}", api_llm_compare_get),
        Route("/api/llm/integration", api_llm_integration),
        Route("/api/llm/meta", api_llm_meta),
        Route("/api/llm/openapi", api_llm_openapi),
        Route("/api/llm/client", api_llm_client),
        Route("/api/llm/services", api_llm_services),
        Route("/api/llm/services/reveal", api_llm_token_reveal, methods=["POST"]),
        Route("/api/llm/installed", api_llm_installed),
        Route("/api/llm/catalog", api_llm_catalog),
        Route("/api/llm/models/install", api_llm_model_install, methods=["POST"]),
        Route("/api/llm/models/delete", api_llm_model_delete, methods=["DELETE"]),
        Route("/api/docker", api_docker),
        Route("/api/weather", api_weather),
        Route("/api/trading/{path:path}", api_trading, methods=["GET", "POST"]),
        Route("/api/tnm/{path:path}", api_tnm, methods=["GET", "POST"]),
        Route("/api/jw/summary", api_jw_summary),
        Route("/api/jw/users", api_jw_users),
        Route("/api/jw/usage", api_jw_usage),
        Route("/api/jw/calls", api_jw_calls),
        Route("/api/jw/users/status", api_jw_user_status, methods=["POST"]),
        Route("/static/{path:path}", static_file),
    ]


_PUBLIC_ASSETS = {"style.css", "login.js"}

# 캐시 정책.
# 우리 자산(app.js·pages/*.js·style.css)은 배포마다 바뀐다. Cache-Control 이
# 없으면 브라우저가 휴리스틱 캐싱(대략 Last-Modified 경과의 10%)을 적용해,
# 배포 후에도 옛 모듈 그래프를 계속 쓴다 — 새 페이지를 추가해도 사이드바에
# 나타나지 않는 증상이 여기서 나온다. no-cache 는 '쓰지 말라'가 아니라 '쓰기 전에
# 반드시 확인하라'는 뜻이라, ETag 로 304 가 떨어져 전송량은 그대로 아낀다.
_NO_CACHE = {"Cache-Control": "no-cache"}
# vendor/ 는 버전 고정된 서드파티라 오래 캐시해도 안전하다(용량이 크다).
_VENDOR_CACHE = {"Cache-Control": "public, max-age=604800"}


def _cache_headers(name: str) -> dict:
    return _VENDOR_CACHE if name.startswith("vendor/") else _NO_CACHE


def _int_param(request, name: str, default: int) -> int:
    try:
        return int(request.query_params.get(name, default))
    except (TypeError, ValueError):
        return default
