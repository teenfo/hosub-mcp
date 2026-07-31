"""키움 REST API 클라이언트.

TR ID / 경로는 공개 자료 기준 초안이다. 공식 문서(openapi.kiwoom.com 로그인)와
대조하고 모의투자에서 호출 확인 후 사용할 것 (README 참고).
초당 요청 제한을 지키기 위해 간단한 토큰버킷을 둔다.
"""
import asyncio
import logging
from collections import deque
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from .. import settings
from . import venue
from .auth import token_manager

# --- TR ID 초안 (모의투자에서 검증 필요) ---
TR_MINUTE_CHART = "ka10080"   # 주식 분봉차트 조회
TR_DAILY_CHART = "ka10081"    # 주식 일봉차트 조회
TR_ORDER_BUY = "kt10000"      # 주식 매수주문
TR_ORDER_SELL = "kt10001"     # 주식 매도주문
TR_ACCOUNT_BALANCE = "kt00018"  # 계좌평가잔고
# 실현손익 계열 — 2026-07-28 실호출로 전부 return_code=0 확인
TR_SYMBOL_REALIZED = "ka10073"   # 일자별종목별실현손익(기간) — 매도 건별
TR_DAILY_REALIZED = "ka10074"    # 일자별실현손익 — 기간 합계 + 수수료·거래세
TR_EXECUTIONS = "ka10076"        # 체결내역 — ord_no·실체결가·실비용
TR_TRADE_VALUE_RANK = "ka10032"  # 거래대금상위 (flu_rt/trde_prica 포함)
TR_VOLUME_SURGE = "ka10023"      # 거래량급증 (sdnin_rt 급증률 포함)
TR_CHANGE_RATE_RANK = "ka10027"  # 전일대비등락률상위 (급등률 상위)
TR_STOCK_LIST = "ka10099"        # 종목정보 리스트 (요청 필드 실호출 검증 필요)

TR_QUOTE = "ka10006"             # 주식시세 — flu_rt(등락률) + cntr_str(체결강도)
TR_STOCK_BASIC = "ka10001"       # 주식기본정보 — 시총·PER·EPS·52주 고저 등
TR_WATCH_INFO = "ka10095"        # 관심종목정보 — **여러 종목을 1콜로**. 편입 게이트용
# 아래 둘은 관측 전용 — 신호·점수 경로에 넣지 않는다. 경로가 서로 다르다(실호출 확정)
TR_INTRADAY_INVESTOR = "ka10063"  # 장중 투자자별 매매 — 800종목 1콜. PATH_MARKET
TR_VI_STOCKS = "ka10054"          # VI 발동종목 — 100종목. PATH_STOCK_INFO

PATH_CHART = "/api/dostk/chart"
PATH_MARKET = "/api/dostk/mrkcond"
PATH_ORDER = "/api/dostk/ordr"
PATH_ACCOUNT = "/api/dostk/acnt"
PATH_RANK = "/api/dostk/rkinfo"
PATH_STOCK_INFO = "/api/dostk/stkinfo"

log = logging.getLogger("trading.kiwoom")


class RateLimiter:
    """초당 호출을 제한하는 토큰버킷 + 429 자동 감속(적응형).

    한도 초과(429)를 받으면 유효 rps 를 즉시 절반으로 낮추고(최소 MIN_RPS),
    이후 무사고 구간이 이어지면 단계적으로 원래 상한까지 회복한다.
    서버가 스스로 눌러주므로 사용자가 주기를 잘못 잡아도 폭주하지 않는다.
    """

    MIN_RPS = 0.5
    RECOVER_AFTER_SEC = 120      # 마지막 429 이후 이 시간이 지나면 1단계 회복
    RECOVER_STEP = 0.5           # 회복 폭(rps)

    def __init__(self, max_rps: float = 4) -> None:
        self.max_rps = float(max_rps)        # 설정 상한(원복 목표)
        self.effective_rps = float(max_rps)  # 현재 적용 중인 상한
        self._last = 0.0
        self._lock = asyncio.Lock()
        self.waited_sec = 0.0        # 누적 대기 — 한도에 눌린 정도(부하 지표)
        self.throttled_until = 0.0   # 감속 상태 표시용(마지막 429 시각 기준)
        self.penalties = 0           # 자동 감속 발동 횟수
        # 우선 호출(매매 데스크)이 대기 중인 수. 일반 호출은 이 값이 0이 될 때까지
        # 자리를 비켜 준다 — 데스크가 분봉 백필 뒤에 줄 서면 2초 주기가 무의미해진다.
        self._priority_waiting = 0
        self.yielded = 0             # 일반 호출이 양보한 횟수(관측치)

    @property
    def interval(self) -> float:
        return 1.0 / max(self.effective_rps, self.MIN_RPS)

    def penalize(self) -> None:
        """429 수신 — 유효 rps 를 절반으로 즉시 감속."""
        self.effective_rps = max(self.MIN_RPS, round(self.effective_rps / 2, 2))
        self.throttled_until = time.monotonic() + self.RECOVER_AFTER_SEC
        self.penalties += 1

    def _maybe_recover(self) -> None:
        """무사고 구간이 이어지면 상한까지 단계적 회복."""
        if self.effective_rps >= self.max_rps:
            return
        if time.monotonic() >= self.throttled_until:
            self.effective_rps = min(self.max_rps,
                                     round(self.effective_rps + self.RECOVER_STEP, 2))
            self.throttled_until = time.monotonic() + self.RECOVER_AFTER_SEC

    @property
    def throttled(self) -> bool:
        return self.effective_rps < self.max_rps

    # 일반 호출이 연속으로 양보할 수 있는 최대 횟수. 데스크는 2초마다 보유
    # 종목만큼 부르므로 우선 대기가 끊이지 않을 수 있다 — 상한이 없으면 분봉
    # 백필이 영구히 굶고 신호가 아예 안 나온다. 양보는 늦추는 것이지 버리는 것이
    # 아니어야 한다.
    MAX_YIELDS = 5

    async def wait(self, priority: bool = False) -> None:
        """호출 간 최소 간격 확보. `priority` 는 매매 데스크 전용 레인이다.

        데스크는 초 단위로 실거래 청산을 판정한다. 그 호출이 분봉 백필 30여 건
        뒤에 줄 서면 2초 주기가 이름만 남는다(실측 2026-07-30: chart 149콜/분에
        눌려 데스크 사이클이 늘어졌다). 우선 호출이 대기 중이면 일반 호출은
        간격만큼 비켜 준다.
        """
        if priority:
            self._priority_waiting += 1
        try:
            yields = 0
            while True:
                async with self._lock:
                    if priority or not self._priority_waiting or yields >= self.MAX_YIELDS:
                        self._maybe_recover()
                        now = time.monotonic()
                        delta = now - self._last
                        if delta < self.interval:
                            pause = self.interval - delta
                            self.waited_sec += pause
                            await asyncio.sleep(pause)
                        self._last = time.monotonic()
                        return
                yields += 1
                self.yielded += 1
                await asyncio.sleep(self.interval)   # 락 밖에서 양보
        finally:
            if priority:
                self._priority_waiting -= 1


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
        max_rps = limiter.effective_rps if limiter else 0
        # 최근 10초 실측 rps 대비 현재 유효 상한 사용률
        rps = round(last_10s / 10, 2)
        return {
            "calls_1m": last_min,
            "calls_1h": len(self._calls),
            "calls_total": self.total,
            "rps_10s": rps,
            "max_rps": max_rps,
            "configured_rps": limiter.max_rps if limiter else 0,
            "auto_throttled": bool(limiter and limiter.throttled),
            "penalties": limiter.penalties if limiter else 0,
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

    def _record(self, status: int) -> None:
        """계측 + 429 자동 감속."""
        self.usage.record(status)
        if status == 429:
            self._limiter.penalize()
            log.warning("키움 API 한도 초과(429) — 자동 감속: %.2f rps",
                        self._limiter.effective_rps)

    async def _request(self, path: str, tr_id: str, body: dict,
                       cont: str = "N", next_key: str = "",
                       priority: bool = False) -> tuple[dict, str]:
        """호출 + 연속조회 키 반환 → (응답 JSON, 다음 페이지 키).

        키움은 이어질 데이터가 있으면 응답 헤더에 cont-yn=Y 와 next-key 를 준다.
        cont-yn 이 Y 가 아니면 빈 문자열을 돌려 호출부가 루프를 끝내게 한다.
        """
        await self._limiter.wait(priority)
        token = await token_manager.get()
        headers = {
            "authorization": f"Bearer {token}",
            "api-id": tr_id,
            "cont-yn": cont,
        }
        if next_key:
            headers["next-key"] = next_key
        # base URL 은 호출 시점에 읽는다 — 설정 화면에서 mock/real 전환 즉시 반영
        try:
            resp = await self._http.post(
                settings.REST_BASE + path, json=body, headers=headers,
            )
        except httpx.HTTPError:
            self._record(599)               # 네트워크 실패도 부하 지표에 포함
            raise
        self._record(resp.status_code)
        resp.raise_for_status()
        nxt = ""
        if str(resp.headers.get("cont-yn", "N")).upper() == "Y":
            nxt = resp.headers.get("next-key", "") or ""
        return resp.json(), nxt

    async def _call(self, path: str, tr_id: str, body: dict, cont: str = "N",
                    priority: bool = False) -> dict:
        data, _ = await self._request(path, tr_id, body, cont, priority=priority)
        return data

    # --- 시세 ---
    def _minute_body(self, symbol: str, interval: int) -> dict:
        return {"stk_cd": symbol, "tic_scope": str(interval), "upd_stkpc_tp": "1"}

    async def minute_chart(self, symbol: str, interval: int = 1) -> dict:
        """분봉차트 최신 1페이지(900봉 ≈ 2.4거래일). interval: 1/3/5/... 분."""
        return await self._call(
            PATH_CHART, TR_MINUTE_CHART, self._minute_body(symbol, interval),
        )

    async def minute_chart_page(self, symbol: str, interval: int = 1,
                                next_key: str = "") -> tuple[dict, str]:
        """분봉차트 연속조회 1페이지 → (응답, 다음 페이지 키).

        next_key 가 비면 최신 페이지, 있으면 그보다 과거 900봉을 받는다.
        더 이상 과거가 없으면 두 번째 값이 빈 문자열이다.
        """
        return await self._request(
            PATH_CHART, TR_MINUTE_CHART, self._minute_body(symbol, interval),
            cont="Y" if next_key else "N", next_key=next_key,
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

    async def trade_value_rank(self, market: str = "000",
                               stex_tp: str = venue.TP_KRX) -> dict:
        """거래대금 상위. market: 000 전체 / 001 코스피 / 101 코스닥.

        `stex_tp` 를 실호출로 확정했다(2026-07-31, `kiwoom/venue.py` 참조):
        1=KRX · 2=NXT(`005930_NX`) · 3=통합(`000660_AL`). 기본값은 KRX 라
        기존 호출부는 동작이 바뀌지 않는다. NXT 는 **관측 경로만** 쓴다 —
        응답 코드에 접미사가 붙으므로 정규화 없이 매매 경로에 넣으면 안 된다.
        """
        return await self._call(
            PATH_RANK,
            TR_TRADE_VALUE_RANK,
            {"mrkt_tp": market, "mang_stk_incls": "0", "stex_tp": stex_tp},
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

    async def quote(self, code: str, priority: bool = False) -> dict:
        """현재 시세 (ka10006). 등락률·체결강도가 같이 온다.

        `priority=True` 는 **매매 데스크 폴백** 전용이다. 데스크는 2초마다 실거래
        청산을 판정하므로 이 호출이 분봉 백필 뒤에 줄 서면 주기가 이름만 남는다.
        """
        return await self._call(PATH_MARKET, TR_QUOTE, {"stk_cd": code},
                                priority=priority)

    async def stock_basic(self, code: str) -> dict:
        """주식기본정보 (ka10001) — 시총·PER·EPS·52주 고저·상하한가 등.

        사람이 종목을 눌렀을 때만 부른다. 자동 루프가 쓰지 않으므로 레이트리밋
        예산에 미치는 영향이 사실상 없다.
        """
        return await self._call(PATH_STOCK_INFO, TR_STOCK_BASIC, {"stk_cd": code})

    async def watch_info(self, codes: list[str]) -> dict:
        """관심종목정보 (ka10095) — 종목코드를 '|' 로 이어 **한 번에** 조회한다.

        ka10006(주식시세)은 종목당 1콜이라 편입 게이트가 목록을 훑을 때마다
        종목 수만큼 콜이 든다. 이건 89종목을 1콜로 받는다(실측 2026-07-29).
        응답 배열 키는 `atn_stk_infr`, 거래대금(trde_prica)·체결강도(cntr_str)·
        5호가·시총이 한 행에 함께 온다.
        """
        return await self._call(PATH_STOCK_INFO, TR_WATCH_INFO,
                                {"stk_cd": "|".join(codes)})

    async def intraday_investor(self, market: str = "000", invsr: str = "6") -> dict:
        """장중 투자자별 매매 (ka10063) — **800종목을 1콜로**. 관측 전용.

        경로가 `mrkcond` 이고 필수 파라미터가 `invsr` 다. `rkinfo`·`stkinfo`·
        `frgnistt` 는 전부 1504(지원하지 않는 API ID)를 돌려주고, `invsr_tp`
        로 보내면 1511(필수 입력 값 없음)이 온다 — 실호출로 확정했다(2026-07-31).

        invsr: 6 외국인 / 7 기관계 등. amt_qty_tp 1 = 금액(백만원) 기준.
        """
        return await self._call(
            PATH_MARKET, TR_INTRADAY_INVESTOR,
            {"mrkt_tp": market, "amt_qty_tp": "1", "invsr": invsr,
             "frgn_all": "0", "smtm_netprps_tp": "0", "stex_tp": "1"},
        )

    async def vi_stocks(self, market: str = "000") -> dict:
        """VI(변동성완화장치) 발동종목 (ka10054) — 100종목. 관측 전용.

        경로는 `stkinfo` 다(`mrkcond` 는 1504). 발동 중에는 단일가로 묶이므로
        시장가가 생각한 값에 체결되지 않는다. 지금은 겹치는지만 센다.
        """
        return await self._call(
            PATH_STOCK_INFO, TR_VI_STOCKS,
            {"mrkt_tp": market, "bf_mkrt_tp": "0", "stk_cd": "", "motn_tp": "0",
             "skip_stk": "000000000", "trde_qty_tp": "0", "min_trde_qty": "0",
             "max_trde_qty": "0", "trde_prica_tp": "0", "min_trde_prica": "0",
             "max_trde_prica": "0", "motn_drc": "0", "stex_tp": "1"},
        )

    async def daily_realized(self, start: str, end: str) -> dict:
        """일자별 실현손익 (ka10074). 날짜는 YYYYMMDD.

        **증권사가 계산한 순 실현손익**이다 — 수수료·거래세가 이미 빠져 있다.
        원장은 엔진이 낸 주문만 알므로 수동매매가 섞이면 계좌와 어긋난다.
        그 차이를 재는 기준선이 이 값이다.
        """
        return await self._call(PATH_ACCOUNT, TR_DAILY_REALIZED,
                                {"strt_dt": start, "end_dt": end})

    async def symbol_realized(self, code: str, start: str, end: str) -> dict:
        """종목별 실현손익, 매도 건별 (ka10073)."""
        return await self._call(PATH_ACCOUNT, TR_SYMBOL_REALIZED,
                                {"stk_cd": code, "strt_dt": start, "end_dt": end})

    async def executions(self, code: str = "") -> dict:
        """체결내역 (ka10076). `ord_no` 로 원장 포지션과 이어붙인다.

        조회 범위가 당일이므로 마감 후 한 번 훑어 그날 체결을 확정한다.
        """
        return await self._call(
            PATH_ACCOUNT, TR_EXECUTIONS,
            {"stk_cd": code, "qry_tp": "0", "sell_tp": "0", "ord_no": "",
             "stex_tp": "0"})

    async def aclose(self) -> None:
        await self._http.aclose()


client = KiwoomClient()
