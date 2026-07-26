"""서비스 토큰 인증 + 레이트리밋.

토큰 → 서비스 식별로 사용량 귀속과 역할 접근 제어를 함께 처리한다.
"""

from __future__ import annotations

import hmac
import time
from collections import defaultdict, deque

from .config import Service, ServiceConfig


class RateLimiter:
    """서비스별 분당 호출 제한 (슬라이딩 윈도)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque] = defaultdict(deque)

    def allow(self, service: Service, now: float | None = None) -> bool:
        limit = service.rate_limit_per_min
        if limit <= 0:
            return True
        now = now if now is not None else time.time()
        q = self._hits[service.name]
        cutoff = now - 60
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def authenticate(services: ServiceConfig, header: str | None) -> Service | None:
    """Authorization 헤더에서 서비스를 식별한다. 실패 시 None."""
    if not header or not header.startswith("Bearer "):
        return None
    presented = header[len("Bearer "):].strip()
    if not presented:
        return None
    candidate = services.by_token(presented)
    if candidate is None:
        return None
    # 역인덱스 조회 후에도 상수시간 비교로 재확인
    if not hmac.compare_digest(candidate.token, presented):
        return None
    return candidate
