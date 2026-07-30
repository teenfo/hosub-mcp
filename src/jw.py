"""jw-mcp(JW.org 콘텐츠 MCP, 127.0.0.1:8604) 대시보드 연동 클라이언트.

jw-mcp 는 hosub 와 **별개 프로세스·별개 저장소**다. 자체 OAuth 2.1 로 사용자를
직접 인증하고, 운영 지표를 `/api/dash/*` 조회 API 로 게시한다. 이 모듈은 그
API 를 대시보드가 쓸 수 있게 감싼다.

**루프백 전용 API 다.** Caddy 는 `/jw/*` 만 공개하고 `/api/dash/*` 는 라우팅하지
않는다(공인 인터넷에서 404). 따라서 브라우저가 직접 부를 수 없고, 반드시 이
서버 안에서 서버사이드로 호출해야 한다 — 그게 이 모듈이 있는 이유다.

`src/gateway.py` 와 같은 규약을 따른다: **실패해도 예외 대신 status/error dict 를
돌려준다.** 호출부(대시보드 라우트)가 그대로 반환하기 때문이다. status 는
ok|error|unconfigured 다. jw-mcp 는 별개 프로세스라 따로 죽거나 배포 중 잠깐
내려간다 — 그때 대시보드가 깨지면 안 된다(스펙 8절).
"""

from __future__ import annotations

import os
import re

import httpx

DEFAULT_URL = "http://127.0.0.1:8604"
TIMEOUT = 10

# 스펙 2절: u_ + 24자리 소문자 hex.
# 서버도 검증하지만 여기서 먼저 막는다 — 대시보드가 임의 경로를 만들어 주지 않는다.
USER_ID_RE = re.compile(r"^u_[0-9a-f]{24}$")

# 스펙 7절의 전부. 이 셋 말고는 서버가 400 을 준다.
STATUSES = ("pending", "active", "disabled")


def base_url() -> str:
    return os.environ.get("HOSUB_JW_URL", DEFAULT_URL).rstrip("/")


def _token() -> str:
    return os.environ.get("HOSUB_JW_TOKEN", "")


def _unconfigured() -> dict:
    return {
        "status": "unconfigured",
        "users": [],
        "calls": [],
        "sections": [],
        "reason": "HOSUB_JW_TOKEN 이 설정되지 않았습니다.",
        "hint": "jw-mcp 의 .env 에 있는 JW_INTERNAL_TOKEN 값을 "
                "hosub-mcp 의 .env 에 HOSUB_JW_TOKEN 으로 넣으세요.",
    }


def _call(method: str, path: str, *, json: dict | None = None,
          client_factory=httpx.Client) -> dict:
    token = _token()
    if not token:
        return _unconfigured()
    try:
        with client_factory(timeout=TIMEOUT) as client:
            res = client.request(
                method, f"{base_url()}/api/dash{path}", json=json,
                headers={"X-Internal-Token": token},
            )
            body = res.json() if res.content else {}
            if res.status_code >= 400 or not body.get("ok"):
                return {
                    "status": "error",
                    "http_status": res.status_code,
                    # 스펙 8절: 오류 본문은 {"ok": false, "error": "..."}
                    "error": body.get("error") or res.text[:200],
                }
    except Exception as exc:
        # 별개 프로세스라 꺼져 있을 수 있다. 연결 실패는 안내 문구로 바꾼다.
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "hint": f"jw-mcp({base_url()})가 떠 있는지 확인하세요 "
                    "(systemctl status jw-mcp).",
        }
    return {"status": "ok", **body}


def summary(*, client_factory=httpx.Client) -> dict:
    """화면 하나를 채우는 모든 값.

    `sections` 를 그대로 그리면 jw-mcp 가 지표를 추가·삭제·재정렬해도 이쪽
    코드는 그대로다(스펙 3.1). 특정 섹션 제목의 존재를 가정하지 않는다.
    """
    return _call("GET", "/summary", client_factory=client_factory)


def users(*, client_factory=httpx.Client) -> dict:
    """사용자 목록. pending 이 먼저 온다(승인 대기가 위로)."""
    return _call("GET", "/users", client_factory=client_factory)


def usage(days: int = 7, *, client_factory=httpx.Client) -> dict:
    """일자별·도구별 사용량. daily 는 호출이 있었던 날만 나온다."""
    days = max(1, min(int(days or 7), 90))
    return _call("GET", f"/usage?days={days}", client_factory=client_factory)


def calls(limit: int = 50, *, client_factory=httpx.Client) -> dict:
    """최근 도구 호출 로그. 도구 인자는 기록되지 않는다(스펙 6절)."""
    limit = max(1, min(int(limit or 50), 500))
    return _call("GET", f"/calls?limit={limit}", client_factory=client_factory)


def set_user_status(user_id: str, status: str, *,
                    client_factory=httpx.Client) -> dict:
    """사용자 승인·비활성화. 이 API 의 **유일한 변경 작업**이다.

    ⚠️ active 가 아닌 상태로 바꾸면 그 사용자의 OAuth 토큰이 **즉시 전부
    폐기된다.** 연결돼 있던 Claude 커넥터는 다음 요청부터 401 을 받고 리프레시로도
    되살아나지 않는다 — 되돌리려면 active 로 바꾼 뒤 사용자가 커넥터를 다시
    연결해야 한다(스펙 7절 부수 효과).

    마지막 활성 관리자를 내리는 것은 서버가 거부한다(자기 잠금 방지).
    """
    if not USER_ID_RE.match(user_id or ""):
        return {"status": "error", "error": "user_id 형식이 올바르지 않습니다."}
    if status not in STATUSES:
        return {"status": "error",
                "error": f"status 는 {' | '.join(STATUSES)} 입니다."}
    return _call("POST", f"/users/{user_id}/status", json={"status": status},
                 client_factory=client_factory)
