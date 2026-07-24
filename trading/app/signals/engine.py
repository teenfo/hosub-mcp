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
        self.guard: dict = {}          # 일일 목표·손실 가드 상태 (대시보드 노출)

    def day_guard_status(self) -> dict:
        """당일 실현손익 + 목표/한도 대비 신규 진입 중단 여부."""
        from ..trade import ledger

        today = ledger.realized_today(self.equity)
        target = float(settings.RISK.get("daily_target_pct", 0) or 0)
        loss = float(settings.RISK.get("daily_loss_limit_pct", 0) or 0)
        halted, why = risk.day_guard(today["pct"], target, loss)
        return {**today, "daily_target_pct": target, "daily_loss_limit_pct": loss,
                "equity": self.equity, "halted": halted, "reason": why}

    def _today_df(self, symbol: str):
        df = store.load_bars(symbol, "1m", limit=800)
        if df.empty:
            return df, None
        today = datetime.now(KST).date()
        today_df = df[df.index.date == today]
        prev = df[df.index.date < today]
        prev_close = float(prev["close"].iloc[-1]) if not prev.empty else None
        return today_df, prev_close

    def _daily_downtrend(self, symbol: str) -> bool | None:
        """일봉 추세가 하락인가 — 딥리서치 1단계 게이트용.
        장기선(120/60일) 하회 또는 이평 역배열(5<20<60)이면 하락으로 본다.
        일봉이 부족하면 None(판단 보류 → 게이트 통과)."""
        d = store.load_bars(symbol, "1d", limit=250)
        if len(d) < 60:
            return None
        c = d["close"]
        ma5, ma20, ma60 = (c.rolling(n).mean().iloc[-1] for n in (5, 20, 60))
        ma_long = c.rolling(120).mean().iloc[-1] if len(d) >= 120 else ma60
        price = float(c.iloc[-1])
        below_long = ma_long == ma_long and price < ma_long  # NaN 아님 확인
        aligned_down = ma5 < ma20 < ma60
        return bool(below_long or aligned_down)

    def _rules_for(self, symbol: str) -> dict:
        """추세 게이트가 켜진 하락 규칙에 일봉 추세 플래그를 주입한 규칙 설정."""
        rules_cfg = settings.RULES
        needs = any(
            rules_cfg.get(k, {}).get("require_downtrend")
            for k in ("bounce_fade", "breakdown_retest")
        )
        if not needs:
            return rules_cfg
        dt = self._daily_downtrend(symbol)
        if dt is None:
            return rules_cfg  # 판단 보류 → 원본대로(게이트 통과)
        merged = dict(rules_cfg)
        for k in ("bounce_fade", "breakdown_retest"):
            if k in merged:
                merged[k] = {**merged[k], "_daily_downtrend": dt}
        return merged

    async def run_once(self) -> list[dict]:
        found: list[dict] = []
        day = datetime.now(KST).date().isoformat()
        # 일일 목표·손실 가드: 목표 도달(이익 확정) 또는 손실 한도 도달 시 신규 진입 중단
        self.guard = self.day_guard_status()
        if self.guard["halted"]:
            self.last_run = datetime.now(KST).isoformat(timespec="seconds")
            log.info("일일 가드 작동: %s (오늘 %+.2f%%)",
                     self.guard["reason"], self.guard["pct"])
            return found
        for symbol, name in settings.WATCHLIST.items():
            await collector.backfill_minutes(symbol)
            df, prev_close = self._today_df(symbol)
            if df.empty:
                continue
            for sig in rules.evaluate_all(df, self._rules_for(symbol), prev_close):
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
                     "qty": qty,
                     "ts": datetime.now(KST).isoformat(timespec="seconds")}
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
