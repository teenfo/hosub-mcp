"""키움 REST API 접근토큰 발급·캐시.

POST {REST_BASE}/oauth2/token
body: {"grant_type": "client_credentials", "appkey": ..., "secretkey": ...}
응답의 token / expires_dt 를 캐시하고 만료 60초 전에 갱신한다.
"""
import time

import httpx

from .. import settings


class TokenManager:
    def __init__(self) -> None:
        self._token: str | None = None
        self._expires_at: float = 0.0

    async def get(self) -> str:
        if self._token and time.time() < self._expires_at - 60:
            return self._token
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
