"""Ollama 백엔드 클라이언트 (맥 스튜디오).

실패는 예외로 던지되, 호출부(스케줄러)가 재시도 가능 여부를 판단할 수 있도록
BackendError 에 retryable 플래그를 담는다.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx


class BackendError(Exception):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class GenerateResult:
    response: str
    eval_count: int | None
    duration_ms: int | None


class OllamaClient:
    def __init__(self, base_url: str, keep_alive: str = "10m") -> None:
        self.base_url = base_url.rstrip("/")
        self.keep_alive = keep_alive

    async def generate(
        self,
        *,
        model: str,
        prompt: str,
        system: str | None = None,
        options: dict | None = None,
        timeout: int = 180,
        client: httpx.AsyncClient | None = None,
    ) -> GenerateResult:
        payload: dict = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "keep_alive": self.keep_alive,
        }
        if system:
            payload["system"] = system
        if options:
            payload["options"] = options

        owns = client is None
        client = client or httpx.AsyncClient(timeout=timeout)
        try:
            res = await client.post(
                f"{self.base_url}/api/generate", json=payload, timeout=timeout
            )
            if res.status_code >= 500:
                raise BackendError(f"백엔드 오류 HTTP {res.status_code}", retryable=True)
            if res.status_code >= 400:
                # 모델 없음 등 — 재시도해도 같다
                raise BackendError(
                    f"요청 거부 HTTP {res.status_code}: {res.text[:200]}", retryable=False
                )
            data = res.json()
        except httpx.TimeoutException as exc:
            raise BackendError(f"타임아웃: {exc}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise BackendError(f"연결 실패: {type(exc).__name__}: {exc}", retryable=True) from exc
        finally:
            if owns:
                await client.aclose()

        total = data.get("total_duration")
        return GenerateResult(
            response=data.get("response") or "",
            eval_count=data.get("eval_count"),
            duration_ms=round(total / 1_000_000) if total else None,
        )

    async def tags(self, timeout: int = 5) -> list[str]:
        """보유 모델 목록. 실패 시 BackendError."""
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                res = await client.get(f"{self.base_url}/api/tags")
                res.raise_for_status()
                data = res.json()
        except httpx.HTTPError as exc:
            raise BackendError(f"{type(exc).__name__}: {exc}") from exc
        return sorted(
            m["name"] for m in (data.get("models") or []) if m.get("name")
        )
