"""공유 LLM 게이트웨이(llm-gateway 컨테이너) 클라이언트 — 모델 설치 요청 전용.

게이트웨이는 외부 서비스가 아직 맥에 없는 모델을 쓰면 설치 요청을 만들어 둔다.
승인 주체는 사람(나)이고, 승인 경로는 이 대시보드와 MCP 도구다.

추론 호출 자체를 게이트웨이로 옮기는 작업은 별도 PR 이다 — 여기서는 승인에
필요한 두 호출만 얇게 감싼다. 실패해도 예외 대신 status/error dict 를 돌려준다.
"""

from __future__ import annotations

import os

import httpx

DEFAULT_URL = "http://127.0.0.1:8603"
TIMEOUT = 10


def base_url() -> str:
    return os.environ.get("LLMGW_URL", DEFAULT_URL).rstrip("/")


def _token() -> str:
    return os.environ.get("LLMGW_TOKEN_HOSUB", "")


def _unconfigured() -> dict:
    return {
        "status": "unconfigured",
        "requests": [],
        "reason": "LLMGW_TOKEN_HOSUB 가 설정되지 않았습니다.",
        "hint": "llm-gateway/.env 의 LLMGW_TOKEN_HOSUB 값을 hosub-mcp 의 .env 에도 넣으세요.",
    }


def _call(method: str, path: str, *, json: dict | None = None,
          client_factory=httpx.Client) -> dict:
    token = _token()
    if not token:
        return _unconfigured()
    url = f"{base_url()}{path}"
    try:
        with client_factory(timeout=TIMEOUT) as client:
            res = client.request(
                method, url, json=json,
                headers={"Authorization": f"Bearer {token}"},
            )
            body = res.json() if res.content else {}
            if res.status_code >= 400:
                return {
                    "status": "error",
                    "http_status": res.status_code,
                    "error": body.get("detail") or body.get("error") or res.text[:200],
                }
    except Exception as exc:
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "hint": f"게이트웨이({base_url()})가 떠 있는지 확인하세요 "
                    "(docker compose ps llm-gateway).",
        }
    return {"status": "ok", **body}


def list_model_requests(status: str | None = None, *, client_factory=httpx.Client) -> dict:
    """모델 설치 요청 목록. pending 이 있으면 사용자의 승인이 필요하다."""
    path = "/v1/models/requests" + (f"?status={status}" if status else "")
    return _call("GET", path, client_factory=client_factory)


def decide_model_request(model: str, action: str, *, client_factory=httpx.Client) -> dict:
    """모델 설치 요청을 승인(approve)하거나 거부(reject)한다."""
    if action not in ("approve", "reject"):
        return {"status": "rejected", "reason": "action 은 approve 또는 reject 여야 합니다."}
    return _call("POST", "/v1/models/requests",
                 json={"model": model, "action": action},
                 client_factory=client_factory)
