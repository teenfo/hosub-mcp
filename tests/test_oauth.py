"""OAuth 2.1 인증 흐름 테스트 (단일 사용자 개인 서버).

.well-known 공개 + 동적 등록 + PKCE authorize/token + refresh + MCP 토큰 수용.
"""

from __future__ import annotations

import base64
import hashlib
import tempfile
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from src.audit import AuditLog
from src.oauth import OAuthStore
from src.registry import Registry
from src.server import build_app
from tests.conftest import FakeRunner

TOKEN = "t" * 40
PASSWORD = "dash-secret-pw"
PUBLIC = "https://hosub.duckdns.org"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture
def client():
    app = build_app(
        registry=Registry.from_dict({"services": {"ollama": {"unit": "ollama.service"}}}),
        runner=FakeRunner(),
        audit=AuditLog(tempfile.mktemp(suffix=".db")),
        mcp_token=TOKEN,
        dash_password=PASSWORD,
        session_secret="session-secret-abcdefgh",
        oauth_store=OAuthStore(tempfile.mktemp(suffix=".db")),
        public_url=PUBLIC,
    )
    with TestClient(app) as c:
        yield c


def _pkce():
    verifier = "verifier-" + "a" * 50
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# --- 메타데이터 공개 ---
def test_well_known_public(client):
    r = client.get("/.well-known/oauth-protected-resource")
    assert r.status_code == 200
    body = r.json()
    assert body["resource"] == PUBLIC
    assert body["authorization_servers"] == [PUBLIC]


def test_well_known_mcp_variant(client):
    r = client.get("/.well-known/oauth-protected-resource/mcp")
    assert r.status_code == 200
    assert r.json()["resource"] == PUBLIC + "/mcp"


def test_authorization_server_metadata(client):
    r = client.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    m = r.json()
    assert m["issuer"] == PUBLIC
    assert m["authorization_endpoint"] == PUBLIC + "/authorize"
    assert m["token_endpoint"] == PUBLIC + "/token"
    assert m["registration_endpoint"] == PUBLIC + "/register"
    assert m["code_challenge_methods_supported"] == ["S256"]
    assert "authorization_code" in m["grant_types_supported"]
    assert "refresh_token" in m["grant_types_supported"]


def test_unauthorized_has_www_authenticate(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401
    wa = r.headers.get("www-authenticate", "")
    assert "resource_metadata=" in wa
    assert "/.well-known/oauth-protected-resource" in wa


# --- 동적 등록 ---
def test_register_client(client):
    r = client.post("/register", json={"redirect_uris": [REDIRECT], "client_name": "Claude"})
    assert r.status_code == 201
    body = r.json()
    assert body["client_id"]
    assert body["redirect_uris"] == [REDIRECT]


def test_register_requires_redirect_uris(client):
    r = client.post("/register", json={"client_name": "x"})
    assert r.status_code == 400


# --- authorize + token 전체 흐름 ---
def _register(client):
    return client.post("/register", json={"redirect_uris": [REDIRECT]}).json()["client_id"]


def _authorize_params(client_id, challenge):
    return {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "xyz",
        "scope": "mcp",
    }


def test_authorize_get_shows_login(client):
    cid = _register(client)
    _, challenge = _pkce()
    r = client.get("/authorize", params=_authorize_params(cid, challenge))
    assert r.status_code == 200
    assert "비밀번호" in r.text


def test_authorize_rejects_unknown_client(client):
    _, challenge = _pkce()
    r = client.get("/authorize", params=_authorize_params("c_nope", challenge))
    assert r.status_code == 400


def test_authorize_requires_pkce_s256(client):
    cid = _register(client)
    params = _authorize_params(cid, "chal")
    params["code_challenge_method"] = "plain"
    r = client.get("/authorize", params=params)
    assert r.status_code == 400


def test_authorize_wrong_password(client):
    cid = _register(client)
    _, challenge = _pkce()
    data = _authorize_params(cid, challenge)
    data["password"] = "wrong"
    r = client.post("/authorize", data=data, follow_redirects=False)
    assert r.status_code == 401


def test_full_flow_and_token_accepted_by_mcp(client):
    cid = _register(client)
    verifier, challenge = _pkce()

    # authorize (승인)
    data = _authorize_params(cid, challenge)
    data["password"] = PASSWORD
    r = client.post("/authorize", data=data, follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    q = parse_qs(urlparse(loc).query)
    assert q["state"] == ["xyz"]
    code = q["code"][0]

    # token 교환 (PKCE 검증)
    r = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": cid,
            "code_verifier": verifier,
        },
    )
    assert r.status_code == 200
    tok = r.json()
    assert tok["token_type"] == "Bearer"
    access = tok["access_token"]
    refresh = tok["refresh_token"]

    # 발급 토큰으로 MCP 접근 → 더 이상 401 아님
    r = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        },
        headers={
            "Authorization": f"Bearer {access}",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert r.status_code == 200

    # refresh 교환
    r = client.post(
        "/token", data={"grant_type": "refresh_token", "refresh_token": refresh}
    )
    assert r.status_code == 200
    assert r.json()["access_token"] != access


def test_token_wrong_pkce_verifier(client):
    cid = _register(client)
    _, challenge = _pkce()
    data = _authorize_params(cid, challenge)
    data["password"] = PASSWORD
    r = client.post("/authorize", data=data, follow_redirects=False)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]

    r = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": cid,
            "code_verifier": "wrong-verifier-xxxxxxxxxxxxxxxxxxxxxxxxx",
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


def test_code_single_use(client):
    cid = _register(client)
    verifier, challenge = _pkce()
    data = _authorize_params(cid, challenge)
    data["password"] = PASSWORD
    r = client.post("/authorize", data=data, follow_redirects=False)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "client_id": cid,
        "code_verifier": verifier,
    }
    assert client.post("/token", data=body).status_code == 200
    # 재사용 거부
    assert client.post("/token", data=body).status_code == 400


def test_static_token_still_works(client):
    # 정적 HOSUB_MCP_TOKEN 병행 유지 (curl/비상용)
    r = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        },
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert r.status_code == 200
