"""키움 REST API 클라이언트.

TR ID / 경로는 공개 자료 기준 초안이다. 공식 문서(openapi.kiwoom.com 로그인)와
대조하고 모의투자에서 호출 확인 후 사용할 것 (README 참고).
초당 요청 제한을 지키기 위해 간단한 토큰버킷을 둔다.
"""
import asyncio
import time

import httpx

from .. import settings
from .auth import token_manager

# --- TR ID 초안 (모의투자에서 검증 필요) ---
TR_MINUTE_CHART = "ka10080"   # 주식 분봉차트 조회
TR_DAILY_CHART = "ka10081"    # 주식 일봉차트 조회
TR_ORDER_BUY = "kt10000"      # 주식 매수주문
TR_ORDER_SELL = "kt10001"     # 주식 매도주문
TR_ACCOUNT_BALANCE = "kt00018"  # 계좌평가잔고

PATH_CHART = "/api/dostk/chart"
PATH_ORDER = "/api/dostk/ordr"
PATH_ACCOUNT = "/api/dostk/acnt"


class RateLimiter:
    """초당 max_rps 회로 호출을 제한하는 토큰버킷."""

    def __init__(self, max_rps: int = 4) -> None:
        self.interval = 1.0 / max_rps
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self.interval:
                await asyncio.sleep(self.interval - delta)
            self._last = time.monotonic()


class KiwoomClient:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=15)
        self._limiter = RateLimiter()

    async def _call(self, path: str, tr_id: str, body: dict, cont: str = "N") -> dict:
        await self._limiter.wait()
        token = await token_manager.get()
        # base URL 은 호출 시점에 읽는다 — 설정 화면에서 mock/real 전환 즉시 반영
        resp = await self._http.post(
            settings.REST_BASE + path,
            json=body,
            headers={
                "authorization": f"Bearer {token}",
                "api-id": tr_id,
                "cont-yn": cont,
            },
        )
        resp.raise_for_status()
        return resp.json()

    # --- 시세 ---
    async def minute_chart(self, symbol: str, interval: int = 1) -> dict:
        """분봉차트. interval: 1/3/5/... 분."""
        return await self._call(
            PATH_CHART,
            TR_MINUTE_CHART,
            {"stk_cd": symbol, "tic_scope": str(interval), "upd_stkpc_tp": "1"},
        )

    async def daily_chart(self, symbol: str, base_date: str = "") -> dict:
        return await self._call(
            PATH_CHART,
            TR_DAILY_CHART,
            {"stk_cd": symbol, "base_dt": base_date, "upd_stkpc_tp": "1"},
        )

    # --- 주문 ---
    async def order(self, side: str, symbol: str, qty: int, price: int = 0) -> dict:
        """side: buy/sell. price=0 이면 시장가."""
        tr = TR_ORDER_BUY if side == "buy" else TR_ORDER_SELL
        body = {
            "dmst_stex_tp": "KRX",
            "stk_cd": symbol,
            "ord_qty": str(qty),
            "ord_uv": str(price) if price else "",
            "trde_tp": "3" if price == 0 else "0",  # 3=시장가, 0=보통(지정가)
        }
        return await self._call(PATH_ORDER, tr, body)

    async def balance(self) -> dict:
        return await self._call(
            PATH_ACCOUNT, TR_ACCOUNT_BALANCE, {"qry_tp": "1", "dmst_stex_tp": "KRX"}
        )

    async def aclose(self) -> None:
        await self._http.aclose()


client = KiwoomClient()
