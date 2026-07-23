"""실시간 틱 → 1분봉 집계 + REST 분봉 백필."""
import logging
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

    async def on_tick(self, symbol: str, price: float, volume: int, ts: str) -> None:
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
