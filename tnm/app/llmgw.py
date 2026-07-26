"""llmgw — 공유 LLM 게이트웨이 클라이언트 (단일 파일, 복사해서 쓴다).

소비 프로젝트(roxlogy·BCL·TNM·trading)는 이 파일 하나만 자기 레포에 복사하면 된다.
의존성은 httpx 하나. 컨테이너·워커 추가 없음.

    from llmgw import LLMGateway

    gw = LLMGateway()                       # LLMGW_URL / LLMGW_TOKEN 환경변수 사용
    text = gw.run("summarize", 긴_문서)      # 끝까지 기다려 응답 텍스트만

    job = gw.generate("analyze_workout", 데이터, system=내_프롬프트, wait=0)
    ...                                     # 다른 일을 하다가
    done = gw.wait_for(job.job_id)          # 나중에 결과 수령

비동기 프레임워크(FastAPI 등)에서는 AsyncLLMGateway 를 쓴다. API 는 동일하다.

계약과 에러 코드는 llm-gateway/docs/integration.md 참고.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field

import httpx

__all__ = [
    "LLMGateway", "AsyncLLMGateway", "Job",
    "GatewayError", "JobFailed", "JobTimeout", "AuthError", "RoleError",
]

DEFAULT_URL = "http://127.0.0.1:8603"
DEFAULT_POLL = 2.0          # 초. 기본 레이트리밋(분당 60)의 절반이라 안전하다
DEFAULT_WAIT_TIMEOUT = 900  # 초. 배치 잡이 큐에서 대기하는 시간까지 감안


# --------------------------------------------------------------------------
# 예외 — 호출자가 구분해서 처리할 수 있게 나눈다
# --------------------------------------------------------------------------
class GatewayError(Exception):
    """게이트웨이 호출 실패 전반."""


class AuthError(GatewayError):
    """토큰이 없거나(401) 그 역할을 쓸 권한이 없다(403). 설정 문제 — 재시도 무의미."""


class RoleError(GatewayError):
    """모르는 역할(404)이거나 프롬프트가 너무 길다(413). 코드 문제 — 재시도 무의미."""


class JobFailed(GatewayError):
    """잡이 failed/cancelled 로 끝났다. .job 에 상세."""

    def __init__(self, job: "Job") -> None:
        super().__init__(job.error or "잡 실패")
        self.job = job


class JobTimeout(GatewayError):
    """정해진 시간 안에 끝나지 않았다. 잡 자체는 계속 돌고 있을 수 있다."""

    def __init__(self, job: "Job") -> None:
        super().__init__(f"잡 {job.job_id} 가 제한 시간 안에 끝나지 않음")
        self.job = job


# --------------------------------------------------------------------------
# 응답 — 동기/비동기 어느 쪽이든 이 모양 하나다
# --------------------------------------------------------------------------
@dataclass
class Job:
    job_id: str
    status: str                     # ok | pending | failed | cancelled
    response: str | None = None
    error: str | None = None
    role: str | None = None
    model: str | None = None
    lane: str | None = None
    attempts: int = 0
    queue_position: int | None = None
    metadata: dict = field(default_factory=dict)
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def pending(self) -> bool:
        return self.status == "pending"

    @property
    def done(self) -> bool:
        return self.status != "pending"

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


def _raise_for_status(res: httpx.Response) -> dict:
    """게이트웨이의 오류 응답을 의미 있는 예외로 옮긴다."""
    try:
        body = res.json() if res.content else {}
    except ValueError:
        body = {}
    if res.status_code < 400:
        return body

    detail = body.get("detail") or body.get("error") or res.text[:200]
    if res.status_code in (401, 403):
        raise AuthError(f"HTTP {res.status_code}: {detail}")
    if res.status_code in (404, 413):
        raise RoleError(f"HTTP {res.status_code}: {detail}")
    if res.status_code == 429:
        raise GatewayError(f"레이트리밋: {detail}. 폴링 간격을 늘리세요.")
    raise GatewayError(f"HTTP {res.status_code}: {detail}")


def _settings(base_url: str | None, token: str | None) -> tuple[str, str]:
    url = (base_url or os.environ.get("LLMGW_URL") or DEFAULT_URL).rstrip("/")
    tok = token or os.environ.get("LLMGW_TOKEN") or ""
    if not tok:
        raise AuthError(
            "토큰이 없습니다. LLMGW_TOKEN 환경변수를 설정하거나 token= 으로 넘기세요."
        )
    return url, tok


# --------------------------------------------------------------------------
# 동기 클라이언트
# --------------------------------------------------------------------------
class LLMGateway:
    def __init__(self, token: str | None = None, base_url: str | None = None,
                 *, timeout: float = 60.0) -> None:
        self.base_url, self._token = _settings(base_url, token)
        self.timeout = timeout

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    def _call(self, method: str, path: str, *, json: dict | None = None,
              timeout: float | None = None) -> dict:
        try:
            with httpx.Client(timeout=timeout or self.timeout) as c:
                res = c.request(method, f"{self.base_url}{path}",
                                json=json, headers=self._headers)
        except httpx.HTTPError as exc:
            raise GatewayError(
                f"게이트웨이({self.base_url}) 연결 실패: {type(exc).__name__}: {exc}"
            ) from exc
        return _raise_for_status(res)

    # --- 생성 ---
    def generate(self, role: str, prompt: str, *, system: str | None = None,
                 wait: float = 30, priority: int = 0,
                 metadata: dict | None = None) -> Job:
        """잡을 만든다. wait 초까지 기다리고, 안 끝나면 pending 으로 돌아온다.

        wait=0 이면 즉시 job_id 만 받는다(긴 배치용). system 을 주면 역할의 기본
        프롬프트 대신 그것이 쓰인다 — 프롬프트는 호출자 소유다.
        """
        body: dict = {"role": role, "prompt": prompt, "wait": wait,
                      "priority": priority}
        if system is not None:
            body["system"] = system
        if metadata:
            body["metadata"] = metadata
        # 서버가 wait 초까지 붙잡고 있으므로 HTTP 타임아웃은 그보다 넉넉해야 한다
        return Job.from_dict(
            self._call("POST", "/v1/generate", json=body,
                       timeout=max(self.timeout, wait + 15))
        )

    def job(self, job_id: str) -> Job:
        return Job.from_dict(self._call("GET", f"/v1/jobs/{job_id}"))

    def jobs(self, *, status: str | None = None, limit: int = 20) -> list[Job]:
        q = f"?limit={limit}" + (f"&status={status}" if status else "")
        return [Job.from_dict(j) for j in self._call("GET", f"/v1/jobs{q}")["jobs"]]

    def cancel(self, job_id: str) -> bool:
        """대기 중인 잡만 취소된다. 이미 실행/완료면 False."""
        try:
            self._call("DELETE", f"/v1/jobs/{job_id}")
        except GatewayError:
            return False
        return True

    def wait_for(self, job_id: str, *, timeout: float = DEFAULT_WAIT_TIMEOUT,
                 poll: float = DEFAULT_POLL) -> Job:
        """완료까지 폴링한다. 실패면 JobFailed, 시간 초과면 JobTimeout."""
        deadline = time.monotonic() + timeout
        job = self.job(job_id)
        while job.pending:
            if time.monotonic() >= deadline:
                raise JobTimeout(job)
            time.sleep(poll)
            job = self.job(job_id)
        if not job.ok:
            raise JobFailed(job)
        return job

    def run(self, role: str, prompt: str, *, system: str | None = None,
            timeout: float = DEFAULT_WAIT_TIMEOUT, **kw) -> str:
        """끝까지 기다려 응답 텍스트만 돌려주는 편의 메서드."""
        job = self.generate(role, prompt, system=system, wait=min(30, timeout), **kw)
        if job.pending:
            job = self.wait_for(job.job_id, timeout=timeout)
        elif not job.ok:
            raise JobFailed(job)
        return job.response or ""

    # --- 조회 ---
    def roles(self) -> list[dict]:
        """이 토큰으로 쓸 수 있는 역할 목록. 모델·레인·타임아웃 포함."""
        return self._call("GET", "/v1/roles")["roles"]

    def status(self) -> dict:
        """백엔드 상태·레인 큐·사용량·모델 설치 요청 수."""
        return self._call("GET", "/v1/status")

    def model_requests(self, status: str | None = None) -> list[dict]:
        """모델 설치 요청 목록(조회 전용 — 승인은 hosub 대시보드/MCP 에서)."""
        q = f"?status={status}" if status else ""
        return self._call("GET", f"/v1/models/requests{q}")["requests"]


# --------------------------------------------------------------------------
# 비동기 클라이언트 — API 는 위와 동일
# --------------------------------------------------------------------------
class AsyncLLMGateway:
    def __init__(self, token: str | None = None, base_url: str | None = None,
                 *, timeout: float = 60.0) -> None:
        self.base_url, self._token = _settings(base_url, token)
        self.timeout = timeout

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    async def _call(self, method: str, path: str, *, json: dict | None = None,
                    timeout: float | None = None) -> dict:
        try:
            async with httpx.AsyncClient(timeout=timeout or self.timeout) as c:
                res = await c.request(method, f"{self.base_url}{path}",
                                      json=json, headers=self._headers)
        except httpx.HTTPError as exc:
            raise GatewayError(
                f"게이트웨이({self.base_url}) 연결 실패: {type(exc).__name__}: {exc}"
            ) from exc
        return _raise_for_status(res)

    async def generate(self, role: str, prompt: str, *, system: str | None = None,
                       wait: float = 30, priority: int = 0,
                       metadata: dict | None = None) -> Job:
        body: dict = {"role": role, "prompt": prompt, "wait": wait,
                      "priority": priority}
        if system is not None:
            body["system"] = system
        if metadata:
            body["metadata"] = metadata
        return Job.from_dict(
            await self._call("POST", "/v1/generate", json=body,
                             timeout=max(self.timeout, wait + 15))
        )

    async def job(self, job_id: str) -> Job:
        return Job.from_dict(await self._call("GET", f"/v1/jobs/{job_id}"))

    async def jobs(self, *, status: str | None = None, limit: int = 20) -> list[Job]:
        q = f"?limit={limit}" + (f"&status={status}" if status else "")
        return [Job.from_dict(j)
                for j in (await self._call("GET", f"/v1/jobs{q}"))["jobs"]]

    async def cancel(self, job_id: str) -> bool:
        try:
            await self._call("DELETE", f"/v1/jobs/{job_id}")
        except GatewayError:
            return False
        return True

    async def wait_for(self, job_id: str, *, timeout: float = DEFAULT_WAIT_TIMEOUT,
                       poll: float = DEFAULT_POLL) -> Job:
        deadline = time.monotonic() + timeout
        job = await self.job(job_id)
        while job.pending:
            if time.monotonic() >= deadline:
                raise JobTimeout(job)
            await asyncio.sleep(poll)
            job = await self.job(job_id)
        if not job.ok:
            raise JobFailed(job)
        return job

    async def run(self, role: str, prompt: str, *, system: str | None = None,
                  timeout: float = DEFAULT_WAIT_TIMEOUT, **kw) -> str:
        job = await self.generate(role, prompt, system=system,
                                  wait=min(30, timeout), **kw)
        if job.pending:
            job = await self.wait_for(job.job_id, timeout=timeout)
        elif not job.ok:
            raise JobFailed(job)
        return job.response or ""

    async def roles(self) -> list[dict]:
        return (await self._call("GET", "/v1/roles"))["roles"]

    async def status(self) -> dict:
        return await self._call("GET", "/v1/status")

    async def model_requests(self, status: str | None = None) -> list[dict]:
        q = f"?status={status}" if status else ""
        return (await self._call("GET", f"/v1/models/requests{q}"))["requests"]
