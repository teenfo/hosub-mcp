"""jw-mcp 대시보드 연동 클라이언트.

실제 jw-mcp 없이 httpx.Client 를 가짜로 주입해 검증한다. 여기서 지키는 것은
세 가지다 — **jw-mcp 가 꺼져 있어도 안 깨진다**, **인증 헤더를 정확히 보낸다**,
**변경 작업의 입력을 클라이언트에서 먼저 막는다**.
"""

from __future__ import annotations

import json

import pytest

from src import jw

TOKEN = "j" * 40
USER_OK = "u_4196f60425d3fcb8eb59f26b"


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
    payload: dict = {"ok": True}
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
    monkeypatch.setenv("HOSUB_JW_TOKEN", TOKEN)
    monkeypatch.delenv("HOSUB_JW_URL", raising=False)
    FakeClient.calls = []
    FakeClient.payload = {"ok": True}
    FakeClient.status = 200
    FakeClient.raise_on_call = None


# --- 인증·주소 ---
def test_internal_token_header_is_sent():
    """jw-mcp 는 Bearer 가 아니라 X-Internal-Token 을 쓴다."""
    jw.summary(client_factory=FakeClient)
    _method, url, _json, headers = FakeClient.calls[0]
    assert headers == {"X-Internal-Token": TOKEN}
    assert url == "http://127.0.0.1:8604/api/dash/summary"


def test_url_is_overridable(monkeypatch):
    monkeypatch.setenv("HOSUB_JW_URL", "http://127.0.0.1:9999/")
    jw.users(client_factory=FakeClient)
    assert FakeClient.calls[0][1] == "http://127.0.0.1:9999/api/dash/users"


def test_unconfigured_without_token(monkeypatch):
    """토큰이 없으면 부르지도 않는다 — 화면은 '설정 필요' 안내를 띄운다."""
    monkeypatch.delenv("HOSUB_JW_TOKEN", raising=False)
    out = jw.summary(client_factory=FakeClient)
    assert out["status"] == "unconfigured"
    assert "HOSUB_JW_TOKEN" in out["reason"]
    assert FakeClient.calls == []


# --- 별개 프로세스라 꺼져 있을 수 있다 ---
def test_connection_failure_becomes_a_dict_not_an_exception():
    """jw-mcp 가 꺼져 있어도 대시보드가 깨지면 안 된다(스펙 8절)."""
    FakeClient.raise_on_call = OSError("Connection refused")
    out = jw.summary(client_factory=FakeClient)
    assert out["status"] == "error"
    assert "Connection refused" in out["error"]
    assert "systemctl status jw-mcp" in out["hint"]


def test_unauthorized_is_surfaced():
    FakeClient.status = 401
    FakeClient.payload = {"ok": False, "error": "unauthorized"}
    out = jw.users(client_factory=FakeClient)
    assert out["status"] == "error" and out["http_status"] == 401
    assert out["error"] == "unauthorized"


def test_ok_false_with_200_is_still_an_error():
    """성공 표식은 ok 다 — 상태코드만 보면 안 된다(스펙 8절)."""
    FakeClient.status = 200
    FakeClient.payload = {"ok": False, "error": "뭔가 잘못됨"}
    out = jw.users(client_factory=FakeClient)
    assert out["status"] == "error" and out["error"] == "뭔가 잘못됨"


# --- 조회 ---
def test_summary_passes_sections_through():
    """sections 를 해석하지 않고 그대로 넘긴다 — 지표가 바뀌어도 이쪽은 그대로다."""
    FakeClient.payload = {
        "ok": True, "service": "jw-mcp", "version": "2.0.0",
        "sections": [{"title": "서비스", "icon": "bi-hdd-network",
                      "items": [{"label": "상태", "value": "정상", "tone": "ok"}]}],
    }
    out = jw.summary(client_factory=FakeClient)
    assert out["status"] == "ok"
    assert out["sections"][0]["items"][0]["tone"] == "ok"


@pytest.mark.parametrize("given,expected", [
    (7, 7), (0, 7), (None, 7), (1, 1), (90, 90), (500, 90), (-3, 1),
])
def test_usage_days_is_clamped(given, expected):
    """서버도 자르지만 여기서 먼저 맞춘다(스펙 5절: 1–90)."""
    jw.usage(given, client_factory=FakeClient)
    assert FakeClient.calls[-1][1].endswith(f"/usage?days={expected}")


@pytest.mark.parametrize("given,expected", [
    (50, 50), (0, 50), (1, 1), (500, 500), (9999, 500),
])
def test_calls_limit_is_clamped(given, expected):
    jw.calls(given, client_factory=FakeClient)
    assert FakeClient.calls[-1][1].endswith(f"/calls?limit={expected}")


# --- 변경 작업: 입력을 먼저 막는다 ---
def test_set_status_sends_post_with_body():
    FakeClient.payload = {"ok": True, "user": {"user_id": USER_OK, "status": "active"}}
    out = jw.set_user_status(USER_OK, "active", client_factory=FakeClient)
    assert out["status"] == "ok"
    method, url, body, _headers = FakeClient.calls[0]
    assert method == "POST"
    assert url.endswith(f"/users/{USER_OK}/status")
    assert body == {"status": "active"}


@pytest.mark.parametrize("bad", [
    "", "u_short", "u_" + "F" * 24, "u_" + "0" * 25, "../../etc/passwd",
    "u_4196f60425d3fcb8eb59f26b/../admin",
])
def test_bad_user_id_never_reaches_the_network(bad):
    """대시보드가 임의 경로를 만들어 주지 않는다."""
    out = jw.set_user_status(bad, "active", client_factory=FakeClient)
    assert out["status"] == "error"
    assert FakeClient.calls == []


@pytest.mark.parametrize("bad", ["", "ACTIVE", "deleted", "banned", None])
def test_bad_status_never_reaches_the_network(bad):
    out = jw.set_user_status(USER_OK, bad, client_factory=FakeClient)
    assert out["status"] == "error"
    assert FakeClient.calls == []


def test_last_admin_rejection_is_surfaced():
    """마지막 활성 관리자는 서버가 거부한다 — 그 사유가 그대로 올라와야 한다."""
    FakeClient.status = 400
    FakeClient.payload = {"ok": False,
                          "error": "마지막 활성 관리자는 비활성화할 수 없습니다."}
    out = jw.set_user_status(USER_OK, "disabled", client_factory=FakeClient)
    assert out["status"] == "error"
    assert "마지막 활성 관리자" in out["error"]


def test_every_public_function_returns_a_dict_never_raises():
    """모듈 규약: 실패해도 예외 대신 status/error dict."""
    FakeClient.raise_on_call = RuntimeError("boom")
    for call in (
        lambda: jw.summary(client_factory=FakeClient),
        lambda: jw.users(client_factory=FakeClient),
        lambda: jw.usage(7, client_factory=FakeClient),
        lambda: jw.calls(10, client_factory=FakeClient),
        lambda: jw.set_user_status(USER_OK, "active", client_factory=FakeClient),
    ):
        out = call()
        assert isinstance(out, dict) and out["status"] == "error"
