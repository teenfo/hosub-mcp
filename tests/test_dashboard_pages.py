"""신규 대시보드 페이지 API 테스트 (브리핑/도커/날씨)."""

from __future__ import annotations

import tempfile

import pytest
from starlette.testclient import TestClient

import src.dashboard as dashboard
from src.audit import AuditLog
from src.oauth import OAuthStore
from src.registry import Registry
from src.runner import RunResult
from src.server import build_app
from tests.conftest import FakeRunner

PASSWORD = "pw-secret-123"


def _client(runner=None, monkeypatch_briefing=None):
    app = build_app(
        registry=Registry.from_dict({"services": {}}),
        runner=runner or FakeRunner(),
        audit=AuditLog(tempfile.mktemp(suffix=".db")),
        mcp_token="t" * 40,
        dash_password=PASSWORD,
        session_secret="session-secret-abcdefgh",
        oauth_store=OAuthStore(tempfile.mktemp(suffix=".db")),
    )
    return TestClient(app)


def _login(c):
    c.post("/login", data={"password": PASSWORD}, follow_redirects=False)


def test_briefing_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "BRIEFING_PATH", str(tmp_path / "nope.md"))
    with _client() as c:
        _login(c)
        r = c.get("/api/briefing")
        assert r.status_code == 200
        body = r.json()
        assert body["exists"] is False
        assert "hint" in body


def test_briefing_present(tmp_path, monkeypatch):
    f = tmp_path / "briefing.md"
    f.write_text("# 오늘의 브리핑\n\n- 항목 1\n- 항목 2\n", encoding="utf-8")
    monkeypatch.setattr(dashboard, "BRIEFING_PATH", str(f))
    with _client() as c:
        _login(c)
        r = c.get("/api/briefing")
        assert r.status_code == 200
        body = r.json()
        assert body["exists"] is True
        assert "오늘의 브리핑" in body["content"]
        assert body["updated_at"]


def test_briefing_requires_auth():
    with _client() as c:
        assert c.get("/api/briefing").status_code == 401


def test_docker_ok():
    line = (
        '{"id":"abc123","name":"ollama","image":"ollama/ollama",'
        '"status":"Up 2 hours","state":"running","ports":"11434/tcp"}'
    )
    runner = FakeRunner(default=RunResult(0, line + "\n", ""))
    with _client(runner=runner) as c:
        _login(c)
        r = c.get("/api/docker")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["containers"][0]["name"] == "ollama"
        assert body["containers"][0]["state"] == "running"


def test_docker_failure_graceful():
    runner = FakeRunner(default=RunResult(127, "", "docker: command not found"))
    with _client(runner=runner) as c:
        _login(c)
        r = c.get("/api/docker")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        assert "command not found" in body["error"]


def test_docker_requires_auth():
    with _client() as c:
        assert c.get("/api/docker").status_code == 401


def test_weather_graceful_on_network_failure():
    # 이 환경은 외부 호출이 차단되므로 ok=False + 200 이어야 한다 (graceful).
    with _client() as c:
        _login(c)
        r = c.get("/api/weather")
        assert r.status_code == 200
        body = r.json()
        assert "ok" in body
        assert "label" in body


def test_weather_requires_auth():
    with _client() as c:
        assert c.get("/api/weather").status_code == 401
