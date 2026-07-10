"""FastMCP 인스턴스 조립 + 전체 ASGI 앱 팩토리.

build_app() 은 MCP 엔드포인트(/mcp, Bearer 인증)와 대시보드(세션 인증)를
하나의 Starlette 앱으로 결합한다. 테스트는 FakeRunner + 임시 레지스트리로
이 팩토리를 그대로 호출할 수 있다.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.server.streamable_http import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.routing import Mount

from . import dashboard
from .audit import AuditLog
from .auth import BearerAuthMiddleware
from .context import AppContext
from .jobs import JobManager
from .registry import Registry
from .runner import CommandRunner
from .tools import control, files, scripts, shell, system
from .tools import jobs as jobs_tools

SERVER_NAME = "hosub-mcp"


def build_mcp(
    ctx: AppContext, *, allowed_hosts: list[str] | None = None
) -> FastMCP:
    """모든 도구가 등록된 FastMCP 인스턴스를 생성한다.

    allowed_hosts 가 주어지면 DNS 리바인딩 보호를 켜고 해당 Host/Origin 만 허용한다.
    None 이면 보호를 끈다 (Bearer 토큰 + Cloudflare Tunnel 이 실제 경계이므로).
    """
    if allowed_hosts:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=[f"https://{h}" for h in allowed_hosts],
        )
    else:
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    mcp = FastMCP(
        SERVER_NAME,
        stateless_http=True,
        json_response=True,
        transport_security=security,
    )
    system.register(mcp, ctx)
    control.register(mcp, ctx)
    scripts.register(mcp, ctx)
    shell.register(mcp, ctx)
    files.register(mcp, ctx)
    jobs_tools.register(mcp, ctx)
    return mcp


def build_context(
    registry: Registry,
    runner: CommandRunner,
    audit: AuditLog,
    *,
    jobs: JobManager | None = None,
) -> AppContext:
    job_mgr = jobs or JobManager(runner, audit)
    return AppContext(registry=registry, runner=runner, jobs=job_mgr, audit=audit)


def build_app(
    *,
    registry: Registry,
    runner: CommandRunner,
    audit: AuditLog,
    mcp_token: str,
    dash_password: str,
    session_secret: str,
    jobs: JobManager | None = None,
    allowed_hosts: list[str] | None = None,
) -> Starlette:
    ctx = build_context(registry, runner, audit, jobs=jobs)
    mcp = build_mcp(ctx, allowed_hosts=allowed_hosts)
    mcp_app = mcp.streamable_http_app()

    routes = dashboard.build_routes(ctx, dash_password)
    # Bearer 인증은 MCP 마운트에만 적용. 대시보드/정적 자산은 세션 인증.
    routes.append(Mount("/", app=BearerAuthMiddleware(mcp_app, mcp_token)))

    app = Starlette(
        routes=routes,
        # 세션 매니저 lifespan 을 부모 앱에서 실행 (마운트 앱은 lifespan 미수신)
        lifespan=lambda _app: mcp_app.router.lifespan_context(_app),
        middleware=[
            Middleware(
                SessionMiddleware,
                secret_key=session_secret,
                same_site="lax",
                https_only=False,
                session_cookie="hosub_dash",
            )
        ],
    )
    # 컨텍스트를 앱에 노출 (테스트/디버깅용)
    app.state.ctx = ctx
    return app
