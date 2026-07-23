"""대시보드 인증 경계 및 API 테스트."""

from __future__ import annotations

import tempfile

import pytest
from starlette.testclient import TestClient

from src.audit import AuditLog
from src.registry import Registry
from src.server import build_app
from tests.conftest import FakeRunner

TOKEN = "t" * 40
PASSWORD = "hunter2-secret"
REG = {"services": {"ollama": {"unit": "ollama.service"}}}


@pytest.fixture
def client():
    app = build_app(
        registry=Registry.from_dict(REG),
        runner=FakeRunner(),
        audit=AuditLog(tempfile.mktemp(suffix=".db")),
        mcp_token=TOKEN,
        dash_password=PASSWORD,
        session_secret="session-secret-abcdefgh",
    )
    with TestClient(app) as c:
        yield c


def test_api_requires_login(client):
    r = client.get("/api/status")
    assert r.status_code == 401


def test_wrong_password_rejected(client):
    r = client.post("/login", data={"password": "nope"}, follow_redirects=False)
    assert r.status_code == 401
    # 세션 쿠키가 인증 상태로 설정되지 않음
    r2 = client.get("/api/status")
    assert r2.status_code == 401


def test_login_and_access(client):
    r = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/"

    # 세션 쿠키로 API 접근 가능
    for path in ["/api/status", "/api/services", "/api/jobs", "/api/audit"]:
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert resp.headers["content-type"].startswith("application/json")

    assert "cpu" in client.get("/api/status").json()
    assert "services" in client.get("/api/services").json()
    assert "jobs" in client.get("/api/jobs").json()
    assert "audit" in client.get("/api/audit").json()


def test_bearer_token_cannot_access_dashboard(client):
    # MCP Bearer 토큰은 대시보드 API 경계를 넘지 못한다
    r = client.get("/api/status", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 401


def test_index_redirects_when_logged_out(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_logout_clears_session(client):
    client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert client.get("/api/status").status_code == 200
    client.get("/logout", follow_redirects=False)
    assert client.get("/api/status").status_code == 401
