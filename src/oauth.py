"""OAuth 2.1 인증 서버 (단일 사용자 개인 서버용).

claude.ai 커넥터 UI에는 Bearer 헤더 입력란이 없어, 표준 OAuth 2.1 흐름으로
연결해야 한다. 이 모듈은 MCP 인증 스펙이 요구하는 최소 엔드포인트를 제공한다:

- GET  /.well-known/oauth-protected-resource[/mcp]  (RFC 9728)
- GET  /.well-known/oauth-authorization-server       (RFC 8414)
- POST /register                                     (RFC 7591 동적 등록)
- GET/POST /authorize   (대시보드 비밀번호로 승인, PKCE S256 필수)
- POST /token           (code 교환 + refresh_token)

승인 주체는 단일 사용자이며, 승인 게이트는 대시보드 비밀번호다. 발급된
액세스/리프레시 토큰은 SQLite에 해시로 저장되고, MCP Bearer 인증 계층이 수용한다.
기존 정적 HOSUB_MCP_TOKEN 인증도 병행 유지된다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

# 토큰 수명
CODE_TTL = 300  # 인가 코드 5분
ACCESS_TTL = 30 * 24 * 3600  # 액세스 토큰 30일
REFRESH_TTL = 365 * 24 * 3600  # 리프레시 토큰 1년

_SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_clients (
  client_id     TEXT PRIMARY KEY,
  redirect_uris TEXT NOT NULL,
  client_name   TEXT,
  created_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_codes (
  code           TEXT PRIMARY KEY,
  client_id      TEXT NOT NULL,
  redirect_uri   TEXT NOT NULL,
  code_challenge TEXT NOT NULL,
  scope          TEXT,
  resource       TEXT,
  expires_at     REAL NOT NULL,
  used           INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS oauth_tokens (
  token_hash TEXT PRIMARY KEY,
  client_id  TEXT,
  kind       TEXT NOT NULL,          -- access | refresh
  scope      TEXT,
  expires_at REAL,
  created_at REAL NOT NULL
);
"""


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def verify_pkce_s256(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(computed, challenge)


def compose_base_url(
    configured: str | None,
    forwarded_proto: str | None,
    forwarded_host: str | None,
    host: str | None,
) -> str:
    """공개 base URL을 만든다. 명시 설정 우선, 없으면 프록시 헤더에서 유추.

    서버는 Caddy 뒤에서 평문 HTTP로 뜨므로, 스킴은 X-Forwarded-Proto(=https)를
    신뢰하고, 없으면 https로 가정한다.
    """
    if configured:
        return configured.rstrip("/")
    scheme = forwarded_proto or "https"
    h = forwarded_host or host or "localhost"
    return f"{scheme}://{h}"


def base_url_from_scope(scope, configured: str | None) -> str:
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    return compose_base_url(
        configured,
        headers.get("x-forwarded-proto"),
        headers.get("x-forwarded-host"),
        headers.get("host"),
    )


def _request_base_url(request: Request, configured: str | None) -> str:
    return compose_base_url(
        configured,
        request.headers.get("x-forwarded-proto"),
        request.headers.get("x-forwarded-host"),
        request.headers.get("host"),
    )


class OAuthStore:
    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._lock, self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # --- 클라이언트 ---
    def register_client(self, redirect_uris: list[str], client_name: str | None) -> str:
        client_id = "c_" + secrets.token_urlsafe(18)
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO oauth_clients (client_id, redirect_uris, client_name, created_at) "
                "VALUES (?,?,?,?)",
                (client_id, json.dumps(redirect_uris), client_name or "", time.time()),
            )
        return client_id

    def client_redirect_uris(self, client_id: str) -> list[str] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT redirect_uris FROM oauth_clients WHERE client_id=?", (client_id,)
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["redirect_uris"])

    # --- 인가 코드 ---
    def create_code(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        scope: str | None,
        resource: str | None,
    ) -> str:
        code = secrets.token_urlsafe(24)
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO oauth_codes "
                "(code, client_id, redirect_uri, code_challenge, scope, resource, expires_at, used) "
                "VALUES (?,?,?,?,?,?,?,0)",
                (
                    code,
                    client_id,
                    redirect_uri,
                    code_challenge,
                    scope,
                    resource,
                    time.time() + CODE_TTL,
                ),
            )
        return code

    def consume_code(self, code: str) -> dict | None:
        """코드를 1회용으로 소비. 유효하면 dict 반환, 아니면 None."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM oauth_codes WHERE code=?", (code,)
            ).fetchone()
            if row is None or row["used"] or row["expires_at"] < time.time():
                return None
            conn.execute("UPDATE oauth_codes SET used=1 WHERE code=?", (code,))
            return dict(row)

    # --- 토큰 ---
    def issue_tokens(self, client_id: str | None, scope: str | None) -> dict:
        access = "at_" + secrets.token_urlsafe(32)
        refresh = "rt_" + secrets.token_urlsafe(32)
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO oauth_tokens (token_hash, client_id, kind, scope, expires_at, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (_sha256(access), client_id, "access", scope, now + ACCESS_TTL, now),
            )
            conn.execute(
                "INSERT INTO oauth_tokens (token_hash, client_id, kind, scope, expires_at, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (_sha256(refresh), client_id, "refresh", scope, now + REFRESH_TTL, now),
            )
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ACCESS_TTL,
            "refresh_token": refresh,
            "scope": scope or "mcp",
        }

    def verify_access(self, token: str) -> bool:
        h = _sha256(token)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT expires_at FROM oauth_tokens WHERE token_hash=? AND kind='access'",
                (h,),
            ).fetchone()
        if row is None:
            return False
        return row["expires_at"] is None or row["expires_at"] > time.time()

    def exchange_refresh(self, refresh_token: str) -> dict | None:
        h = _sha256(refresh_token)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT client_id, scope, expires_at FROM oauth_tokens "
                "WHERE token_hash=? AND kind='refresh'",
                (h,),
            ).fetchone()
        if row is None or (row["expires_at"] is not None and row["expires_at"] < time.time()):
            return None
        return self.issue_tokens(row["client_id"], row["scope"])


# --- 승인(로그인) 페이지 ---
_AUTHORIZE_PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>hosub MCP 연결 승인</title>
<link rel="stylesheet" href="/static/style.css"></head>
<body><div class="login-wrap"><form class="login-box" method="post" action="/authorize">
<h1>hosub MCP 연결 승인</h1>
<p>Claude 커넥터가 이 서버에 연결하려고 합니다. 승인하려면 대시보드 비밀번호를 입력하세요.</p>
{hidden}
<input type="password" name="password" placeholder="대시보드 비밀번호" autofocus autocomplete="current-password">
<button type="submit">승인하고 연결</button>
{error}
</form></div></body></html>
"""

_OAUTH_PARAMS = [
    "response_type",
    "client_id",
    "redirect_uri",
    "code_challenge",
    "code_challenge_method",
    "state",
    "scope",
    "resource",
]


def _hidden_fields(values: dict) -> str:
    out = []
    for k in _OAUTH_PARAMS:
        v = values.get(k)
        if v:
            out.append(
                f'<input type="hidden" name="{k}" value="{_html_escape(v)}">'
            )
    return "\n".join(out)


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _redirect_with(redirect_uri: str, params: dict) -> str:
    from urllib.parse import urlencode

    sep = "&" if "?" in redirect_uri else "?"
    return f"{redirect_uri}{sep}{urlencode(params)}"


def build_oauth_routes(
    store: OAuthStore,
    dash_password: str,
    *,
    public_url: str | None = None,
    audit=None,
) -> list[Route]:
    def _base(request: Request) -> str:
        return _request_base_url(request, public_url)

    async def protected_resource(request: Request):
        base = _base(request)
        return JSONResponse(
            {
                "resource": base,
                "authorization_servers": [base],
                "bearer_methods_supported": ["header"],
                "scopes_supported": ["mcp"],
            }
        )

    async def protected_resource_mcp(request: Request):
        base = _base(request)
        return JSONResponse(
            {
                "resource": base + "/mcp",
                "authorization_servers": [base],
                "bearer_methods_supported": ["header"],
                "scopes_supported": ["mcp"],
            }
        )

    async def authorization_server(request: Request):
        base = _base(request)
        return JSONResponse(
            {
                "issuer": base,
                "authorization_endpoint": base + "/authorize",
                "token_endpoint": base + "/token",
                "registration_endpoint": base + "/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
                "scopes_supported": ["mcp"],
            }
        )

    async def register(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        redirect_uris = body.get("redirect_uris")
        if not isinstance(redirect_uris, list) or not redirect_uris:
            return JSONResponse(
                {"error": "invalid_client_metadata", "error_description": "redirect_uris 필요"},
                status_code=400,
            )
        client_id = store.register_client(redirect_uris, body.get("client_name"))
        if audit is not None:
            audit.log(tool="__oauth_register", outcome="ok", result_summary=client_id)
        return JSONResponse(
            {
                "client_id": client_id,
                "client_id_issued_at": int(time.time()),
                "redirect_uris": redirect_uris,
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "scope": body.get("scope", "mcp"),
            },
            status_code=201,
        )

    def _validate_authorize(values: dict) -> tuple[bool, str]:
        if values.get("response_type") != "code":
            return False, "response_type must be code"
        client_id = values.get("client_id")
        redirect_uri = values.get("redirect_uri")
        if not client_id or not redirect_uri:
            return False, "client_id/redirect_uri required"
        registered = store.client_redirect_uris(client_id)
        if registered is None:
            return False, "unknown client"
        if redirect_uri not in registered:
            return False, "redirect_uri mismatch"
        if not values.get("code_challenge") or values.get("code_challenge_method") != "S256":
            return False, "PKCE S256 required"
        return True, ""

    async def authorize_get(request: Request):
        values = {k: request.query_params.get(k) for k in _OAUTH_PARAMS}
        ok, msg = _validate_authorize(values)
        if not ok:
            return JSONResponse(
                {"error": "invalid_request", "error_description": msg}, status_code=400
            )
        page = _AUTHORIZE_PAGE.format(hidden=_hidden_fields(values), error="")
        return HTMLResponse(page)

    async def authorize_post(request: Request):
        form = await request.form()
        values = {k: form.get(k) for k in _OAUTH_PARAMS}
        ok, msg = _validate_authorize(values)
        if not ok:
            return JSONResponse(
                {"error": "invalid_request", "error_description": msg}, status_code=400
            )
        password = str(form.get("password", ""))
        if not (dash_password and hmac.compare_digest(password, dash_password)):
            err = '<div class="error show">비밀번호가 올바르지 않습니다.</div>'
            page = _AUTHORIZE_PAGE.format(hidden=_hidden_fields(values), error=err)
            return HTMLResponse(page, status_code=401)

        code = store.create_code(
            client_id=values["client_id"],
            redirect_uri=values["redirect_uri"],
            code_challenge=values["code_challenge"],
            scope=values.get("scope"),
            resource=values.get("resource"),
        )
        if audit is not None:
            audit.log(
                tool="__oauth_authorize",
                outcome="ok",
                result_summary=values["client_id"],
            )
        params = {"code": code}
        if values.get("state"):
            params["state"] = values["state"]
        return RedirectResponse(
            _redirect_with(values["redirect_uri"], params), status_code=302
        )

    async def token(request: Request):
        form = await request.form()
        grant_type = form.get("grant_type")

        if grant_type == "authorization_code":
            code = form.get("code")
            redirect_uri = form.get("redirect_uri")
            client_id = form.get("client_id")
            code_verifier = form.get("code_verifier")
            if not (code and redirect_uri and code_verifier):
                return _token_error("invalid_request", "missing parameters")
            row = store.consume_code(code)
            if row is None:
                return _token_error("invalid_grant", "code invalid or expired")
            if client_id and row["client_id"] != client_id:
                return _token_error("invalid_grant", "client mismatch")
            if row["redirect_uri"] != redirect_uri:
                return _token_error("invalid_grant", "redirect_uri mismatch")
            if not verify_pkce_s256(code_verifier, row["code_challenge"]):
                return _token_error("invalid_grant", "PKCE verification failed")
            tokens = store.issue_tokens(row["client_id"], row["scope"])
            if audit is not None:
                audit.log(
                    tool="__oauth_token",
                    outcome="ok",
                    result_summary="authorization_code",
                )
            return JSONResponse(tokens, headers={"Cache-Control": "no-store"})

        if grant_type == "refresh_token":
            refresh_token = form.get("refresh_token")
            if not refresh_token:
                return _token_error("invalid_request", "missing refresh_token")
            tokens = store.exchange_refresh(refresh_token)
            if tokens is None:
                return _token_error("invalid_grant", "refresh_token invalid or expired")
            if audit is not None:
                audit.log(
                    tool="__oauth_token", outcome="ok", result_summary="refresh_token"
                )
            return JSONResponse(tokens, headers={"Cache-Control": "no-store"})

        return _token_error("unsupported_grant_type", str(grant_type))

    return [
        Route("/.well-known/oauth-protected-resource", protected_resource, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource/mcp", protected_resource_mcp, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", authorization_server, methods=["GET"]),
        Route("/register", register, methods=["POST"]),
        Route("/authorize", authorize_get, methods=["GET"]),
        Route("/authorize", authorize_post, methods=["POST"]),
        Route("/token", token, methods=["POST"]),
    ]


def _token_error(error: str, description: str) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=400,
        headers={"Cache-Control": "no-store"},
    )
