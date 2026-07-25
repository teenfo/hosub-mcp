"""Ollama 클라이언트 — Mac Studio(primary) → hosub 로컬(fallback).

핵심 계약(계획 3절): 연결 실패는 OllamaUnavailable 로 던져 파이프라인이
행을 보류(백오프 후 자동 재처리)하게 하고, 응답 형식 불량(SchemaError,
M5 분류에서 사용)만 재시도 후 llm_failed 로 적재한다.
"""
from __future__ import annotations

import logging

import httpx

from . import settings

log = logging.getLogger("tnm.ollama")


class OllamaUnavailable(Exception):
    """모든 엔드포인트 연결 불가 — 항목을 버리지 말고 보류하라는 신호."""


class SchemaError(Exception):
    """LLM 응답이 규격(JSON 스키마)을 벗어남 — 재시도 대상 (M5)."""


def bases() -> list[str]:
    return [u.rstrip("/") for u in (settings.OLLAMA_URL, settings.OLLAMA_FALLBACK_URL) if u]


_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=int(settings.LLM.get("timeout_sec", 120)))
    return _client


async def reachable() -> str | None:
    """상태 표시용 — 접속 가능한 첫 base URL (없으면 None)."""
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
