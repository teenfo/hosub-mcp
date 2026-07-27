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


# --- 모델 설치 요청 (게이트웨이 프록시) ---
def test_model_requests_need_login(client):
    assert client.get("/api/llm/models").status_code == 401
    assert client.post("/api/llm/models/decide",
                       json={"model": "m", "action": "approve"}).status_code == 401


def test_model_requests_proxied_to_gateway(client, monkeypatch):
    from src import gateway

    seen = {}

    def fake_list(status=None):
        seen["status"] = status
        return {"status": "ok", "requests": [], "can_decide": True}

    monkeypatch.setattr(gateway, "list_model_requests", fake_list)
    client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    body = client.get("/api/llm/models?status=pending").json()
    assert body["can_decide"] is True
    assert seen["status"] == "pending"


def test_model_decision_is_audited(client, monkeypatch):
    from src import gateway

    calls = []
    monkeypatch.setattr(gateway, "decide_model_request",
                        lambda m, a: calls.append((m, a)) or
                        {"status": "ok", "request": {"model": m, "status": "approved"}})
    client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    r = client.post("/api/llm/models/decide", json={"model": "qwen3:32b", "action": "approve"})
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert calls == [("qwen3:32b", "approve")]

    rows = client.get("/api/audit?limit=5").json()["audit"]
    assert any(row["tool"] == "llm_decide_model" for row in rows)


def test_model_decision_requires_model(client):
    client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    r = client.post("/api/llm/models/decide", json={"action": "approve"})
    assert r.status_code == 400


# --- LLM: 맥 직접 호출이 아니라 게이트웨이 경유 ---
def test_llm_status_comes_from_gateway(client, monkeypatch):
    from src import gateway

    monkeypatch.setattr(gateway, "status", lambda: {
        "status": "ok",
        "backend": {"base_url": "http://100.69.201.28:11434", "online": True,
                    "models": ["qwen2.5:7b"], "loaded_model": None, "error": None},
        "lanes": {"interactive": {"queued": 0, "running": 1}},
        "roles": [{"name": "summarize", "model": "qwen2.5:7b", "lane": "interactive",
                   "timeout": 120, "model_available": True}],
        "usage": [{"service": "hosub", "calls": 3, "tokens": 40, "ok": 3}],
        "mem_budget_gb": 40,
    })
    client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    d = client.get("/api/llm/status").json()
    assert d["backend"]["online"] is True
    assert d["lanes"]["interactive"]["running"] == 1
    assert d["usage"][0]["service"] == "hosub"


def test_llm_generate_goes_through_gateway_and_is_audited(client, monkeypatch):
    from src import gateway

    seen = {}

    def fake_generate(role, prompt, **kw):
        seen.update(role=role, prompt=prompt)
        return {"job_id": "j9", "status": "ok", "response": "요약", "model": "qwen2.5:7b"}

    monkeypatch.setattr(gateway, "generate", fake_generate)
    client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    r = client.post("/api/llm/generate", json={"role": "summarize", "prompt": "긴 글"})
    assert r.json()["response"] == "요약"
    assert seen == {"role": "summarize", "prompt": "긴 글"}

    rows = client.get("/api/audit?limit=5").json()["audit"]
    assert any(row["tool"] == "llm_generate" for row in rows)


def test_llm_job_route_polls_pending_result(client, monkeypatch):
    from src import gateway

    monkeypatch.setattr(gateway, "get_job",
                        lambda jid: {"job_id": jid, "status": "ok", "response": "늦게 옴"})
    client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert client.get("/api/llm/jobs/j9").json()["response"] == "늦게 옴"


def test_llm_routes_need_login(client):
    assert client.get("/api/llm/status").status_code == 401
    assert client.get("/api/llm/jobs/x").status_code == 401
    assert client.post("/api/llm/generate", json={"prompt": "x"}).status_code == 401


def test_llm_generate_rejects_empty_prompt(client):
    client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    r = client.post("/api/llm/generate", json={"prompt": "   "})
    assert r.status_code == 400


# --- 모델 운영 (게이트웨이 /v1/admin/* 프록시) ---
def test_model_ops_routes_need_login(client):
    assert client.get("/api/llm/installed").status_code == 401
    assert client.get("/api/llm/catalog").status_code == 401
    assert client.post("/api/llm/models/install", json={"model": "m"}).status_code == 401
    assert client.delete("/api/llm/models/delete?model=m").status_code == 401


def test_installed_models_proxied(client, monkeypatch):
    from src import gateway

    monkeypatch.setattr(gateway, "list_installed_models", lambda: {
        "status": "ok", "total_size_gb": 12.5,
        "models": [{"name": "qwen2.5:7b", "size_gb": 4.5, "roles": ["summarize"],
                    "blockers": [{"kind": "roles", "message": "쓰는 역할이 있습니다"}],
                    "calls_30d": 3, "last_used": None}],
    })
    client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    d = client.get("/api/llm/installed").json()
    assert d["total_size_gb"] == 12.5
    assert d["models"][0]["blockers"][0]["kind"] == "roles"


def test_model_install_is_audited_as_medium(client, monkeypatch):
    from src import gateway

    calls = []
    monkeypatch.setattr(gateway, "install_model",
                        lambda m: calls.append(m) or {"status": "approved"})
    client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert client.post("/api/llm/models/install",
                       json={"model": "qwen3:4b"}).status_code == 200
    assert calls == ["qwen3:4b"]
    row = next(r for r in client.get("/api/audit?limit=5").json()["audit"]
               if r["tool"] == "llm_install_model")
    assert row["risk"] == "medium"


def test_model_delete_is_audited_as_high(client, monkeypatch):
    """되돌리려면 수십 GB 를 다시 받아야 한다 — 감사 등급이 승인보다 높아야 한다."""
    from src import gateway

    calls = []
    monkeypatch.setattr(gateway, "delete_model",
                        lambda m: calls.append(m) or
                        {"status": "deleted", "model": m, "freed_gb": 4.5})
    client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    r = client.delete("/api/llm/models/delete?model=old:7b")
    assert r.status_code == 200 and r.json()["status"] == "deleted"
    assert calls == ["old:7b"]
    row = next(r for r in client.get("/api/audit?limit=5").json()["audit"]
               if r["tool"] == "llm_delete_model")
    assert row["risk"] == "high"


def test_model_ops_require_model_param(client):
    client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert client.post("/api/llm/models/install", json={}).status_code == 400
    assert client.delete("/api/llm/models/delete?model=").status_code == 400


def test_catalog_search_passes_query(client, monkeypatch):
    from src import gateway

    seen = {}

    def fake(query="", kind=None):
        seen.update(query=query, kind=kind)
        return {"status": "ok", "models": [], "installed": []}

    monkeypatch.setattr(gateway, "search_catalog", fake)
    client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    client.get("/api/llm/catalog?q=qwen&kind=embed")
    assert seen == {"query": "qwen", "kind": "embed"}
