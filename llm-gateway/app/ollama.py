"""Ollama 백엔드 클라이언트 (맥 스튜디오).

실패는 예외로 던지되, 호출부(스케줄러)가 재시도 가능 여부를 판단할 수 있도록
BackendError 에 retryable 플래그를 담는다.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx


class InputTooLong(Exception):
    """모델 컨텍스트를 넘는 입력. 호출자가 잘라서 다시 보내야 한다(재시도 무의미)."""


class BackendError(Exception):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class GenerateResult:
    response: str
    eval_count: int | None
    duration_ms: int | None


@dataclass
class EmbedResult:
    embeddings: list[list[float]]
    prompt_eval_count: int | None
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

    async def pull(
        self,
        model: str,
        *,
        progress_cb=None,
        timeout: int = 3600,
    ) -> None:
        """맥의 Ollama 에 모델 설치를 지시한다 (SSH 불필요).

        /api/pull 은 진행 상황을 JSON 라인으로 스트리밍한다. progress_cb(percent)
        로 0~100 을 콜백한다. 실패 시 BackendError.
        """
        import json as _json

        url = f"{self.base_url}/api/pull"
        payload = {"model": model, "stream": True}
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", url, json=payload) as res:
                    if res.status_code >= 400:
                        body = (await res.aread()).decode("utf-8", "replace")[:200]
                        raise BackendError(
                            f"pull 거부 HTTP {res.status_code}: {body}",
                            retryable=res.status_code >= 500,
                        )
                    async for line in res.aiter_lines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            evt = _json.loads(line)
                        except ValueError:
                            continue
                        if evt.get("error"):
                            raise BackendError(str(evt["error"]), retryable=False)
                        total, done = evt.get("total"), evt.get("completed")
                        if progress_cb and total:
                            pct = int(min(100, (done or 0) * 100 / total))
                            progress_cb(pct)
        except httpx.TimeoutException as exc:
            raise BackendError(f"pull 타임아웃: {exc}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise BackendError(
                f"pull 연결 실패: {type(exc).__name__}: {exc}", retryable=True
            ) from exc
        if progress_cb:
            progress_cb(100)

    async def embed(
        self,
        *,
        model: str,
        inputs: list[str],
        timeout: int = 60,
    ) -> "EmbedResult":
        """텍스트 임베딩. /api/embed 는 배열 입력을 한 번에 처리한다.

        구형 /api/embeddings 는 한 번에 한 건뿐이라 배치가 느리다.
        """
        # truncate=false: 기본값(true)이면 모델 컨텍스트를 넘는 입력을 **조용히 잘라내고**
        # status ok 를 돌려준다. 문서 뒷부분이 통째로 빠진 벡터가 저장되는데 에러가
        # 안 나서 발견이 늦다. 통제된 실패로 바꾼다.
        payload = {"model": model, "input": inputs, "truncate": False,
                   "keep_alive": self.keep_alive}
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                res = await client.post(f"{self.base_url}/api/embed", json=payload)
                if res.status_code >= 500:
                    raise BackendError(f"백엔드 오류 HTTP {res.status_code}", retryable=True)
                if res.status_code >= 400:
                    body = res.text[:300]
                    # truncate=false 로 컨텍스트 초과를 거부당한 경우를 구분한다
                    if "context" in body.lower() or "too long" in body.lower():
                        raise InputTooLong(
                            f"입력이 모델 컨텍스트를 넘습니다. 더 짧게 잘라 보내세요: {body}"
                        )
                    raise BackendError(
                        f"임베딩 거부 HTTP {res.status_code}: {body}", retryable=False,
                    )
                data = res.json()
        except httpx.TimeoutException as exc:
            raise BackendError(f"임베딩 타임아웃: {exc}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise BackendError(
                f"임베딩 연결 실패: {type(exc).__name__}: {exc}", retryable=True
            ) from exc

        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or not vectors:
            raise BackendError("임베딩 응답 형식 오류(embeddings 없음)", retryable=False)
        total = data.get("total_duration")
        return EmbedResult(
            embeddings=vectors,
            prompt_eval_count=data.get("prompt_eval_count"),
            duration_ms=round(total / 1_000_000) if total else None,
        )

    async def tags_detailed(self, timeout: int = 5) -> list[dict]:
        """보유 모델 상세. /api/tags 는 이름 말고도 크기·양자화를 준다.

        디스크 크기를 알면 "모델 크기 20GB 로 가정" 이라는 위험한 기본값을 쓸
        일이 줄고, 대시보드에서 무엇이 맥 디스크를 먹는지 보여줄 수 있다.
        """
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                res = await client.get(f"{self.base_url}/api/tags")
                res.raise_for_status()
                data = res.json()
        except httpx.HTTPError as exc:
            raise BackendError(f"{type(exc).__name__}: {exc}") from exc
        out = []
        for m in data.get("models") or []:
            name = m.get("name")
            if not name:
                continue
            details = m.get("details") or {}
            size = m.get("size")
            out.append({
                "name": name,
                "size_bytes": size,
                "size_gb": round(size / 1_000_000_000, 2) if size else None,
                "parameter_size": details.get("parameter_size"),
                "quantization": details.get("quantization_level"),
                "family": details.get("family"),
                "modified_at": m.get("modified_at"),
            })
        return sorted(out, key=lambda d: d["name"])

    async def tags(self, timeout: int = 5) -> list[str]:
        """보유 모델 이름 목록. 실패 시 BackendError."""
        return [m["name"] for m in await self.tags_detailed(timeout=timeout)]

    async def delete(self, model: str, *, timeout: int = 30) -> bool:
        """맥에서 모델을 지운다. 이미 없으면(404) 성공으로 본다(멱등).

        반환값은 "실제로 지웠는가" — 404 였으면 False 지만 예외는 아니다.
        """
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                res = await client.request(
                    "DELETE", f"{self.base_url}/api/delete", json={"model": model}
                )
            if res.status_code == 404:
                return False
            if res.status_code >= 500:
                raise BackendError(f"백엔드 오류 HTTP {res.status_code}", retryable=True)
            if res.status_code >= 400:
                raise BackendError(
                    f"삭제 거부 HTTP {res.status_code}: {res.text[:200]}",
                    retryable=False,
                )
        except httpx.TimeoutException as exc:
            raise BackendError(f"삭제 타임아웃: {exc}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise BackendError(
                f"삭제 연결 실패: {type(exc).__name__}: {exc}", retryable=True
            ) from exc
        return True
