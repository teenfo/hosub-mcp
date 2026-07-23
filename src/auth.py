"""Bearer 토큰 인증 (순수 ASGI 미들웨어).

MCP 엔드포인트(/mcp)에만 적용된다. 정적 HOSUB_MCP_TOKEN 과 OAuth 로 발급된
액세스 토큰을 모두 수용하도록 verify 콜백을 받는다. 인증 실패 시 MCP 인증
스펙에 따라 WWW-Authenticate 헤더에 resource_metadata 를 실어 OAuth 흐름을
개시하도록 안내한다.

BaseHTTPMiddleware 는 스트리밍 응답을 버퍼링하므로 사용하지 않는다.
"""

from __future__ import annotations

from typing import Callable

from starlette.responses import JSONResponse

from .oauth import base_url_from_scope


class BearerAuthMiddleware:
    def __init__(
        self,
        app,
        verify: Callable[[str], bool],
        *,
        public_url: str | None = None,
    ) -> None:
        self._app = app
        self._verify = verify
        self._public_url = public_url

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode("latin-1")
        token = auth[len("Bearer ") :] if auth.startswith("Bearer ") else None
        if token and self._verify(token):
            await self._app(scope, receive, send)
            return

        base = base_url_from_scope(scope, self._public_url)
        resource_metadata = f"{base}/.well-known/oauth-protected-resource"
        response = JSONResponse(
            {"error": "unauthorized"},
            status_code=401,
            headers={
                "WWW-Authenticate": (
                    f'Bearer resource_metadata="{resource_metadata}"'
                )
            },
        )
        await response(scope, receive, send)
