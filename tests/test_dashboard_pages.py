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
    monkeypatch.setattr(dashboard, "BRIEFING_DIR", str(tmp_path / "empty"))
    with _client() as c:
        _login(c)
        r = c.get("/api/briefing")
        assert r.status_code == 200
        body = r.json()
        assert body["exists"] is False
        assert body["dates"] == []
        assert "hint" in body


def test_briefing_latest_and_dates(tmp_path, monkeypatch):
    (tmp_path / "2026-07-21.html").write_text("<h2>21일</h2>", encoding="utf-8")
    (tmp_path / "2026-07-23.html").write_text("<h2>23일</h2><script>alert(1)</script>", encoding="utf-8")
    (tmp_path / "2026-07-22.md").write_text("# 22일", encoding="utf-8")
    monkeypatch.setattr(dashboard, "BRIEFING_DIR", str(tmp_path))
    with _client() as c:
        _login(c)
        r = c.get("/api/briefing")
        body = r.json()
        assert body["exists"] is True
        assert body["date"] == "2026-07-23"           # 최신
        assert body["dates"] == ["2026-07-23", "2026-07-22", "2026-07-21"]
        assert body["format"] == "html"
        assert "23일" in body["content"]
        assert "<script>" not in body["content"]        # script 제거됨


def test_briefing_by_date_and_md(tmp_path, monkeypatch):
    (tmp_path / "2026-07-22.md").write_text("# 22일 브리핑", encoding="utf-8")
    (tmp_path / "2026-07-23.html").write_text("<h2>23일</h2>", encoding="utf-8")
    monkeypatch.setattr(dashboard, "BRIEFING_DIR", str(tmp_path))
    with _client() as c:
        _login(c)
        r = c.get("/api/briefing?date=2026-07-22")
        body = r.json()
        assert body["date"] == "2026-07-22"
        assert body["format"] == "md"
        assert "22일" in body["content"]


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


# --- 정적 자산 캐시 정책 ---
# 배포마다 바뀌는 우리 자산이 브라우저 휴리스틱 캐싱에 걸려, 새 페이지를 추가해도
# 사이드바에 나타나지 않던 문제의 회귀 방지.

def test_own_assets_are_revalidated():
    with _client() as c:
        _login(c)
        for path in ("/static/app.js", "/static/pages/index.js", "/static/style.css"):
            r = c.get(path)
            assert r.status_code == 200, path
            assert r.headers["cache-control"] == "no-cache", path


def test_vendor_assets_cached_long():
    """버전 고정된 서드파티는 오래 캐시 — 매 요청 재검증하지 않는다."""
    with _client() as c:
        r = c.get("/static/vendor/bootstrap/bootstrap.bundle.min.js")
        assert r.status_code == 200
        assert "max-age=604800" in r.headers["cache-control"]


def test_html_shell_revalidated():
    with _client() as c:
        _login(c)
        assert c.get("/").headers["cache-control"] == "no-cache"
        assert c.get("/login").headers["cache-control"] == "no-cache"


def test_journal_page_is_registered():
    """매매일지 페이지가 라우팅 레지스트리에 실제로 실려 나가는지."""
    with _client() as c:
        _login(c)
        idx = c.get("/static/pages/index.js").text
        assert 'import journal from "./journal.js"' in idx
        assert "journal," in idx.split("export const PAGES")[1]
        assert c.get("/static/pages/journal.js").status_code == 200
