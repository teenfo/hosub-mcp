"""대시보드 인증 경계 및 API 테스트."""

from __future__ import annotations

import json
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


# --- 역할 운영 · A/B 비교 ---
def test_role_ops_need_login(client):
    assert client.get("/api/llm/roles").status_code == 401
    assert client.post("/api/llm/roles",
                       json={"role": "x", "fields": {"model": "m"}}).status_code == 401
    assert client.delete("/api/llm/roles?role=x").status_code == 401
    assert client.post("/api/llm/compare",
                       json={"prompt": "x", "models": ["a", "b"]}).status_code == 401


def test_role_override_is_audited_as_medium(client, monkeypatch):
    from src import gateway

    calls = []
    monkeypatch.setattr(gateway, "set_role_override",
                        lambda role, fields, note=None:
                        calls.append((role, fields)) or
                        {"status": "ok", "role": {"name": role, "origin": "yaml"}})
    client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    r = client.post("/api/llm/roles",
                    json={"role": "summarize", "fields": {"model": "qwen3:8b"}})
    assert r.status_code == 200
    assert calls == [("summarize", {"model": "qwen3:8b"})]
    row = next(x for x in client.get("/api/audit?limit=5").json()["audit"]
               if x["tool"] == "llm_set_role")
    assert row["risk"] == "medium"


def test_new_db_role_is_audited_as_high(client, monkeypatch):
    """새 역할은 계약 추가라 기존 역할 수정보다 등급이 높다."""
    from src import gateway

    monkeypatch.setattr(gateway, "set_role_override",
                        lambda role, fields, note=None:
                        {"status": "ok", "role": {"name": role, "origin": "db"}})
    client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    client.post("/api/llm/roles", json={"role": "newbie", "fields": {"model": "m"}})
    row = next(x for x in client.get("/api/audit?limit=5").json()["audit"]
               if x["tool"] == "llm_set_role")
    assert row["risk"] == "high"


def test_role_save_requires_fields(client):
    client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert client.post("/api/llm/roles", json={"role": "x"}).status_code == 400
    assert client.post("/api/llm/roles", json={"fields": {"model": "m"}}).status_code == 400
    assert client.delete("/api/llm/roles?role=").status_code == 400


def test_role_delete_is_audited_as_high(client, monkeypatch):
    from src import gateway

    monkeypatch.setattr(gateway, "revert_role",
                        lambda role: {"status": "ok", "removed": True})
    client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    client.delete("/api/llm/roles?role=newbie")
    row = next(x for x in client.get("/api/audit?limit=5").json()["audit"]
               if x["tool"] == "llm_revert_role")
    assert row["risk"] == "high"


def test_compare_proxied_and_requires_two_models(client, monkeypatch):
    from src import gateway

    seen = {}

    def fake(prompt, models, system=None, options=None):
        seen.update(prompt=prompt, models=models, system=system)
        return {"status": "ok", "run": {"id": "r1"}, "done": False}

    monkeypatch.setattr(gateway, "compare_models", fake)
    client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert client.post("/api/llm/compare",
                       json={"prompt": "q", "models": ["a"]}).status_code == 400
    r = client.post("/api/llm/compare",
                    json={"prompt": "q", "models": ["a", "b"], "system": "s"})
    assert r.status_code == 200 and r.json()["run"]["id"] == "r1"
    assert seen == {"prompt": "q", "models": ["a", "b"], "system": "s"}


def test_llm_generate_passes_system_through(client, monkeypatch):
    """지금까지 대시보드가 system 을 버리고 있었다 — 역할 기본값밖에 못 썼다."""
    from src import gateway

    seen = {}

    def fake(role, prompt, system=None, **kw):
        seen.update(role=role, system=system)
        return {"status": "ok", "response": "네"}

    monkeypatch.setattr(gateway, "generate", fake)
    client.post("/login", data={"password": PASSWORD}, follow_redirects=False)

    client.post("/api/llm/generate", json={"role": "general", "prompt": "x"})
    assert seen["system"] is None                 # 키 생략 = 역할 기본값

    client.post("/api/llm/generate",
                json={"role": "general", "prompt": "x", "system": "너는 코치다"})
    assert seen["system"] == "너는 코치다"

    # 빈 문자열은 "시스템 프롬프트 없음" — None 과 다른 의미다
    client.post("/api/llm/generate",
                json={"role": "general", "prompt": "x", "system": ""})
    assert seen["system"] == ""


def test_integration_doc_needs_login(client):
    assert client.get("/api/llm/integration").status_code == 401


def test_integration_doc_proxied_as_markdown(client, monkeypatch):
    """게이트웨이는 JSON 이 아니라 text/markdown 을 준다 — 전용 경로가 필요하다."""
    from src import gateway

    monkeypatch.setattr(gateway, "integration_doc", lambda: {
        "status": "ok", "markdown": "# 소비 프로젝트 통합 가이드\n\n본문", "bytes": 42,
    })
    client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    d = client.get("/api/llm/integration").json()
    assert d["status"] == "ok"
    assert d["markdown"].startswith("# 소비 프로젝트 통합 가이드")
    assert d["bytes"] == 42


def test_integration_doc_surfaces_gateway_error(client, monkeypatch):
    from src import gateway

    monkeypatch.setattr(gateway, "integration_doc", lambda: {
        "status": "error", "error": "ConnectError: 연결 실패", "hint": "게이트웨이 확인",
    })
    client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    d = client.get("/api/llm/integration").json()
    assert d["status"] == "error" and "연결 실패" in d["error"]


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


# --- 계약 메타데이터 · 스펙 · 클라이언트 원본 ---
#
# 브라우저가 게이트웨이를 직접 부르면 소비자 토큰이 노출되므로 전부 여기서
# 프록시한다. 순수 읽기라 감사 로그를 남기지 않는다(api_llm_integration 과 같은 취급).
def test_meta_openapi_client_need_login(client):
    for path in ("/api/llm/meta", "/api/llm/openapi", "/api/llm/client"):
        assert client.get(path).status_code == 401, path


def test_meta_exposes_the_client_hash(client, monkeypatch):
    """화면에서 값어치 있는 것은 사본 드리프트를 잡는 sha256 이다."""
    from src import gateway

    monkeypatch.setattr(gateway, "meta", lambda: {
        "status": "ok", "contract_version": "1.0",
        "client": {"files": {"python": {"available": True, "sha256": "abc123",
                                       "bytes": 13736}}},
    })
    client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    d = client.get("/api/llm/meta").json()
    assert d["client"]["files"]["python"]["sha256"] == "abc123"


def test_openapi_passes_the_format(client, monkeypatch):
    from src import gateway

    seen = {}

    def fake(fmt="json"):
        seen["fmt"] = fmt
        return {"status": "ok", "text": "openapi: 3.1.0\n", "bytes": 15}

    monkeypatch.setattr(gateway, "openapi", fake)
    client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert client.get("/api/llm/openapi?fmt=yaml").json()["status"] == "ok"
    assert seen["fmt"] == "yaml"
    client.get("/api/llm/openapi")
    assert seen["fmt"] == "json"      # 기본값


def test_client_source_is_carried_inside_json(client, monkeypatch):
    """비-JSON 을 그대로 태울 수 없으므로 문자열로 감싸 넘긴다."""
    from src import gateway

    seen = {}

    def fake(name="llmgw.py"):
        seen["name"] = name
        return {"status": "ok", "text": '"""클라이언트."""\n', "bytes": 20}

    monkeypatch.setattr(gateway, "client_file", fake)
    client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    d = client.get("/api/llm/client?name=mock_gateway.py").json()
    assert d["text"].startswith('"""클라이언트."""')
    assert seen["name"] == "mock_gateway.py"


def test_contract_reads_are_not_audited(monkeypatch, audit):
    """조회는 감사 로그를 남기지 않는다 — 변경 계열만 남긴다."""
    from src import gateway

    monkeypatch.setattr(gateway, "meta", lambda: {"status": "ok"})
    monkeypatch.setattr(gateway, "openapi", lambda fmt="json": {"status": "ok"})
    monkeypatch.setattr(gateway, "client_file",
                        lambda name="llmgw.py": {"status": "ok"})
    app = build_app(
        registry=Registry.from_dict(REG),
        runner=FakeRunner(),
        audit=audit,
        mcp_token=TOKEN,
        dash_password=PASSWORD,
        session_secret="session-secret-abcdefgh",
    )
    with TestClient(app) as c:
        c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        before = len(audit.recent(50))
        c.get("/api/llm/meta")
        c.get("/api/llm/openapi")
        c.get("/api/llm/client")
    assert len(audit.recent(50)) == before


# --- jw-mcp (별개 프로세스, 루프백 전용 API) ---
#
# 브라우저는 jw-mcp 를 직접 못 부른다(Caddy 가 /api/dash/* 를 라우팅하지 않음).
# 그래서 대시보드가 서버사이드로 프록시하고, 그 경계를 여기서 지킨다.
def test_jw_routes_need_login(client):
    for path in ("/api/jw/summary", "/api/jw/users", "/api/jw/usage", "/api/jw/calls"):
        assert client.get(path).status_code == 401, path
    assert client.post("/api/jw/users/status",
                       json={"user_id": "u_" + "0" * 24, "status": "active"}
                       ).status_code == 401


def test_jw_summary_passes_sections_through(client, monkeypatch):
    """sections 를 해석하지 않는다 — jw-mcp 가 지표를 바꿔도 이쪽은 그대로다."""
    from src import jw

    monkeypatch.setattr(jw, "summary", lambda: {
        "status": "ok", "version": "2.0.0",
        "sections": [{"title": "사용자", "icon": "bi-people",
                      "items": [{"label": "승인 대기", "value": 1, "tone": "warn"}]}],
    })
    client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    d = client.get("/api/jw/summary").json()
    assert d["sections"][0]["items"][0]["tone"] == "warn"


def test_jw_usage_and_calls_pass_their_bounds(client, monkeypatch):
    from src import jw

    seen = {}
    monkeypatch.setattr(jw, "usage", lambda days=7: seen.update(days=days) or {"status": "ok"})
    monkeypatch.setattr(jw, "calls", lambda limit=50: seen.update(limit=limit) or {"status": "ok"})
    client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    client.get("/api/jw/usage?days=30")
    client.get("/api/jw/calls?limit=5")
    assert seen == {"days": 30, "limit": 5}
    # 숫자가 아니면 기본값으로 떨어진다(500 이 아니라)
    client.get("/api/jw/usage?days=abc")
    client.get("/api/jw/calls?limit=abc")
    assert seen == {"days": 7, "limit": 50}


def test_jw_service_down_is_reported_not_crashed(client, monkeypatch):
    """jw-mcp 는 따로 죽는다 — 그때 대시보드가 500 을 내면 안 된다."""
    from src import jw

    monkeypatch.setattr(jw, "summary", lambda: {
        "status": "error", "error": "ConnectError: 연결 실패",
        "hint": "systemctl status jw-mcp",
    })
    client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    r = client.get("/api/jw/summary")
    assert r.status_code == 200
    assert r.json()["status"] == "error" and "연결 실패" in r.json()["error"]


def test_jw_status_change_requires_both_fields(client):
    client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    for body in ({}, {"user_id": "u_" + "0" * 24}, {"status": "active"}):
        r = client.post("/api/jw/users/status", json=body)
        assert r.status_code == 400, body
        assert r.json()["status"] == "rejected"


def test_jw_status_change_is_audited(monkeypatch, audit):
    """변경 계열이므로 감사에 남긴다. 비활성화는 토큰을 폐기하므로 risk=high."""
    from src import jw

    monkeypatch.setattr(jw, "set_user_status",
                        lambda uid, st: {"status": "ok", "user": {"user_id": uid}})
    app = build_app(
        registry=Registry.from_dict(REG),
        runner=FakeRunner(),
        audit=audit,
        mcp_token=TOKEN,
        dash_password=PASSWORD,
        session_secret="session-secret-abcdefgh",
    )
    uid = "u_4196f60425d3fcb8eb59f26b"
    with TestClient(app) as c:
        c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        c.post("/api/jw/users/status", json={"user_id": uid, "status": "disabled"})
        c.post("/api/jw/users/status", json={"user_id": uid, "status": "active"})

    rows = [r for r in audit.recent(50) if r["tool"] == "jw_set_user_status"]
    assert len(rows) == 2
    risks = {json.loads(r["params_json"])["status"]: r["risk"] for r in rows}
    assert risks == {"disabled": "high", "active": "medium"}
    # 이메일·표시이름은 감사에 남기지 않는다 — user_id 로 충분히 추적된다
    assert all("email" not in r["params_json"] for r in rows)
