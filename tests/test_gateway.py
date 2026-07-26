"""공유 LLM 게이트웨이 클라이언트 + 모델 승인 경로(대시보드·MCP 도구).

실제 게이트웨이 없이 httpx.Client 를 가짜로 주입해 검증한다.
"""

from __future__ import annotations

import json

import pytest

from src import gateway


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.content = json.dumps(payload).encode()
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeClient:
    """httpx.Client 대체. 마지막 요청을 기록한다."""

    calls: list[tuple] = []
    payload: dict = {"requests": [], "can_decide": True}
    status: int = 200
    raise_on_call = None

    def __init__(self, timeout=None):
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def request(self, method, url, json=None, headers=None):
        if FakeClient.raise_on_call:
            raise FakeClient.raise_on_call
        FakeClient.calls.append((method, url, json, headers))
        return FakeResponse(FakeClient.payload, FakeClient.status)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    FakeClient.calls = []
    FakeClient.payload = {"requests": [], "can_decide": True}
    FakeClient.status = 200
    FakeClient.raise_on_call = None
    monkeypatch.setenv("LLMGW_TOKEN_HOSUB", "t" * 40)
    monkeypatch.delenv("LLMGW_URL", raising=False)
    yield


# --- 클라이언트 ---
def test_list_sends_bearer_token_to_gateway():
    out = gateway.list_model_requests(client_factory=FakeClient)
    method, url, body, headers = FakeClient.calls[0]
    assert method == "GET"
    assert url == "http://127.0.0.1:8603/v1/models/requests"
    assert headers["Authorization"] == "Bearer " + "t" * 40
    assert out["status"] == "ok" and out["can_decide"] is True


def test_list_passes_status_filter():
    gateway.list_model_requests("pending", client_factory=FakeClient)
    assert FakeClient.calls[0][1].endswith("/v1/models/requests?status=pending")


def test_decide_posts_action():
    FakeClient.payload = {"request": {"model": "m", "status": "approved"}}
    out = gateway.decide_model_request("m", "approve", client_factory=FakeClient)
    method, url, body, _ = FakeClient.calls[0]
    assert (method, body) == ("POST", {"model": "m", "action": "approve"})
    assert out["request"]["status"] == "approved"


def test_decide_rejects_unknown_action():
    out = gateway.decide_model_request("m", "explode", client_factory=FakeClient)
    assert out["status"] == "rejected"
    assert FakeClient.calls == []          # 게이트웨이를 부르지도 않는다


def test_missing_token_is_reported_not_raised(monkeypatch):
    monkeypatch.delenv("LLMGW_TOKEN_HOSUB", raising=False)
    out = gateway.list_model_requests(client_factory=FakeClient)
    assert out["status"] == "unconfigured"
    assert out["requests"] == []
    assert FakeClient.calls == []


def test_gateway_down_is_reported_not_raised():
    FakeClient.raise_on_call = OSError("connection refused")
    out = gateway.list_model_requests(client_factory=FakeClient)
    assert out["status"] == "error"
    assert "connection refused" in out["error"]
    assert "게이트웨이" in out["hint"]


# --- 추론 경로 (맥을 직접 부르지 않고 게이트웨이를 거친다) ---
def test_generate_posts_role_and_prompt():
    FakeClient.payload = {"job_id": "j1", "status": "ok", "response": "요약본",
                          "model": "qwen2.5:7b", "lane": "interactive"}
    out = gateway.generate("summarize", "긴 문서", client_factory=FakeClient)
    method, url, body, _ = FakeClient.calls[0]
    assert method == "POST" and url.endswith("/v1/generate")
    assert body["role"] == "summarize" and body["prompt"] == "긴 문서"
    assert "system" not in body           # 안 주면 역할 기본 프롬프트가 쓰인다
    assert out["status"] == "ok" and out["response"] == "요약본"


def test_generate_forwards_caller_system_prompt():
    gateway.generate("analyze_workout", "데이터", system="너는 코치다",
                     wait=0, metadata={"session_id": 7}, client_factory=FakeClient)
    body = FakeClient.calls[0][2]
    assert body["system"] == "너는 코치다"
    assert body["wait"] == 0
    assert body["metadata"] == {"session_id": 7}


def test_generate_pending_is_not_an_error():
    """긴 잡은 pending 으로 돌아온다 — 실패가 아니다."""
    FakeClient.payload = {"job_id": "j2", "status": "pending", "model": "qwen2.5:32b",
                          "queue_position": 2}
    out = gateway.generate("general", "x", wait=0, client_factory=FakeClient)
    assert out["status"] == "pending" and out["job_id"] == "j2"


def test_get_job_fetches_result():
    FakeClient.payload = {"job_id": "j2", "status": "ok", "response": "끝"}
    out = gateway.get_job("j2", client_factory=FakeClient)
    assert FakeClient.calls[0][1].endswith("/v1/jobs/j2")
    assert out["response"] == "끝"


def test_roles_and_status_are_read_only_gets():
    FakeClient.payload = {"roles": [{"name": "summarize", "model": "qwen2.5:7b"}]}
    assert gateway.list_roles(client_factory=FakeClient)["roles"][0]["name"] == "summarize"
    FakeClient.payload = {"backend": {"online": True}, "lanes": {}, "roles": []}
    assert gateway.status(client_factory=FakeClient)["backend"]["online"] is True
    assert [c[0] for c in FakeClient.calls] == ["GET", "GET"]


def test_http_error_surfaces_detail():
    FakeClient.status = 403
    FakeClient.payload = {"error": "forbidden", "detail": "권한 없음"}
    out = gateway.decide_model_request("m", "approve", client_factory=FakeClient)
    assert out["status"] == "error" and out["http_status"] == 403
    assert out["error"] == "권한 없음"


def test_base_url_is_overridable(monkeypatch):
    monkeypatch.setenv("LLMGW_URL", "http://gw.internal:9999/")
    gateway.list_model_requests(client_factory=FakeClient)
    assert FakeClient.calls[0][1].startswith("http://gw.internal:9999/v1/")


# --- MCP 도구 ---
PENDING = {
    "status": "ok", "can_decide": True,
    "requests": [{"model": "qwen3:32b", "status": "pending", "roles": ["novel"],
                  "requested_by": "roxlogy", "est_size_gb": 21.3, "progress": 0}],
}


@pytest.fixture
def mcp_server(tmp_path):
    from src.audit import AuditLog
    from src.registry import Registry
    from src.server import build_context, build_mcp
    from tests.conftest import FakeRunner

    ctx = build_context(Registry.from_dict({}), FakeRunner(),
                        AuditLog(tmp_path / "audit.db"))
    return build_mcp(ctx)


async def _call(mcp, name, args):
    result = await mcp.call_tool(name, args)
    if isinstance(result, list) and result and hasattr(result[0], "text"):
        return json.loads(result[0].text)
    if isinstance(result, dict):
        return result
    raise AssertionError(f"unexpected tool result: {result!r}")


@pytest.mark.asyncio
async def test_model_requests_tool_hints_pending(mcp_server, monkeypatch):
    monkeypatch.setattr(gateway, "list_model_requests", lambda status=None: dict(PENDING))
    out = await _call(mcp_server, "llm_model_requests", {})
    assert out["requests"][0]["model"] == "qwen3:32b"
    assert "승인 대기 1건" in out["hint"]


@pytest.mark.asyncio
async def test_decide_tool_requires_confirm(mcp_server, monkeypatch):
    called = []
    monkeypatch.setattr(gateway, "list_model_requests", lambda status=None: dict(PENDING))
    monkeypatch.setattr(gateway, "decide_model_request",
                        lambda m, a: called.append((m, a)) or {"status": "ok"})

    out = await _call(mcp_server, "llm_decide_model", {"model": "qwen3:32b"})
    assert out["status"] == "approval_required"
    assert out["risk"] == "medium"
    assert "21.3GB" in out["action"]
    assert called == []                    # 승인 전에는 게이트웨이를 부르지 않는다

    out = await _call(mcp_server, "llm_decide_model",
                      {"model": "qwen3:32b", "confirm": True})
    assert out["status"] == "ok"
    assert called == [("qwen3:32b", "approve")]


@pytest.mark.asyncio
async def test_decide_tool_rejects_unknown_model(mcp_server, monkeypatch):
    monkeypatch.setattr(gateway, "list_model_requests", lambda status=None: dict(PENDING))
    out = await _call(mcp_server, "llm_decide_model",
                      {"model": "ghost:7b", "confirm": True})
    assert out["status"] == "rejected"
    assert out["known_models"] == ["qwen3:32b"]


@pytest.mark.asyncio
async def test_decide_tool_rejects_bad_action(mcp_server):
    out = await _call(mcp_server, "llm_decide_model",
                      {"model": "qwen3:32b", "action": "delete", "confirm": True})
    assert out["status"] == "rejected"


@pytest.mark.asyncio
async def test_generate_tool_goes_through_gateway(mcp_server, monkeypatch):
    """맥을 직접 부르지 않고 게이트웨이를 거쳐야 한다."""
    seen = {}

    def fake_generate(role, prompt, *, system=None, wait=120, metadata=None):
        seen.update(role=role, prompt=prompt, system=system, wait=wait)
        return {"job_id": "j1", "status": "ok", "response": "결과",
                "model": "qwen2.5:7b", "lane": "interactive"}

    monkeypatch.setattr(gateway, "generate", fake_generate)
    out = await _call(mcp_server, "llm_generate",
                      {"prompt": "안녕", "role": "summarize", "system": "간결히"})
    assert out["status"] == "ok" and out["response"] == "결과"
    assert seen == {"role": "summarize", "prompt": "안녕",
                    "system": "간결히", "wait": 120}


@pytest.mark.asyncio
async def test_generate_tool_clamps_wait_and_guides_on_pending(mcp_server, monkeypatch):
    seen = {}

    def fake_generate(role, prompt, *, system=None, wait=120, metadata=None):
        seen["wait"] = wait
        return {"job_id": "abc", "status": "pending", "model": "qwen2.5:32b"}

    monkeypatch.setattr(gateway, "generate", fake_generate)
    out = await _call(mcp_server, "llm_generate", {"prompt": "x", "wait": 9999})
    assert seen["wait"] == 300                      # 게이트웨이 상한으로 클램프
    assert out["status"] == "pending"
    assert "llm_job" in out["hint"] and "abc" in out["hint"]


@pytest.mark.asyncio
async def test_job_tool_fetches_pending_result(mcp_server, monkeypatch):
    monkeypatch.setattr(gateway, "get_job",
                        lambda jid: {"job_id": jid, "status": "ok", "response": "늦게 옴"})
    out = await _call(mcp_server, "llm_job", {"job_id": "abc"})
    assert out["response"] == "늦게 옴"


@pytest.mark.asyncio
async def test_status_and_roles_tools_use_gateway(mcp_server, monkeypatch):
    monkeypatch.setattr(gateway, "status",
                        lambda: {"status": "ok", "backend": {"online": True}, "roles": []})
    monkeypatch.setattr(gateway, "list_roles", lambda: {"status": "ok", "roles": []})
    assert (await _call(mcp_server, "llm_status", {}))["backend"]["online"] is True
    out = await _call(mcp_server, "llm_list_roles", {})
    assert "roles.yaml" in out["hint"]              # 비었을 때 어디를 고칠지 알려준다
