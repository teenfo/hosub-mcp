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
