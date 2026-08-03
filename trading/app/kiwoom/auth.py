"""키움 REST API 접근토큰 발급·캐시.

POST {REST_BASE}/oauth2/token
body: {"grant_type": "client_credentials", "appkey": ..., "secretkey": ...}
응답의 token / expires_dt 를 캐시하고 만료 60초 전에 갱신한다.
"""
import asyncio
import time

import httpx

from .. import settings


class TokenManager:
    def __init__(self) -> None:
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    def reset(self) -> None:
        """키/환경 변경·토큰 무효 감지 시 캐시 무효화."""
        self._token = None
        self._expires_at = 0.0

    async def get(self) -> str:
        if self._token and time.time() < self._expires_at - 60:
            return self._token
        # **발급은 한 번에 하나만.** 만료 시점에 REST·WS 가 동시에 갱신을 타면
        # 같은 초에 토큰이 두 번 발급된다(실측 2026-08-03 15:11 — 두 번 발급
        # 직후 REST 가 200-빈 응답, WS 가 LOGIN 인증 경합에 빠졌다. 증권사가
        # 재발급 시 이전 토큰을 무효화하면 늦게 저장된 쪽이 죽은 토큰일 수
        # 있다). 락 안에서 재확인해 첫 획득자만 발급한다.
        async with self._lock:
            if self._token and time.time() < self._expires_at - 60:
                return self._token
            return await self._fetch()

    async def _fetch(self) -> str:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{settings.REST_BASE}/oauth2/token",
                json={
                    "grant_type": "client_credentials",
                    "appkey": settings.KIWOOM_APP_KEY,
                    "secretkey": settings.KIWOOM_SECRET_KEY,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        token = data.get("token") or data.get("access_token")
        if not token:
            raise RuntimeError(f"토큰 발급 실패: {data}")
        self._token = token
        # expires_dt(YYYYMMDDHHMMSS) 또는 expires_in(초) 대응
        expires_in = data.get("expires_in")
        self._expires_at = time.time() + (int(expires_in) if expires_in else 6 * 3600)
        return token


token_manager = TokenManager()
