"""신호 평가 루프. 1분 주기로 감시 종목의 오늘 분봉을 평가해 승인 큐에 올린다."""
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from .. import settings
from ..data import collector, store
from ..trade import orders, risk
from . import rules

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")


class SignalEngine:
    def __init__(self, equity: float = 10_000_000) -> None:
        self.equity = equity
        self.state = risk.DailyRiskState(
            equity=equity,
            daily_loss_limit_pct=settings.RISK.get("daily_loss_limit_pct", 2.0),
            max_positions=settings.RISK.get("max_positions", 3),
        )
        self._fired: set[tuple[str, str, str]] = set()  # (day, symbol, rule)
        self.last_run: str = ""
        self.last_signals: list[dict] = []

    def _today_df(self, symbol: str):
        df = store.load_bars(symbol, "1m", limit=800)
        if df.empty:
            return df, None
        today = datetime.now(KST).date()
        today_df = df[df.index.date == today]
        prev = df[df.index.date < today]
        prev_close = float(prev["close"].iloc[-1]) if not prev.empty else None
        return today_df, prev_close

    async def run_once(self) -> list[dict]:
        found: list[dict] = []
        day = datetime.now(KST).date().isoformat()
        for symbol, name in settings.WATCHLIST.items():
            await collector.backfill_minutes(symbol)
            df, prev_close = self._today_df(symbol)
            if df.empty:
                continue
            for sig in rules.evaluate_all(df, settings.RULES, prev_close):
                key = (day, symbol, sig.rule)
                if key in self._fired:
                    continue
                ok, why = self.state.can_open()
                if not ok:
                    log.info("신호 차단 %s %s: %s", symbol, sig.rule, why)
                    continue
                sig.symbol = symbol
                qty = risk.position_size(
                    self.equity, settings.RISK.get("risk_per_trade_pct", 0.5),
                    sig.entry, sig.stop,
                )
                if qty <= 0:
                    continue
                order_id = orders.propose(sig, qty)
                self._fired.add(key)
                found.append(
                    {"order_id": order_id, "symbol": symbol, "name": name,
                     "rule": sig.rule, "side": sig.side, "reason": sig.reason,
                     "entry": sig.entry, "stop": sig.stop, "target": sig.target,
                     "qty": qty}
                )
                log.info("신호 등록 %s(%s) %s %s", name, symbol, sig.rule, sig.side)
        self.last_run = datetime.now(KST).isoformat(timespec="seconds")
        if found:
            self.last_signals = (found + self.last_signals)[:50]
        return found

    async def loop(self, interval_sec: int = 60) -> None:
        while True:
            try:
                now = datetime.now(KST)
                if (
                    settings.KIWOOM_APP_KEY
                    and now.weekday() < 5
                    and "09:00" <= now.strftime("%H:%M") <= "15:30"
                ):
                    await self.run_once()
            except Exception:  # noqa: BLE001
                log.exception("엔진 루프 오류")
            await asyncio.sleep(interval_sec)
