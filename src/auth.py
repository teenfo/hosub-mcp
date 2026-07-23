"""Bearer 토큰 인증 (순수 ASGI 미들웨어).

MCP 엔드포인트(/mcp)에만 적용된다. SDK 내장 인증은 OAuth 지향이라, 단일
정적 토큰 검증에는 이 얇은 래퍼가 가장 단순하다. BaseHTTPMiddleware 는
스트리밍 응답을 버퍼링하므로 사용하지 않는다.
"""

from __future__ import annotations

import hmac

from starlette.responses import JSONResponse


class BearerAuthMiddleware:
    def __init__(self, app, token: str) -> None:
        self._app = app
        self._token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            # lifespan 등은 그대로 통과
            await self._app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode("latin-1")
        if not self._authorized(auth):
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)

    def _authorized(self, auth_header: str) -> bool:
        if not auth_header.startswith("Bearer "):
            return False
        presented = auth_header[len("Bearer ") :]
        return hmac.compare_digest(presented, self._token)
