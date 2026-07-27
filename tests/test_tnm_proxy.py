"""TNM 프록시 화이트리스트·인증 테스트 (M6)."""

from __future__ import annotations

import tempfile

from starlette.testclient import TestClient

import src.dashboard as dashboard
from src.audit import AuditLog
from src.oauth import OAuthStore
from src.registry import Registry
from src.server import build_app
from tests.conftest import FakeRunner

PASSWORD = "pw-secret-123"


def _client():
    app = build_app(
        registry=Registry.from_dict({"services": {}}),
        runner=FakeRunner(),
        audit=AuditLog(tempfile.mktemp(suffix=".db")),
        mcp_token="t" * 40,
        dash_password=PASSWORD,
        session_secret="session-secret-abcdefgh",
        oauth_store=OAuthStore(tempfile.mktemp(suffix=".db")),
    )
    return TestClient(app)


def _login(c):
    c.post("/login", data={"password": PASSWORD}, follow_redirects=False)


def test_tnm_proxy_requires_session():
    with _client() as c:
        assert c.get("/api/tnm/status").status_code == 401


def test_tnm_proxy_whitelist():
    """허용목록 밖 경로는 백엔드 호출 없이 404 (인증돼 있어도)."""
    with _client() as c:
        _login(c)
        for path in ("shutdown", "items/abc", "settings/raw", "watchx"):
            assert c.get(f"/api/tnm/{path}").status_code == 404, path
        # `..` 는 클라이언트/서버 정규화로 /api/tnm 밖 경로가 되어 프록시에
        # 도달하지 못한다(MCP 마운트 401 또는 404) — 어느 쪽이든 백엔드 미도달.
        for path in ("watch/../../etc", "collect/run/../x"):
            assert c.get(f"/api/tnm/{path}").status_code in (401, 404), path
        assert c.post("/api/tnm/watch/12345/exclude").status_code == 404  # 5자리
        assert c.post("/api/tnm/items/1/delete").status_code == 404


def test_tnm_proxy_allowed_paths_reach_backend():
    """허용 경로는 백엔드로 전달 시도 — 서비스 미가동이면 502 graceful."""
    with _client() as c:
        _login(c)
        for path in ("status", "items", "items/3", "watch", "settings"):
            r = c.get(f"/api/tnm/{path}")
            assert r.status_code == 502, path          # 테스트 환경엔 :8602 없음
            assert "연결 실패" in r.json().get("error", "")
        for path in ("watch/sync", "watch/005930/exclude", "items/3/label",
                     "collect/run"):
            assert c.post(f"/api/tnm/{path}", json={}).status_code == 502, path


def test_trading_proxy_unaffected():
    with _client() as c:
        _login(c)
        assert c.get("/api/trading/status").status_code == 502   # 기존 프록시 정상
        assert c.get("/api/trading/shutdown").status_code == 404


def test_journal_proxy_paths():
    """매매일지 경로만 통과하고 인접 경로는 백엔드에 닿지 않는다."""
    with _client() as c:
        _login(c)
        for path in ("journal", "journal/history"):
            assert c.get(f"/api/trading/{path}").status_code == 502, path
        assert c.post("/api/trading/journal/run", json={}).status_code == 502
        for path in ("journal/2026-07-27", "journal/delete", "journal/run"):
            assert c.get(f"/api/trading/{path}").status_code == 404, path
        assert c.post("/api/trading/journal", json={}).status_code == 404


def test_guard_override_proxy_paths():
    """가드 임시 해제는 POST 만, 지정 경로만 통과한다."""
    with _client() as c:
        _login(c)
        for path in ("guard/override", "guard/override/clear"):
            assert c.post(f"/api/trading/{path}", json={}).status_code == 502, path
            assert c.get(f"/api/trading/{path}").status_code == 404, path   # GET 불가
        for path in ("guard", "guard/override/x", "guard/disable"):
            assert c.post(f"/api/trading/{path}", json={}).status_code == 404, path
