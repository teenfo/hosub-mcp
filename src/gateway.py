"""공유 LLM 게이트웨이(llm-gateway 컨테이너) 클라이언트.

hosub 는 맥의 Ollama 를 **직접 부르지 않는다.** 모든 추론은 게이트웨이를 거친다.
그래야 다른 소비자(roxlogy·TNM·trading)와 같은 큐·레인·재시도·사용량 집계를
공유하고, 모델 교체가 `llm-gateway/config/roles.yaml` 한 줄로 끝난다.

실패해도 예외 대신 status/error dict 를 돌려준다 — 호출부(MCP 도구·대시보드)가
그대로 반환하기 때문이다. status 는 ok|pending|failed 중 하나이거나, 게이트웨이에
닿지 못했을 때의 error|unconfigured 다.
"""

from __future__ import annotations

import os
from urllib.parse import quote

import httpx

DEFAULT_URL = "http://127.0.0.1:8603"
TIMEOUT = 10
# /v1/status 는 맥에 /api/tags 를 물어보므로 맥이 꺼져 있으면 그만큼 걸린다
STATUS_TIMEOUT = 15
# 대화형 호출의 기본 대기. 넘기면 job_id 로 나중에 수령한다(게이트웨이 상한 300)
DEFAULT_WAIT = 120


def base_url() -> str:
    return os.environ.get("LLMGW_URL", DEFAULT_URL).rstrip("/")


def _token() -> str:
    return os.environ.get("LLMGW_TOKEN_HOSUB", "")


def _unconfigured() -> dict:
    return {
        "status": "unconfigured",
        "requests": [],
        "roles": [],
        "reason": "LLMGW_TOKEN_HOSUB 가 설정되지 않았습니다.",
        "hint": "llm-gateway/.env 의 LLMGW_TOKEN_HOSUB 값을 hosub-mcp 의 .env 에도 넣으세요.",
    }


def _call(method: str, path: str, *, json: dict | None = None,
          timeout: float | None = None, client_factory=httpx.Client) -> dict:
    token = _token()
    if not token:
        return _unconfigured()
    url = f"{base_url()}{path}"
    try:
        with client_factory(timeout=timeout or TIMEOUT) as client:
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


def list_roles(*, client_factory=httpx.Client) -> dict:
    """쓸 수 있는 역할·모델. 모델 교체는 게이트웨이 roles.yaml 에서 한다."""
    return _call("GET", "/v1/roles", client_factory=client_factory)


def status(*, client_factory=httpx.Client) -> dict:
    """백엔드(맥) 상태·보유 모델·레인 큐·메모리 예산·사용량·모델 설치 요청 수."""
    return _call("GET", "/v1/status", client_factory=client_factory,
                 timeout=STATUS_TIMEOUT)


def generate(role: str, prompt: str, *, system: str | None = None,
             wait: float = DEFAULT_WAIT, metadata: dict | None = None,
             client_factory=httpx.Client) -> dict:
    """생성 요청. wait 초까지 기다리고, 안 끝나면 status=pending + job_id 를 준다.

    system 을 주면 역할의 기본 프롬프트 대신 그것이 쓰인다(프롬프트는 호출자 소유).
    """
    body: dict = {"role": role, "prompt": prompt, "wait": wait}
    if system is not None:
        body["system"] = system
    if metadata:
        body["metadata"] = metadata
    # 서버가 wait 초까지 붙잡고 있으므로 HTTP 타임아웃은 그보다 넉넉해야 한다
    return _call("POST", "/v1/generate", json=body,
                 timeout=wait + 20, client_factory=client_factory)


def get_job(job_id: str, *, client_factory=httpx.Client) -> dict:
    """잡 조회. pending 이던 생성 요청의 결과를 나중에 수령한다."""
    return _call("GET", f"/v1/jobs/{quote(job_id, safe='')}",
                 client_factory=client_factory)


def list_model_requests(status: str | None = None, *, client_factory=httpx.Client) -> dict:
    """모델 설치 요청 목록. pending 이 있으면 사용자의 승인이 필요하다."""
    path = "/v1/models/requests" + (f"?status={quote(status, safe='')}" if status else "")
    return _call("GET", path, client_factory=client_factory)


def decide_model_request(model: str, action: str, *, client_factory=httpx.Client) -> dict:
    """모델 설치 요청을 승인(approve)하거나 거부(reject)한다."""
    if action not in ("approve", "reject"):
        return {"status": "rejected", "reason": "action 은 approve 또는 reject 여야 합니다."}
    return _call("POST", "/v1/models/requests",
                 json={"model": model, "action": action},
                 client_factory=client_factory)


# --- 관리 API ---
#
# /v1/admin/* 은 Caddy 가 공개 경로(/llm/*)에서 404 로 잘라낸다. 즉 여기서
# 부르는 127.0.0.1:8603 으로만 닿는다 — 대시보드는 서버 안에서 돌므로 문제없다.
# 게이트웨이가 다시 admin 토큰(hosub)인지 확인하므로 통제는 두 겹이다.

def list_admin_roles(*, client_factory=httpx.Client) -> dict:
    """역할별 유효값 + roles.yaml 기본값 대비 차이 + 붙여 넣을 스니펫."""
    return _call("GET", "/v1/admin/roles", client_factory=client_factory)


def set_role_override(role: str, fields: dict, *, note: str | None = None,
                      client_factory=httpx.Client) -> dict:
    """역할의 모델·레인·타임아웃·옵션을 런타임에 바꾼다(roles.yaml 은 그대로)."""
    body: dict = {"role": role, "fields": fields}
    if note:
        body["note"] = note
    return _call("POST", "/v1/admin/roles", json=body, client_factory=client_factory)


def revert_role(role: str, *, client_factory=httpx.Client) -> dict:
    """오버라이드를 지워 roles.yaml 기본값으로 되돌린다."""
    return _call("DELETE", f"/v1/admin/roles?role={quote(role, safe='')}",
                 client_factory=client_factory)


def list_installed_models(*, client_factory=httpx.Client) -> dict:
    """맥에 설치된 모델 + 용량 + 쓰는 역할 + 최근 사용 + 삭제 차단 사유."""
    return _call("GET", "/v1/admin/models", client_factory=client_factory,
                 timeout=STATUS_TIMEOUT)


def delete_model(model: str, *, client_factory=httpx.Client) -> dict:
    """맥에서 모델을 지운다. 쓰는 역할·대기 잡이 있으면 409 로 거부된다."""
    # 모델 이름에는 ':'·'/' 가 들어간다 — 쿼리스트링에 그대로 넣지 않는다
    return _call("DELETE", f"/v1/admin/models?model={quote(model, safe='')}",
                 client_factory=client_factory, timeout=60)


def install_model(model: str, *, client_factory=httpx.Client) -> dict:
    """모델 설치를 지시한다(기존 승인 파이프라인에 approved 로 들어간다)."""
    return _call("POST", "/v1/admin/models/install", json={"model": model},
                 client_factory=client_factory)


def search_catalog(query: str = "", kind: str | None = None, *,
                   client_factory=httpx.Client) -> dict:
    """내장 카탈로그 검색. 목록에 없으면 이름을 직접 입력해 설치한다."""
    # 검색어는 사용자 자유 입력이다. 인코딩하지 않으면 'a&b' 가 잘린다.
    path = f"/v1/admin/catalog?q={quote(query or '', safe='')}"
    if kind:
        path += f"&kind={quote(kind, safe='')}"
    return _call("GET", path, client_factory=client_factory)


def compare_models(prompt: str, models: list, *, system: str | None = None,
                   options: dict | None = None, client_factory=httpx.Client) -> dict:
    """같은 프롬프트를 두 모델에 돌려 tok/s 를 비교한다(잡 큐 경유·순차)."""
    body: dict = {"prompt": prompt, "models": models}
    if system:
        body["system"] = system
    if options:
        body["options"] = options
    return _call("POST", "/v1/admin/compare", json=body,
                 client_factory=client_factory)


def integration_doc(*, client_factory=httpx.Client) -> dict:
    """소비자용 통합 가이드(마크다운) 원문.

    _call 을 쓰지 않는다 — 이 엔드포인트만 JSON 이 아니라 text/markdown 을
    돌려주므로 res.json() 이 터진다. 대시보드가 렌더할 수 있게 dict 로 감싼다.
    """
    token = _token()
    if not token:
        return _unconfigured()
    url = f"{base_url()}/v1/integration"
    try:
        with client_factory(timeout=TIMEOUT) as client:
            res = client.request("GET", url,
                                 headers={"Authorization": f"Bearer {token}"})
            if res.status_code >= 400:
                return {"status": "error", "http_status": res.status_code,
                        "error": res.text[:200]}
            text = res.text
    except Exception as exc:
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "hint": f"게이트웨이({base_url()})가 떠 있는지 확인하세요.",
        }
    return {"status": "ok", "markdown": text, "bytes": len(text.encode("utf-8"))}


def get_comparison(run_id: str, *, client_factory=httpx.Client) -> dict:
    return _call("GET", f"/v1/admin/compare/{quote(run_id, safe='')}",
                 client_factory=client_factory)
