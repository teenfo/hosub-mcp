"""신호 평가 루프. 1분 주기로 감시 종목의 오늘 분봉을 평가해 승인 큐에 올린다."""
import asyncio
import logging
import time
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
        self._fired_restored = False   # 재시작 후 오늘 발사분 복원 여부
        self.last_run: str = ""
        self.last_signals: list[dict] = []
        self.guard: dict = {}          # 일일 목표·손실 가드 상태 (대시보드 노출)
        self.equity_synced = False     # 실계좌 잔고로 자산을 동기화했는가
        self._equity_synced_at = 0.0

    def _restore_fired(self) -> None:
        """재시작·재배포 후 '오늘 이미 발사한 신호'를 주문 이력에서 복원한다.
        _fired 는 인메모리라 재시작하면 비어, 같은 신호가 다시 승인대기로
        올라와 중복 발주될 수 있다. 오늘(KST) 생성된 진입 주문의 (symbol, rule)
        을 dedup 집합에 재적용해 재시작을 안전하게 만든다."""
        from ..trade import orders

        today = datetime.now(KST).date()
        restored = 0
        for o in orders.list_orders(limit=300):
            if o.get("kind") not in (None, "entry"):
                continue  # 청산(exit) 주문은 신호 dedup 대상이 아니다
            created = o.get("created")
            symbol, rule = o.get("symbol"), o.get("rule")
            if not (created and symbol and rule):
                continue
            try:
                d = datetime.fromisoformat(created).astimezone(KST).date()
            except ValueError:
                continue
            if d == today:
                self._fired.add((today.isoformat(), symbol, rule))
                restored += 1
        if restored:
            log.info("재시작 복원: 오늘 발사 신호 %d건 dedup 재적용(중복 발주 방지)",
                     restored)

    async def _sync_equity(self) -> None:
        """포지션 사이징 전에 실계좌 예탁자산으로 self.equity 를 맞춘다.
        대시보드 호출에 의존하지 않고 엔진이 직접 조회한다(5분 스로틀).
        동기화 실패 시 equity_synced=False 로 남겨 신규 사이징을 보류시킨다."""
        now = time.monotonic()
        if self.equity_synced and now - self._equity_synced_at < 300:
            return
        if not settings.KIWOOM_APP_KEY:
            return
        try:
            from ..kiwoom.account import parse_balance
            from ..kiwoom.client import client

            data = parse_balance(await client.balance())
        except Exception:  # noqa: BLE001
            log.warning("잔고 동기화 실패 — 이전 자산값 유지")
            return
        if data.get("ok"):
            eq = data.get("deposit_est") or data.get("total_eval") or 0
            if eq > 0:
                self.equity = self.state.equity = float(eq)
                self.equity_synced = True
                self._equity_synced_at = now

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
        # 수집 로스터 갱신 — 현재 감시목록 종목의 '마지막 감시 시각'을 기록해
        # 나중에 목록에서 빠져도 유예기간 동안 백필을 이어가게 한다.
        from ..data import roster
        roster.touch(settings.WATCHLIST)
        # 재시작 후 첫 사이클: 오늘 이미 발사한 신호를 복원해 중복 발주 방지
        if not self._fired_restored:
            self._restore_fired()
            self._fired_restored = True
        # 실계좌 자산 동기화 — 포지션 사이징이 가짜 기본값(1천만원)으로 계산되는 것 방지
        await self._sync_equity()
        if not self.equity_synced:
            self.last_run = datetime.now(KST).isoformat(timespec="seconds")
            log.warning("실계좌 자산 미확인 — 신규 신호 보류(포지션 사이징 불가)")
            return found
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
                sig.symbol = symbol
                # 감시목록 신호는 '금액 제한 없이' 산출한다 — 계좌가 못 사는 종목
                # (예: 고가주)도 최근 신호에 기록해 감사·검증에 쓴다.
                qty = risk.position_size(
                    self.equity, settings.RISK.get("risk_per_trade_pct", 0.5),
                    sig.entry, sig.stop,
                )
                rec = {"symbol": symbol, "name": name, "rule": sig.rule,
                       "side": sig.side, "reason": sig.reason,
                       "entry": sig.entry, "stop": sig.stop,
                       "target": sig.target, "qty": qty, "actionable": False,
                       "ts": datetime.now(KST).isoformat(timespec="seconds")}
                # 승인대기 주문은 '계좌 잔고를 참고'해 실제 매수 가능할 때만 만든다.
                # qty 는 position_size 가 floor(예탁자산/진입가)로 이미 잔고를 반영한다.
                # 롱 전용 모드: 현물 계좌는 개별주 공매도가 불가하므로 숏 신호는
                # (감사용으로 기록만 하고) 발주하지 않는다 — 규칙 종류와 무관하게 차단.
                if settings.RISK.get("long_only", False) and sig.side != "long":
                    rec["note"] = "롱 전용 모드 — 숏 미발주(현물 계좌 개별주 공매도 불가)"
                elif qty < 1:
                    rec["note"] = (f"잔고 부족 — 1주 ≈ {int(sig.entry):,}원 / "
                                   f"자산 {int(self.equity):,}원 (승인대기 미생성)")
                else:
                    ok, why = self.state.can_open()
                    if ok:
                        rec["order_id"] = orders.propose(sig, qty)
                        rec["actionable"] = True
                    else:
                        rec["note"] = f"진입 보류 — {why}"
                        log.info("신호 차단 %s %s: %s", symbol, sig.rule, why)
                self._fired.add(key)
                found.append(rec)
                log.info("신호 등록 %s(%s) %s %s qty=%d%s", name, symbol,
                         sig.rule, sig.side, qty,
                         "" if rec["actionable"] else " · 승인대기 미생성")
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

    async def collect_roster_once(self) -> int:
        """감시목록에서 이탈했지만 유예기간 내인 종목의 분봉을 백필한다.
        현재 감시목록은 run_once 가 매 사이클 백필하므로 여기선 '이탈 종목'만
        처리해 API 호출을 아낀다. 반환: 백필 시도한 이탈 종목 수."""
        from ..data import roster

        cfg = settings.CONFIG.get("collection", {})
        days = int(cfg.get("roster_retention_days", 30))
        roster.touch(settings.WATCHLIST)
        roster.prune(days)
        dropped = [c for c in roster.active(days) if c not in settings.WATCHLIST]
        cap = int(cfg.get("roster_backfill_max", 200))
        if len(dropped) > cap:
            log.warning("로스터 이탈 종목 %d개 > 상한 %d — 최신순 %d개만 수집",
                        len(dropped), cap, cap)
            dropped = dropped[:cap]
        for code in dropped:
            try:
                await collector.backfill_minutes(code)
            except Exception:  # noqa: BLE001 - 개별 실패는 다음 주기에 재시도
                log.warning("로스터 백필 실패 %s", code)
        return len(dropped)

    async def roster_loop(self, interval_sec: int = 900) -> None:
        """이탈 종목 수집 루프(느린 주기, 기본 15분). 장중에만 분봉이 갱신되므로
        장 시간에만 돈다. run_once(60초)와 분리해 레이트리밋을 아낀다."""
        if not settings.CONFIG.get("collection", {}).get("roster_enabled", True):
            return
        while True:
            try:
                now = datetime.now(KST)
                if (
                    settings.KIWOOM_APP_KEY
                    and now.weekday() < 5
                    and "09:00" <= now.strftime("%H:%M") <= "15:40"
                ):
                    cnt = await self.collect_roster_once()
                    if cnt:
                        log.info("로스터 수집: 감시목록 이탈 %d종목 백필", cnt)
            except Exception:  # noqa: BLE001
                log.exception("로스터 루프 오류")
            await asyncio.sleep(interval_sec)
