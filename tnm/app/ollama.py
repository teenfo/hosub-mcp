"""LLM 접근 계층 — 분류는 공유 LLM 게이트웨이(:8603), 임베딩은 Ollama 직접.

- 분류(chat): llm-gateway 의 role `classify_news` 경유. 인증·레인 큐·사용량
  귀속·모델 교체(roles.yaml 한 줄)는 게이트웨이가 담당한다. system 프롬프트는
  호출자(TNM) 소유 — 요청마다 함께 보낸다.
- 임베딩(embed): 게이트웨이가 임베딩 API 를 아직 제공하지 않아 Mac Ollama
  (OLLAMA_URL)를 직접 호출한다. 게이트웨이에 embed 가 생기면 여기만 바꾸면 된다.

핵심 계약(계획 3절)은 유지된다: 연결·큐 실패는 OllamaUnavailable 로 던져
파이프라인이 행을 보류(백오프 후 자동 재처리)하게 하고, 응답 내용 불량
(SchemaError)만 재시도 후 llm_failed 로 적재한다.
"""
from __future__ import annotations

import logging
import time

import httpx

from . import settings
from .llmgw import AsyncLLMGateway, GatewayError

log = logging.getLogger("tnm.ollama")


class OllamaUnavailable(Exception):
    """LLM 백엔드(게이트웨이/Ollama) 사용 불가 — 항목을 버리지 말고 보류하라는 신호."""


class SchemaError(Exception):
    """LLM 응답이 규격(JSON 스키마)을 벗어남 — 재시도 대상."""


_gw: AsyncLLMGateway | None = None


def _gateway() -> AsyncLLMGateway:
    global _gw
    if _gw is None:
        if not settings.LLMGW_TOKEN:
            raise OllamaUnavailable("LLMGW_TOKEN 미설정")
        _gw = AsyncLLMGateway(token=settings.LLMGW_TOKEN,
                              base_url=settings.LLMGW_URL, timeout=30)
    return _gw


async def chat(system: str, user: str) -> tuple[str, str, int]:
    """게이트웨이 role 로 분류 생성. 반환: (content, model_name, latency_ms).

    게이트웨이 연결 실패·잡 실패·시간 초과 → OllamaUnavailable (보류 후 재시도).
    잡이 pending 으로 돌아오면 완료까지 폴링한다.
    """
    role = settings.LLM.get("classify_role", "classify_news")
    wait = min(int(settings.LLM.get("timeout_sec", 120)), 290)
    t0 = time.monotonic()
    try:
        gw = _gateway()
        job = await gw.generate(role, user, system=system, wait=wait,
                                metadata={"src": "tnm-classify"})
        if job.pending:
            job = await gw.wait_for(job.job_id, timeout=wait, poll=2.0)
    except OllamaUnavailable:
        raise
    except GatewayError as e:
        # 연결 실패·잡 실패·타임아웃·권한 문제 전부 '지금은 못 쓴다' — 보류
        raise OllamaUnavailable(f"게이트웨이 사용 불가: {e}") from e
    latency = int((time.monotonic() - t0) * 1000)
    if not job.ok or not (job.response or "").strip():
        raise OllamaUnavailable(f"게이트웨이 잡 실패: {job.error or job.status}")
    return job.response, job.model or role, latency


async def gateway_reachable() -> bool:
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(f"{settings.LLMGW_URL.rstrip('/')}/healthz")
            return r.status_code == 200
    except httpx.HTTPError:
        return False


# ---------------- 임베딩 (Ollama 직접 — 게이트웨이 미지원 구간) ----------------

def bases() -> list[str]:
    return [u.rstrip("/") for u in (settings.OLLAMA_URL, settings.OLLAMA_FALLBACK_URL) if u]


_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=int(settings.LLM.get("timeout_sec", 120)))
    return _client


async def reachable() -> str | None:
    """상태 표시용 — 게이트웨이 우선, 아니면 직접 Ollama 도달 주소."""
    if await gateway_reachable():
        return settings.LLMGW_URL
    for base in bases():
        try:
            r = await _http().get(f"{base}/api/version", timeout=3)
            if r.status_code == 200:
                return base
        except httpx.HTTPError:
            continue
    return None


async def embed(text: str) -> list[float]:
    """bge-m3 임베딩(1024차원). 모든 base 실패 → OllamaUnavailable."""
    model = settings.LLM.get("embed_model", "bge-m3")
    last_err: Exception | None = None
    for base in bases():
        try:
            r = await _http().post(f"{base}/api/embeddings",
                                   json={"model": model, "prompt": text}, timeout=60)
            r.raise_for_status()
            vec = r.json().get("embedding")
            if not isinstance(vec, list) or not vec:
                raise OllamaUnavailable(f"{base}: 임베딩 응답 형식 오류")
            return vec
        except (httpx.HTTPError, ValueError) as e:
            # 연결 거부·타임아웃·모델 미설치(404)·5xx 전부 '이 base 불가'로 다음 시도
            last_err = e
            log.warning("임베딩 실패 %s: %s", base, e)
            continue
    raise OllamaUnavailable(f"Ollama 연결 불가: {last_err or 'OLLAMA_URL 미설정'}")
