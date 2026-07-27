"""키움 REST API 클라이언트.

TR ID / 경로는 공개 자료 기준 초안이다. 공식 문서(openapi.kiwoom.com 로그인)와
대조하고 모의투자에서 호출 확인 후 사용할 것 (README 참고).
초당 요청 제한을 지키기 위해 간단한 토큰버킷을 둔다.
"""
import asyncio
from collections import deque
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from .. import settings
from .auth import token_manager

# --- TR ID 초안 (모의투자에서 검증 필요) ---
TR_MINUTE_CHART = "ka10080"   # 주식 분봉차트 조회
TR_DAILY_CHART = "ka10081"    # 주식 일봉차트 조회
TR_ORDER_BUY = "kt10000"      # 주식 매수주문
TR_ORDER_SELL = "kt10001"     # 주식 매도주문
TR_ACCOUNT_BALANCE = "kt00018"  # 계좌평가잔고
TR_TRADE_VALUE_RANK = "ka10032"  # 거래대금상위 (flu_rt/trde_prica 포함)
TR_VOLUME_SURGE = "ka10023"      # 거래량급증 (sdnin_rt 급증률 포함)
TR_CHANGE_RATE_RANK = "ka10027"  # 전일대비등락률상위 (급등률 상위)
TR_STOCK_LIST = "ka10099"        # 종목정보 리스트 (요청 필드 실호출 검증 필요)

PATH_CHART = "/api/dostk/chart"
PATH_ORDER = "/api/dostk/ordr"
PATH_ACCOUNT = "/api/dostk/acnt"
PATH_RANK = "/api/dostk/rkinfo"
PATH_STOCK_INFO = "/api/dostk/stkinfo"


class RateLimiter:
    """초당 max_rps 회로 호출을 제한하는 토큰버킷."""

    def __init__(self, max_rps: int = 4) -> None:
        self.max_rps = max_rps
        self.interval = 1.0 / max_rps
        self._last = 0.0
        self._lock = asyncio.Lock()
        self.waited_sec = 0.0        # 누적 대기 — 한도에 눌린 정도(부하 지표)

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self.interval:
                pause = self.interval - delta
                self.waited_sec += pause
                await asyncio.sleep(pause)
            self._last = time.monotonic()


class ApiUsage:
    """키움 REST 호출 계측 — 화면에 부하·한도 여유를 보여주기 위한 관측치.

    최근 60초/1시간 호출 타임스탬프만 들고 있어 메모리 부담이 없다.
    한도 초과(429)·에러는 별도 카운트해 '지금 눌리고 있는지'를 판단한다.
    """

    def __init__(self, window_sec: int = 3600) -> None:
        self.window = window_sec
        self._calls: deque[float] = deque()
        self._errors: deque[tuple[float, int]] = deque()   # (ts, status)
        self.total = 0
        self.rate_limited = 0                              # 429 누적

    def record(self, status: int | None = None) -> None:
        now = time.time()
        self._calls.append(now)
        self.total += 1
        if status and status >= 400:
            self._errors.append((now, status))
            if status == 429:
                self.rate_limited += 1
        cutoff = now - self.window
        while self._calls and self._calls[0] < cutoff:
            self._calls.popleft()
        while self._errors and self._errors[0][0] < cutoff:
            self._errors.popleft()

    def snapshot(self, limiter: "RateLimiter | None" = None) -> dict:
        now = time.time()
        last_min = sum(1 for t in self._calls if t >= now - 60)
        last_10s = sum(1 for t in self._calls if t >= now - 10)
        max_rps = limiter.max_rps if limiter else 0
        # 최근 10초 실측 rps 대비 클라이언트 상한 사용률
        rps = round(last_10s / 10, 2)
        return {
            "calls_1m": last_min,
            "calls_1h": len(self._calls),
            "calls_total": self.total,
            "rps_10s": rps,
            "max_rps": max_rps,
            "usage_pct": round(rps / max_rps * 100) if max_rps else 0,
            "errors_1h": len(self._errors),
            "rate_limited_1h": sum(1 for _, s in self._errors if s == 429),
            "rate_limited_total": self.rate_limited,
            "throttle_wait_sec": round(limiter.waited_sec, 1) if limiter else 0.0,
            "last_error": (f"HTTP {self._errors[-1][1]}" if self._errors else ""),
        }


class KiwoomClient:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=15)
        self._limiter = RateLimiter()
        self.usage = ApiUsage()

    def usage_snapshot(self) -> dict:
        return self.usage.snapshot(self._limiter)

    async def _call(self, path: str, tr_id: str, body: dict, cont: str = "N") -> dict:
        await self._limiter.wait()
        token = await token_manager.get()
        # base URL 은 호출 시점에 읽는다 — 설정 화면에서 mock/real 전환 즉시 반영
        try:
            resp = await self._http.post(
                settings.REST_BASE + path,
                json=body,
                headers={
                    "authorization": f"Bearer {token}",
                    "api-id": tr_id,
                    "cont-yn": cont,
                },
            )
        except httpx.HTTPError:
            self.usage.record(599)          # 네트워크 실패도 부하 지표에 포함
            raise
        self.usage.record(resp.status_code)
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
        """일봉차트. base_dt 는 필수 (빈 값이면 1511 입력값 오류) — 기본 오늘(KST)."""
        if not base_date:
            base_date = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
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

    async def trade_value_rank(self, market: str = "000") -> dict:
        """거래대금 상위. market: 000 전체 / 001 코스피 / 101 코스닥.
        stex_tp 값은 문서 미상 — 모의투자 호출로 검증 필요."""
        return await self._call(
            PATH_RANK,
            TR_TRADE_VALUE_RANK,
            {"mrkt_tp": market, "mang_stk_incls": "0", "stex_tp": "1"},
        )

    async def volume_surge_rank(self, market: str = "000", sort: str = "2") -> dict:
        """거래량급증 (ka10023). sort: 1 급증량 / 2 급증률. tm_tp=1 분 단위."""
        return await self._call(
            PATH_RANK,
            TR_VOLUME_SURGE,
            {
                "mrkt_tp": market, "sort_tp": sort, "tm_tp": "1", "tm": "",
                "trde_qty_tp": "50", "stk_cnd": "20",  # 5만주↑, ETF/ETN/스팩 제외
                "pric_tp": "0", "stex_tp": "1",
            },
        )

    async def change_rate_rank(self, market: str = "001", sort: str = "1") -> dict:
        """전일대비 등락률 상위(ka10027) = 급등률 상위. market: 001 코스피 / 101 코스닥.
        sort: 1 상승률. 요청 필드는 계정/환경에 따라 다를 수 있어 라이브 호출로
        검증 후 조정할 것(응답 파싱은 parse_rank 가 stk_cd/flu_rt/cur_prc 로 견고)."""
        return await self._call(
            PATH_RANK,
            TR_CHANGE_RATE_RANK,
            {
                "mrkt_tp": market, "sort_tp": sort, "trde_qty_cnd": "0000",
                "stk_cnd": "0", "crd_cnd": "0", "updown_incls": "1",
                "pric_cnd": "0", "trde_prica_cnd": "0", "stex_tp": "1",
            },
        )

    async def stock_list(self, market: str = "0") -> dict:
        """종목정보 리스트 (ka10099). market: 0 코스피 / 10 코스닥 (실호출 검증;
        000/001/101 은 null 반환). 응답 배열 키 'list', 필드 code/name."""
        return await self._call(PATH_STOCK_INFO, TR_STOCK_LIST, {"mrkt_tp": market})

    async def balance(self) -> dict:
        return await self._call(
            PATH_ACCOUNT, TR_ACCOUNT_BALANCE, {"qry_tp": "1", "dmst_stex_tp": "KRX"}
        )

    async def aclose(self) -> None:
        await self._http.aclose()


client = KiwoomClient()
