"""LLM 접근 계층 — 분류·임베딩 모두 공유 LLM 게이트웨이(:8603) 경유.

- 분류(chat): role `classify_news` (잡 큐, pending 시 폴링)
- 임베딩(embed): role `embed` (kind: embed — 큐를 타지 않는 동기·배치 API)
인증·레인·사용량 귀속·모델 교체(roles.yaml)는 게이트웨이 소유.

핵심 계약(계획 3절)은 유지된다: 게이트웨이·백엔드 불가는 OllamaUnavailable 로
던져 파이프라인이 행을 보류(백오프 후 자동 재처리)하게 하고, 응답 내용 불량
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
    """게이트웨이 role 로 분류 생성. 반환: (content, model_name, latency_ms)."""
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


async def embed_batch(texts: list[str]) -> list[list[float]]:
    """bge-m3 임베딩 배치 — 게이트웨이 /v1/embed (동기, 큐 미경유).

    한 번의 호출로 배치 전체를 처리한다. 실패 → OllamaUnavailable (전체 보류).
    """
    if not texts:
        return []
    role = settings.LLM.get("embed_role", "embed")
    try:
        gw = _gateway()
        vecs = await gw.embed(texts, role=role)
    except OllamaUnavailable:
        raise
    except GatewayError as e:
        raise OllamaUnavailable(f"게이트웨이 임베딩 불가: {e}") from e
    if not isinstance(vecs, list) or len(vecs) != len(texts):
        raise OllamaUnavailable("게이트웨이 임베딩 응답 형식 오류")
    return vecs


async def embed(text: str) -> list[float]:
    """단건 임베딩 (배치 경로의 편의 래퍼)."""
    return (await embed_batch([text]))[0]


async def reachable() -> str | None:
    """상태 표시용 — 게이트웨이 healthz 도달 시 그 URL."""
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(f"{settings.LLMGW_URL.rstrip('/')}/healthz")
            if r.status_code == 200:
                return settings.LLMGW_URL
    except httpx.HTTPError:
        pass
    return None
