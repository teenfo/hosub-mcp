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


# --- 통합 가이드 (유일하게 JSON 이 아닌 엔드포인트) ---
class MarkdownResponse:
    """text/markdown 응답. json() 을 부르면 터진다 — 실제 게이트웨이와 같다."""

    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status
        self.content = text.encode()

    def json(self):
        raise ValueError("not json")


class MarkdownClient:
    text = "# 소비 프로젝트 통합 가이드\n\n한글 본문"
    status = 200
    calls: list = []

    def __init__(self, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def request(self, method, url, json=None, headers=None):
        MarkdownClient.calls.append((method, url, headers))
        return MarkdownResponse(MarkdownClient.text, MarkdownClient.status)


def test_integration_doc_returns_markdown_not_json():
    MarkdownClient.calls = []
    MarkdownClient.status = 200
    out = gateway.integration_doc(client_factory=MarkdownClient)
    assert out["status"] == "ok"
    assert out["markdown"].startswith("# 소비 프로젝트 통합 가이드")
    # 바이트 수는 UTF-8 기준 — 한글이 있으면 글자 수보다 크다
    assert out["bytes"] == len(MarkdownClient.text.encode("utf-8"))
    assert out["bytes"] > len(MarkdownClient.text)
    method, url, headers = MarkdownClient.calls[0]
    assert method == "GET" and url.endswith("/v1/integration")
    assert headers["Authorization"].startswith("Bearer ")


def test_integration_doc_reports_http_error():
    MarkdownClient.calls = []
    MarkdownClient.status = 401
    out = gateway.integration_doc(client_factory=MarkdownClient)
    assert out["status"] == "error" and out["http_status"] == 401


def test_integration_doc_unconfigured_without_token(monkeypatch):
    monkeypatch.delenv("LLMGW_TOKEN_HOSUB", raising=False)
    out = gateway.integration_doc(client_factory=MarkdownClient)
    assert out["status"] == "unconfigured"


# --- 메타데이터 · 스펙 · 클라이언트 원본 ---
#
# 뒤 둘은 JSON 이 아니므로 _call 을 쓸 수 없다(res.json() 이 터진다).
# integration_doc 과 같은 방식으로 문자열을 dict 에 감싸 넘긴다 — 모듈 규약이
# "실패해도 예외 대신 status/error dict" 이기 때문이다.
def test_meta_goes_through_the_normal_json_path():
    out = gateway.meta(client_factory=FakeClient)
    assert out["status"] == "ok"
    method, url, _json, _headers = FakeClient.calls[-1]
    assert method == "GET" and url.endswith("/v1/meta")


def test_openapi_returns_raw_text():
    MarkdownClient.calls = []
    MarkdownClient.status = 200
    MarkdownClient.text = "openapi: 3.1.0\ninfo:\n  title: 한글\n"
    out = gateway.openapi("yaml", client_factory=MarkdownClient)
    assert out["status"] == "ok"
    assert out["text"].startswith("openapi: 3.1.0")
    assert out["bytes"] == len(MarkdownClient.text.encode("utf-8"))
    _method, url, _headers = MarkdownClient.calls[0]
    assert url.endswith("/v1/openapi.yaml")


def test_openapi_rejects_unknown_format():
    out = gateway.openapi("xml", client_factory=MarkdownClient)
    assert out["status"] == "error" and "json" in out["error"]


def test_client_file_serves_the_two_known_names():
    for name in ("llmgw.py", "mock_gateway.py"):
        MarkdownClient.calls = []
        MarkdownClient.status = 200
        MarkdownClient.text = '"""공유 LLM 게이트웨이 클라이언트."""\n'
        out = gateway.client_file(name, client_factory=MarkdownClient)
        assert out["status"] == "ok", name
        _method, url, _headers = MarkdownClient.calls[0]
        assert url.endswith(f"/v1/client/{name}")


def test_client_file_rejects_anything_else():
    """게이트웨이가 리터럴 라우트 둘만 노출하므로 여기서도 허용 목록으로 막는다."""
    for bad in ("../app/main.py", "secrets.py", "", "llmgw.py?x=1"):
        out = gateway.client_file(bad, client_factory=MarkdownClient)
        assert out["status"] == "error", bad


def test_text_fetchers_report_http_error():
    MarkdownClient.calls = []
    MarkdownClient.status = 404
    MarkdownClient.text = "not found"
    out = gateway.client_file("llmgw.py", client_factory=MarkdownClient)
    assert out["status"] == "error" and out["http_status"] == 404


def test_text_fetchers_are_unconfigured_without_a_token(monkeypatch):
    monkeypatch.delenv("LLMGW_TOKEN_HOSUB", raising=False)
    assert gateway.openapi("json",
                           client_factory=MarkdownClient)["status"] == "unconfigured"
    assert gateway.client_file("llmgw.py",
                              client_factory=MarkdownClient)["status"] == "unconfigured"


# --- 소비자 토큰 관측 ---
def test_list_services_clamps_the_window():
    for given, expected in ((7, 7), (0, 7), (1, 1), (90, 90), (999, 90)):
        gateway.list_services(given, client_factory=FakeClient)
        assert FakeClient.calls[-1][1].endswith(f"/v1/admin/services?days={expected}")


def test_reveal_token_targets_one_service():
    gateway.reveal_token("roxlogy", client_factory=FakeClient)
    method, url, _json, _headers = FakeClient.calls[-1]
    assert method == "GET"
    assert url.endswith("/v1/admin/services/roxlogy/token")


def test_reveal_token_rejects_odd_names_before_the_network():
    """대시보드가 임의 경로를 만들어 주지 않는다."""
    before = len(FakeClient.calls)
    for bad in ("", "../admin/roles", "a b", "roxlogy/../hosub", "x?y=1"):
        out = gateway.reveal_token(bad, client_factory=FakeClient)
        assert out["status"] == "error", bad
    assert len(FakeClient.calls) == before
