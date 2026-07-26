"""LLM 게이트웨이 테스트 (레지스트리 검증 + 라우팅 + 실패 처리).

실제 Mac/Ollama 없이 동작하도록 httpx.Client 를 가짜로 주입한다.
"""

from __future__ import annotations

import pytest

from src.llm import LLMConfigError, LLMRegistry, backend_status, generate

VALID = {
    "backends": {
        "mac": {"base_url": "http://100.107.151.46:11434", "type": "ollama"},
        "local": {"base_url": "http://127.0.0.1:11434"},
    },
    "default_backend": "mac",
    "roles": {
        "summarize": {
            "model": "qwen2.5:7b",
            "description": "요약",
            "system": "요약해라",
            "options": {"temperature": 0.3},
            "timeout": 60,
        },
        "code": {"backend": "mac", "model": "qwen2.5-coder:14b"},
        "general": {"model": "qwen2.5:32b"},
    },
    "fallback_role": "general",
}


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeClient:
    """httpx.Client 대체. 마지막 요청을 기록한다."""

    last_url = None
    last_json = None
    post_payload = {"response": "안녕하세요", "eval_count": 12, "total_duration": 2_500_000_000}
    get_payload = {"models": [{"name": "qwen2.5:7b"}, {"name": "qwen2.5:32b"}]}
    raise_on_call = None

    def __init__(self, timeout=None):
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None):
        if FakeClient.raise_on_call:
            raise FakeClient.raise_on_call
        FakeClient.last_url = url
        FakeClient.last_json = json
        return FakeResponse(FakeClient.post_payload)

    def get(self, url):
        if FakeClient.raise_on_call:
            raise FakeClient.raise_on_call
        FakeClient.last_url = url
        return FakeResponse(FakeClient.get_payload)


@pytest.fixture(autouse=True)
def _reset_fake():
    FakeClient.raise_on_call = None
    FakeClient.last_url = None
    FakeClient.last_json = None
    yield


# --- 레지스트리 로드/검증 ---
def test_load_valid():
    reg = LLMRegistry.from_dict(VALID)
    assert reg.role_names == ["code", "general", "summarize"]
    assert reg.role("summarize").model == "qwen2.5:7b"
    assert reg.role("summarize").backend == "mac"      # default_backend 적용
    assert reg.fallback_role == "general"


def test_missing_model_rejected():
    with pytest.raises(LLMConfigError):
        LLMRegistry.from_dict({"backends": {"mac": {"base_url": "http://x"}},
                               "roles": {"bad": {"backend": "mac"}}})


def test_unknown_backend_rejected():
    with pytest.raises(LLMConfigError):
        LLMRegistry.from_dict({"backends": {"mac": {"base_url": "http://x"}},
                               "roles": {"r": {"backend": "nope", "model": "m"}}})


def test_backend_without_base_url_rejected():
    with pytest.raises(LLMConfigError):
        LLMRegistry.from_dict({"backends": {"mac": {}}, "roles": {}})


def test_bad_fallback_rejected():
    bad = dict(VALID, fallback_role="nope")
    with pytest.raises(LLMConfigError):
        LLMRegistry.from_dict(bad)


def test_missing_file_gives_empty_registry(tmp_path):
    reg = LLMRegistry.load(tmp_path / "none.yaml")
    assert reg.role_names == []


def test_shipped_registry_loads():
    # 저장소에 커밋된 실제 설정이 유효해야 한다
    reg = LLMRegistry.load("config/llm_registry.yaml")
    assert "summarize" in reg.role_names
    assert "general" in reg.role_names


# --- 라우팅/생성 ---
def test_generate_routes_to_role_model():
    reg = LLMRegistry.from_dict(VALID)
    out = generate(reg, "code", "hello", client_factory=FakeClient)
    assert out["status"] == "ok"
    assert out["model"] == "qwen2.5-coder:14b"
    assert out["response"] == "안녕하세요"
    assert FakeClient.last_url == "http://100.107.151.46:11434/api/generate"
    assert FakeClient.last_json["model"] == "qwen2.5-coder:14b"
    assert FakeClient.last_json["stream"] is False


def test_generate_applies_system_and_options():
    reg = LLMRegistry.from_dict(VALID)
    generate(reg, "summarize", "본문", client_factory=FakeClient)
    assert FakeClient.last_json["system"] == "요약해라"
    assert FakeClient.last_json["options"] == {"temperature": 0.3}


def test_generate_system_override():
    reg = LLMRegistry.from_dict(VALID)
    generate(reg, "summarize", "본문", system="다르게", client_factory=FakeClient)
    assert FakeClient.last_json["system"] == "다르게"


def test_generate_unknown_role_rejected():
    reg = LLMRegistry.from_dict(VALID)
    out = generate(reg, "nope", "x", client_factory=FakeClient)
    assert out["status"] == "rejected"
    assert "general" in out["known_roles"]


def test_generate_backend_failure_graceful():
    reg = LLMRegistry.from_dict(VALID)
    FakeClient.raise_on_call = ConnectionError("connection refused")
    out = generate(reg, "general", "x", client_factory=FakeClient)
    assert out["status"] == "error"
    assert "connection refused" in out["error"]
    assert "OLLAMA_HOST" in out["hint"]


def test_generate_duration_converted_to_ms():
    reg = LLMRegistry.from_dict(VALID)
    out = generate(reg, "general", "x", client_factory=FakeClient)
    assert out["total_duration_ms"] == 2500


# --- 상태 조회 ---
def test_backend_status_marks_model_availability():
    reg = LLMRegistry.from_dict(VALID)
    st = backend_status(reg, client_factory=FakeClient)
    by_name = {r["name"]: r for r in st["roles"]}
    assert by_name["summarize"]["model_available"] is True     # qwen2.5:7b 보유
    assert by_name["code"]["model_available"] is False         # coder 미보유
    assert st["backends"][0]["online"] is True


def test_backend_status_offline_graceful():
    reg = LLMRegistry.from_dict(VALID)
    FakeClient.raise_on_call = TimeoutError("timed out")
    st = backend_status(reg, client_factory=FakeClient)
    assert all(b["online"] is False for b in st["backends"])
    assert all(r["model_available"] is False for r in st["roles"])
