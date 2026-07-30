"""실시간 틱 → 1분봉 집계 + REST 분봉 백필."""
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from ..kiwoom.client import client
from . import store

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")


class BarAggregator:
    """체결 틱을 받아 1분봉을 만들고, 봉이 닫힐 때 store 에 저장한다."""

    def __init__(self) -> None:
        self._current: dict[str, dict] = {}  # symbol -> {minute, o,h,l,c,v}
        # 마지막 틱이 **도착한** 시각(monotonic). 봉의 분 시각(`minute`)과 다르다 —
        # 거래가 뜸하면 봉은 남아 있는데 값이 낡는다. 매매 데스크가 "이 가격을
        # 믿어도 되는가" 를 판정하려면 도착 시각이 필요하다.
        self._seen: dict[str, float] = {}

    async def on_tick(self, symbol: str, price: float, volume: int, ts: str) -> None:
        self._seen[symbol] = time.monotonic()
        now = datetime.now(KST)
        if len(ts) == 6:  # HHMMSS
            now = now.replace(
                hour=int(ts[0:2]), minute=int(ts[2:4]), second=int(ts[4:6]), microsecond=0
            )
        minute = now.replace(second=0, microsecond=0)
        bar = self._current.get(symbol)
        if bar and bar["minute"] != minute:
            self._flush(symbol, bar)
            bar = None
        if bar is None:
            self._current[symbol] = {
                "minute": minute, "o": price, "h": price, "l": price, "c": price, "v": volume,
            }
            return
        bar["h"] = max(bar["h"], price)
        bar["l"] = min(bar["l"], price)
        bar["c"] = price
        bar["v"] += volume

    def apply_rest_price(self, symbol: str, price: float) -> None:
        """REST 로 받은 현재가를 형성 중인 봉에 반영한다 (WS 틱이 아니다).

        데스크가 폴백으로 조회한 시세를 화면·`_price_of` 도 볼 수 있게 하려고
        둔다. 종전에는 데스크만 쓰고 버려서, WS 틱이 오지 않는 종목은 화면
        가격이 분봉 백필 주기까지 고정됐다(실측 2026-07-30 ETF).

        **`_seen` 을 건드리지 않는 것이 핵심이다.** 갱신하면 다음 사이클에
        `fresh_price` 가 이 값을 '신선한 WS 값' 으로 돌려주고, 데스크 요약의
        `WS x · REST y` 가 거짓이 된다 — WS 가 죽었는지 살았는지 못 가리게 된다.
        그 로그가 오늘 전환의 유일한 진단 수단이다.

        거래량은 더하지 않는다. REST 는 누적 거래량을 주므로 틱처럼 더하면
        봉의 거래량이 폭증한다.
        """
        now = datetime.now(KST)
        minute = now.replace(second=0, microsecond=0)
        bar = self._current.get(symbol)
        if bar and bar["minute"] != minute:
            self._flush(symbol, bar)
            bar = None
        if bar is None:
            self._current[symbol] = {"minute": minute, "o": price, "h": price,
                                     "l": price, "c": price, "v": 0}
            return
        bar["h"] = max(bar["h"], price)
        bar["l"] = min(bar["l"], price)
        bar["c"] = price

    def snapshot(self, symbol: str) -> dict | None:
        """형성 중인 현재 분봉 (차트 실시간 표시용)."""
        bar = self._current.get(symbol)
        if not bar:
            return None
        return {
            "time": int(bar["minute"].timestamp()),
            "open": bar["o"], "high": bar["h"], "low": bar["l"],
            "close": bar["c"], "volume": bar["v"],
        }

    def age_sec(self, symbol: str) -> float | None:
        """마지막 틱이 도착한 뒤 지난 초. 한 번도 못 받았으면 None.

        `snapshot()` 의 `time` 은 봉의 분 시각이라 신선도 판정에 쓸 수 없다.
        """
        seen = self._seen.get(symbol)
        return None if seen is None else max(0.0, time.monotonic() - seen)

    def fresh_price(self, symbol: str, stale_sec: float) -> float | None:
        """`stale_sec` 이내에 틱이 온 종목의 현재가. 낡았거나 없으면 None.

        **호출자는 None 을 '가격 없음'이 아니라 '못 믿는다'로 읽어야 한다** —
        REST 로 보충하든지 이번 판정을 건너뛰든지 정하는 것은 호출자의 몫이다.
        """
        age = self.age_sec(symbol)
        if age is None or age > stale_sec:
            return None
        bar = self._current.get(symbol)
        return float(bar["c"]) if bar else None

    def _flush(self, symbol: str, bar: dict) -> None:
        df = pd.DataFrame(
            [{"open": bar["o"], "high": bar["h"], "low": bar["l"],
              "close": bar["c"], "volume": bar["v"]}],
            index=[bar["minute"]],
        )
        store.upsert_bars(symbol, "1m", df)


def parse_chart_response(data: dict) -> pd.DataFrame:
    """키움 분봉 응답을 DataFrame 으로. 필드명은 TR 검증 시 함께 확인할 것."""
    items = (
        data.get("stk_min_pole_chart_qry")
        or data.get("stk_dt_pole_chart_qry")
        or data.get("output")
        or []
    )
    rows = []
    for it in items:
        ts_raw = it.get("cntr_tm") or it.get("dt") or ""
        if not ts_raw:
            continue
        fmt = "%Y%m%d%H%M%S" if len(ts_raw) == 14 else "%Y%m%d"
        try:
            ts = datetime.strptime(ts_raw, fmt).replace(tzinfo=KST)
        except ValueError:
            continue
        rows.append(
            {
                "ts": ts,
                "open": abs(float(it.get("open_pric", 0))),
                "high": abs(float(it.get("high_pric", 0))),
                "low": abs(float(it.get("low_pric", 0))),
                "close": abs(float(it.get("cur_prc", 0))),
                "volume": abs(int(float(it.get("trde_qty", 0)))),
            }
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).set_index("ts").sort_index()


async def backfill_minutes(symbol: str) -> int:
    """REST 분봉 조회로 최근 데이터를 채운다."""
    try:
        data = await client.minute_chart(symbol, interval=1)
    except Exception as e:  # noqa: BLE001 - 백필 실패는 치명적이지 않음
        log.warning("백필 실패 %s: %s", symbol, e)
        return 0
    df = parse_chart_response(data)
    if df.empty:
        return 0
    return store.upsert_bars(symbol, "1m", df)


async def deep_backfill(symbol: str, pages: int = 5) -> int:
    """연속조회로 과거 분봉을 여러 페이지 확보한다 (신규 편입 시 종목당 1회).

    분봉 API 는 호출 시각 기준 최근 900봉(≈2.4거래일)만 준다. 언제 부르든
    '최근'이라 마감 후에 불러도 과거가 늘지 않는다. 과거를 늘리는 유일한 길이
    응답 헤더의 next-key 를 물고 다시 부르는 연속조회다. 5페이지면 약 2주치라
    편입 당일부터 백테스트 최소 일수(backtest.min_days)를 채울 수 있다.

    반환: 저장한 봉 수. 첫 페이지 실패는 올려 보내 호출부가 재시도하게 하고,
    2페이지 이후 실패는 확보한 만큼만 남기고 멈춘다.
    """
    total, next_key = 0, ""
    for i in range(max(1, int(pages))):
        try:
            data, next_key = await client.minute_chart_page(symbol, 1, next_key)
        except Exception as e:  # noqa: BLE001
            if i == 0:
                raise                       # 조회 자체 실패 → 완료로 기록하지 않는다
            log.warning("연속조회 중단 %s p%d: %s", symbol, i + 1, e)
            break
        df = parse_chart_response(data)
        if df.empty:
            break
        total += store.upsert_bars(symbol, "1m", df)
        if not next_key:
            break                           # 더 과거가 없다
    return total
