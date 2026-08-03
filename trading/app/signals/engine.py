"""신호 평가 루프. 1분 주기로 감시 종목의 오늘 분봉을 평가해 승인 큐에 올린다."""
import asyncio
import json
import logging
import statistics
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import settings
from ..data import collector, store
from ..notify import slack
from ..trade import orders, risk
from . import priority, rules

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
_REGIME_ORDER = {"약세": -1, "중립": 0, "강세": 1}
_ORDER_REGIME = {-1: "약세", 0: "중립", 1: "강세"}
# 야간 리포트(미국장 분석 등)가 남기는 익일 시장 편향 파일
NIGHT_BIAS_FILE = Path(settings.DATA_DIR) / "night_bias.json"


class SignalEngine:
    def __init__(self, equity: float = 10_000_000) -> None:
        self.equity = equity
        self.state = risk.DailyRiskState(
            equity=equity,
            daily_loss_limit_pct=settings.RISK.get("daily_loss_limit_pct", 2.0),
            max_positions=settings.RISK.get("max_positions", 3),
        )
        # (day, symbol, rule) → 발주 여부. True=이미 발주(재발주 금지),
        # False=기록만 됨(차단 해제 시 재평가해 발주 가능)
        self._fired: dict[tuple[str, str, str], bool] = {}
        self._fired_restored = False   # 재시작 후 오늘 발사분 복원 여부
        self.last_run: str = ""
        self.last_signals: list[dict] = []
        # 평가손익 가드가 쓸 가격원. main 이 기동 시 실시간 스냅샷 우선 함수를
        # 꽂아 준다(`main._price_of`). 안 꽂혀도 최근 분봉 종가로 동작하므로
        # 가드가 침묵하지는 않는다 — 신선도만 떨어진다.
        self.price_of = None
        self.guard: dict = {}          # 일일 목표·손실 가드 상태 (대시보드 노출)
        self.equity_synced = False     # 실계좌 잔고로 자산을 동기화했는가
        self._equity_synced_at = 0.0
        self.base_regime = "중립"      # 전일 breadth 기반 국면(야간 발굴)
        self._base_regime_at = 0.0
        self.gap_bias = "중립"         # 당일 시가 갭 방향
        self.night_bias = "중립"       # 야간 리포트(미국장 등) 익일 편향
        self.regime = "중립"           # 유효 국면 = base + 시가갭 + 야간리포트 결합
        # 분봉 백필 라운드로빈 — 한 사이클에 감시목록 전체를 부르지 않는다
        self._backfill_cursor = 0
        self._backfill_held: set[str] = set()

    def _base_regime(self) -> str:
        """야간 발굴이 계산한 전일 breadth 국면(강세/약세/중립). 10분 캐시."""
        now = time.monotonic()
        if now - self._base_regime_at < 600 and self._base_regime_at:
            return self.base_regime
        try:
            from .. import export
            m = (export.latest_manifest() or {}).get("market", {})
            self.base_regime = m.get("regime") or "중립"
        except Exception:  # noqa: BLE001
            self.base_regime = self.base_regime or "중립"
        self._base_regime_at = now
        return self.base_regime

    def _open_gap_bias(self) -> str:
        """당일 '시가 갭 방향' — 감시목록 종목들의 시가 갭 중앙값(전일 종가 대비).
        갭하락(음수) 다수면 약세, 갭상승 다수면 강세. 표본 부족하면 중립."""
        gate = settings.CONFIG.get("regime_gate", {})
        if not gate.get("use_open_gap", True):
            return "중립"
        gaps = []
        for sym in list(settings.WATCHLIST):
            df, prev = self._today_df(sym)
            if prev and df is not None and not df.empty:
                gaps.append((float(df["open"].iloc[0]) - prev) / prev * 100)
        if len(gaps) < 5:
            return "중립"
        med = statistics.median(gaps)
        th = float(gate.get("open_gap_th", 0.5))
        return "약세" if med <= -th else ("강세" if med >= th else "중립")

    def _read_night_bias(self) -> str:
        """야간 리포트가 남긴 익일 시장 편향(오늘 날짜일 때만 반영)."""
        gate = settings.CONFIG.get("regime_gate", {})
        if not gate.get("use_night_bias", True):
            return "중립"
        try:
            d = json.loads(NIGHT_BIAS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "중립"
        if d.get("date") != datetime.now(KST).date().isoformat():
            return "중립"     # 오래된 편향은 무시
        r = d.get("regime")
        return r if r in _REGIME_ORDER else "중립"

    def _effective_regime(self) -> str:
        """유효 국면 = 전일 breadth(기준)에 당일 시가갭·야간리포트 편향을 합성.
        야간리포트(미국장 반영)가 있으면 기준을 그 값으로 두고, 시가갭으로 ±1 보정."""
        base = self._base_regime()
        self.night_bias = self._read_night_bias()
        anchor = self.night_bias if self.night_bias != "중립" else base
        self.gap_bias = self._open_gap_bias()
        score = _REGIME_ORDER[anchor] + _REGIME_ORDER[self.gap_bias]
        score = max(-1, min(1, score))
        self.regime = _ORDER_REGIME[score]
        # 네 값을 한 레코드로 남긴다 — 야간 리포트가 결정론 대조군을 이기는지
        # 40거래일 뒤에 물으려면 오늘부터 쌓여 있어야 한다. 값이 바뀔 때만 쓴다.
        from ..data import regime_log

        # self.base_regime 이 아니라 **실제로 합성에 쓰인 값**(base)을 남긴다.
        # 둘은 보통 같지만, 갈라지는 순간 이력이 조용히 거짓이 된다.
        regime_log.record(self.night_bias, base, self.gap_bias, self.regime)
        return self.regime

    def _inverse_blocked(self, symbol: str, side: str = "long") -> bool:
        """국면 게이트: 인버스 ETF 는 강세 유효국면(=지수 상승 예상)에서 매수 보류.
        숏 신호가 인버스 ETF '매수'로 매핑되는 경우(long_only 해제 시)도 동일하게
        게이트한다 — 매핑 우회로 강세장 인버스 매수가 생기는 것 방지."""
        gate = settings.CONFIG.get("regime_gate", {})
        if not gate.get("enabled", True):
            return False
        inverse_set = set(settings.CONFIG.get("inverse_etfs", []))
        is_inverse_buy = symbol in inverse_set or (
            side == "short"
            and not settings.RISK.get("long_only", False)
            and settings.INVERSE_ETF                       # 숏 → 인버스 매수 매핑
        )
        if not is_inverse_buy:
            return False
        return self.regime == gate.get("inverse_block_regime", "강세")

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
                self._fired[(today.isoformat(), symbol, rule)] = True
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
                settings.set_equity(eq)   # 감시목록 편입 tier 판정에 쓰인다
                self.equity_synced = True
                self._equity_synced_at = now

    def _guard_price_of(self, symbol: str):
        from ..trade import ledger
        if self.price_of is not None:
            return self.price_of(symbol)
        return ledger.latest_price(symbol)

    def day_guard_status(self) -> dict:
        """당일 실현손익 + 목표/한도 대비 신규 진입 중단 여부.

        임시 해제(override)가 살아 있으면 **손실 한도만** 늘려 잡는다. 실현손익
        자체는 손대지 않으므로 화면·일지의 숫자는 진실 그대로다 — 바뀌는 것은
        '어느 선에서 멈추는가' 뿐이다. (이익 목표는 성격이 달라 대상 아님)
        """
        from ..trade import ledger, override

        self._effective_regime()   # 대시보드 표시용으로 유효 국면 최신화
        today = ledger.realized_today(self.equity)
        target = float(settings.RISK.get("daily_target_pct", 0) or 0)
        loss = float(settings.RISK.get("daily_loss_limit_pct", 0) or 0)
        ov = override.state()
        loss_eff = round(loss + (ov["extra_loss_pct"] if ov["active"] else 0.0), 4)
        # 평가손익은 가드가 보지 못하던 절반이다. 가격을 못 얻은 포지션이 있으면
        # 그만큼 손실을 과소계상하므로 `priced` 를 함께 노출한다.
        incl = bool(settings.RISK.get("guard_include_unrealized", True))
        open_pnl = ledger.open_pnl(self._guard_price_of, self.equity)
        halted, why = risk.day_guard(today["pct"], target, loss_eff,
                                     open_pnl["pct"], incl)
        if halted and ov["active"] and "손실" in why:
            why += f" (임시 허용 +{ov['extra_loss_pct']:g}% 반영 후에도 초과)"
        flat_at = float(settings.RISK.get("daily_loss_flatten_pct", 0) or 0)
        flatten, flat_why = risk.flatten_call(today["pct"], open_pnl["pct"], flat_at)
        return {**today, "daily_target_pct": target, "daily_loss_limit_pct": loss,
                "loss_limit_effective_pct": loss_eff, "override": ov,
                "unrealized": open_pnl, "guard_include_unrealized": incl,
                "daily_loss_flatten_pct": flat_at,
                "flatten": flatten, "flatten_reason": flat_why,
                "equity": self.equity, "halted": halted, "reason": why,
                "regime": self.regime, "base_regime": self.base_regime,
                "gap_bias": self.gap_bias, "night_bias": self.night_bias}

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
        """심볼별 규칙 설정 구성: ① 규칙 config 의 regimes(활성 국면 목록)와 현재
        유효 국면이 안 맞으면 그 규칙을 자동 비활성(국면 연동 활성) ② 추세 게이트가
        켜진 하락 규칙에 일봉 추세 플래그를 주입."""
        rules_cfg = settings.RULES
        merged = None

        # ① 국면 연동: regimes 가 지정된 규칙은 현재 유효 국면이 목록에 있을 때만 켠다.
        #    (예: momentum 에 regimes:[강세,중립] → 약세장에선 자동 OFF)
        for name, cfg in rules_cfg.items():
            if not isinstance(cfg, dict):
                continue
            regimes = cfg.get("regimes")
            if regimes and cfg.get("enabled") and self.regime not in regimes:
                if merged is None:
                    merged = dict(rules_cfg)
                merged[name] = {**cfg, "enabled": False,
                                "_regime_blocked": self.regime}
        base = merged if merged is not None else rules_cfg

        # ② 일봉 하락 추세 게이트 주입 (숏 규칙용)
        needs = any(
            base.get(k, {}).get("require_downtrend") and base.get(k, {}).get("enabled")
            for k in ("bounce_fade", "breakdown_retest")
        )
        if needs:
            dt = self._daily_downtrend(symbol)
            if dt is not None:      # None 이면 판단 보류 → 원본대로(게이트 통과)
                base = dict(base)
                for k in ("bounce_fade", "breakdown_retest"):
                    if k in base:
                        base[k] = {**base[k], "_daily_downtrend": dt}

        # ③ 돌파 확인(UI 설정) 주입 — risk.json 오버라이드가 config.yaml 기본값을 이긴다.
        override = settings.RISK.get("confirm_on_close")
        if override is not None and bool(override) != bool(
                base.get("confirm_on_close", False)):
            base = dict(base)
            base["confirm_on_close"] = bool(override)
        return base

    def max_position_weight_pct(self) -> float:
        """한 종목 최대 비중(%) — 0 이면 미적용. 잘못된 값은 무시하고 기본값."""
        try:
            v = float(settings.RISK.get("max_position_weight_pct", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
        return v if 0 < v <= 100 else 0.0

    @staticmethod
    def _log_signal(day: str, rec: dict) -> None:
        """매매일지용 신호 기록. 주문 테이블에는 '발주된 것'만 남아 자금이 없어
        밀린 신호가 보이지 않는다 — 사후 진단을 위해 미발주분까지 남긴다.
        기록 실패가 매매를 막지 않도록 삼킨다."""
        from .. import journal

        try:
            journal.record_signal(day, rec)
        except Exception:  # noqa: BLE001 - 일지 기록 실패는 비치명적
            log.exception("신호 기록 실패 %s %s", rec.get("symbol"), rec.get("rule"))

    @staticmethod
    def _held_symbols() -> set[str]:
        """보유 중인 종목코드. 장부 조회 실패 시 빈 집합(과차단보다 통과)."""
        from ..trade import ledger

        try:
            return ledger.open_symbols()
        except Exception:  # noqa: BLE001 - 조회 실패로 매매를 막지 않는다
            log.warning("보유 종목 조회 실패 — 중복 진입 차단 이번 사이클 생략")
            return set()

    def _sync_open_positions(self) -> None:
        """열린 포지션 수를 장부에서 읽어 리스크 상태에 반영한다.
        조회 실패는 무시(직전 값 유지) — 한도 판정 때문에 사이클을 멈추지 않는다."""
        from ..trade import ledger

        self.state.max_positions = settings.RISK.get("max_positions", 3)
        try:
            self.state.open_positions = ledger.open_count()
        except Exception:  # noqa: BLE001 - 장부 조회 실패는 비치명적
            log.warning("열린 포지션 수 조회 실패 — 직전 값(%d) 유지",
                        self.state.open_positions)

    def _zero_qty_note(self, sig, risk_pct: float, max_w: float,
                       avail: float) -> str:
        """수량 0 의 원인을 구분해 화면에 보여줄 문구를 만든다.
        (1) 남은 현금 부족 (2) 종목당 비중 상한 (3) 거래당 리스크 한도."""
        affordable = int(avail // sig.entry) if sig.entry > 0 else 0
        if affordable < 1:
            return (f"잔고 부족 — 1주 ≈ {int(sig.entry):,}원 > "
                    f"가용 {int(avail):,}원")
        if max_w and risk.weight_cap_qty(self.equity, sig.entry, max_w) < 1:
            limit = self.equity * max_w / 100
            return (f"비중 상한 — 1주 ≈ {int(sig.entry):,}원이 종목당 상한"
                    f"(계좌의 {max_w:g}% ≈ {int(limit):,}원)을 넘음. "
                    f"상한을 올리면 매매 가능")
        dist = abs(sig.entry - sig.stop)
        budget = self.equity * risk_pct / 100
        stop_pct = (dist / sig.entry * 100) if sig.entry else 0
        return (f"리스크 한도 — 손절폭 {stop_pct:.1f}%(≈{int(dist):,}원)가 "
                f"거래당 {risk_pct:g}% 한도(≈{int(budget):,}원)를 초과해 0주 "
                f"(매수여력은 {affordable}주). 리스크%를 올리면 매매 가능")

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
        # 수집: 장중 실시간 백필. 매매 판단보다 **앞에** 둔다 — 잔고 조회 실패나
        # 일일 가드로 매매가 멈춰도 분봉 축적은 이어져야 한다(차트·백테스트가 쓴다).
        #
        # 수집전용(COLLECT_ONLY)은 기본적으로 제외한다. 매매하지 않으므로 30초마다
        # 최신화할 이유가 없고, 분봉 API 는 1회 호출로 900봉(≈2.3거래일)을 주므로
        # 장 마감 후 한 번만 불러도 그날치가 통째로 들어온다(eod_backfill_once).
        # 아낀 호출은 매매 대상의 감시 주기를 줄이는 데 쓸 수 있다.
        # 보유 종목을 배치 앞에 두기 위해 장부에서 읽는다(DB 읽기, API 0콜).
        try:
            from ..trade import ledger as _ledger
            self._backfill_held = _ledger.open_symbols()
        except Exception:  # noqa: BLE001 - 실패하면 우선순위만 잃는다
            self._backfill_held = set()
        for symbol in self._backfill_batch(self._collect_targets()):
            await collector.backfill_minutes(symbol)
        # 신규 편입 종목의 과거 확보 — 단발 조회는 최근 900봉뿐이라 편입 당일에는
        # 백테스트 최소 일수를 못 채운다. 연속조회로 2주치를 끌어온다(종목당 1회).
        await self.deep_backfill_pending()
        # 실계좌 자산 동기화 — 포지션 사이징이 가짜 기본값(1천만원)으로 계산되는 것 방지
        await self._sync_equity()
        if not self.equity_synced:
            self.last_run = datetime.now(KST).isoformat(timespec="seconds")
            log.warning("실계좌 자산 미확인 — 신규 신호 보류(포지션 사이징 불가)")
            return found
        # 유효 국면 갱신 — 백필 뒤에 계산해야 첫 사이클에도 '오늘 시가갭'이 반영된다.
        self._effective_regime()
        # 일일 목표·손실 가드 — **완전 자동 발주를 위한 안전장치**다.
        # 사람이 개입하지 않고 주문이 나가는 모드에서만 신규 진입을 끊는다.
        # 자동 발주를 끄면 모든 주문이 사용자 승인을 거치므로 가드가 진입을 막지
        # 않고, 대신 신호에 경고를 달아 판단을 사용자에게 넘긴다.
        self.guard = self.day_guard_status()
        auto = bool(settings.RISK.get("auto_approve", False))
        if self.guard["halted"] and auto:
            # 종전에는 여기서 사이클을 통째로 끝내 **신호가 기록조차 안 됐다.**
            # 대기열 무조건 생성(사용자 결정 2026-08-03)에 따라 평가·기록·대기열은
            # 계속하고 자동발주만 멈춘다 — 아래 자동발주 조건이 halted 를
            # 재확인하고, guard_warn 이 화면·일지에 '한도 도달 후 진입' 을 남긴다.
            log.info("일일 가드 작동(자동 발주 정지 — 평가·대기열은 계속): %s "
                     "(오늘 %+.2f%%)", self.guard["reason"], self.guard["pct"])
        guard_warn = self.guard["reason"] if self.guard["halted"] else ""
        if guard_warn:
            log.info("일일 가드 도달했으나 수동 승인 모드 — 신호 평가 계속: %s",
                     guard_warn)
        # 매매: 수집전용(COLLECT_ONLY)을 제외한 트레이딩 대상만 규칙 평가·주문 제안.
        risk_pct = settings.RISK.get("risk_per_trade_pct", 0.5)
        max_w = self.max_position_weight_pct()
        # 동시 포지션 한도의 기준값을 장부에서 동기화한다. open_positions 는 어디서도
        # 갱신되지 않아 항상 0 이었고 — max_positions 가 사실상 무제한이었다.
        self._sync_open_positions()
        # 같은 종목 중복 진입 차단용 — 이미 보유 중인 종목 집합.
        # 규칙이 다르면 각각 진입해 한 종목의 같은 하락을 두 번 맞는다
        # (실측 2026-07-27: 신일전자 momentum + pullback 둘 다 손절).
        held = self._held_symbols() if settings.RISK.get("one_position_per_symbol",
                                                         True) else None

        # --- 1단계: 후보 수집 (아직 발주하지 않는다) ---
        # 발견 즉시 발주하면 감시목록 순회 순서(= 종목코드 순)가 자금 배분을 정해,
        # 뒤에 나온 더 좋은 신호가 '잔고 부족'으로 밀린다. 한 패스에서 나온 신호를
        # 모두 모은 뒤 우선순위대로 발주한다. 지연의 상한은 '이 패스 소요 시간'이지
        # 감시 주기가 아니다(어차피 한 바퀴 도는 동안 발견되는 신호들이다).
        cands: list[dict] = []
        for symbol, name in settings.WATCHLIST.items():
            if symbol in settings.COLLECT_ONLY:
                continue  # 수집전용 — 데이터만 쌓고 신호·주문은 만들지 않는다
            df, prev_close = self._today_df(symbol)
            if df.empty:
                continue
            for sig in rules.evaluate_all(df, self._rules_for(symbol), prev_close):
                sig.symbol = symbol
                if self._fired.get((day, symbol, sig.rule)) is True:
                    continue      # 이미 발주됨 — 재발주 금지(정렬 대상에서도 제외)
                cands.append({"symbol": symbol, "name": name, "rule": sig.rule,
                              "side": sig.side, "reason": sig.reason,
                              "entry": sig.entry, "stop": sig.stop,
                              "target": sig.target, "qty": 0, "actionable": False,
                              "ts": datetime.now(KST).isoformat(timespec="seconds"),
                              "_sig": sig})

        # --- 2단계: 우선순위 정렬 (규칙 기대값 × 손익비 — 결정론) ---
        ranked = priority.order(cands, settings.RULES)
        # 인버스 발주 우선권(사용자 결정 2026-08-03 — 하락장 수익 최우선 전제).
        # 약세 유효국면에서는 인버스 ETF 신호를 같은 사이클의 롱보다 먼저
        # 발주한다 — 실측 8/3: 인버스 신호 2건(누적 유일 흑자 축, 6건 5승)이
        # 포지션 한도 경쟁에서 롱에 밀려 미발주됐다. **측정 근거가 아니라
        # 사용자 결정이다**(breadth 국면 예측력은 소급 194일 기준 미달로
        # measurement.md 에 기록) — 국면 채점(score_first) 적중률로 사후
        # 평가하고, regime_gate.inverse_priority 로 배포 없이 끈다.
        # 정렬은 stable — 인버스/롱 각각의 내부 순위는 유지된다.
        gate_cfg = settings.CONFIG.get("regime_gate", {})
        inv = set(settings.CONFIG.get("inverse_etfs", []))
        inv_regime = bool(inv) and self.regime in tuple(gate_cfg.get(
            "inverse_priority_regimes", ["약세"]))
        if gate_cfg.get("inverse_priority", True) and ranked and inv_regime:
            ranked.sort(key=lambda c: c["symbol"] not in inv)
        # 약세 리스크 프로필(사용자 결정 2026-08-03 후속 — 우선권만으로는 롱
        # 기준 자리·비중에 갇혀 '작게' 이긴다는 지적).
        #  · 예약 슬롯: 롱은 max_positions-reserved 까지만 — 롱이 자리를 전부
        #    점유해 인버스가 못 들어오는 문제는 정렬로 못 푼다. 총 한도는 불변.
        #  · 인버스 비중 상한 별도: 지수 ETF 는 개별주 대비 저변동·손절 관통
        #    위험이 낮고 대상이 2~3종뿐이라 분산 논리가 약하다.
        # 값 0 이면 각각 비활성. 국면 조건은 우선권과 같은 목록을 쓴다.
        reserved = (int(gate_cfg.get("inverse_reserved_slots", 0) or 0)
                    if inv_regime else 0)
        inv_w = (float(gate_cfg.get("inverse_weight_pct", 0) or 0)
                 if inv_regime else 0.0)
        if not 0 < inv_w <= 100:
            inv_w = 0.0
        # 이미 인버스가 예약분만큼 자리를 차지했으면 예약은 소진된 것 — 롱이
        # 남은 자리를 쓴다. 보유 조회(_backfill_held)가 비어 있으면(조회 실패)
        # 예약이 전부 남은 것으로 계산돼 롱이 더 제한된다 — 하락장 수익 최우선
        # 전제에서 보수적인 방향이라 그대로 둔다.
        inverse_open = len(self._backfill_held & inv) if inv_regime else 0
        if len(ranked) > 1:
            log.info("사이클 신호 %d건 발주 순서: %s", len(ranked),
                     " > ".join(f"{c['name']}({c['rule']} {c['priority']:.0f})"
                                for c in ranked))

        # --- 3단계: 우선순위대로 발주 ---
        # 같은 사이클 안에서 앞선 발주가 쓴 금액을 빼고 남은 현금으로 수량을 계산한다
        # (브로커 거부 대신 화면에 '잔고 부족'으로 정직하게 표시).
        avail = float(self.equity)
        for rec in ranked:
            sig = rec.pop("_sig")
            symbol, name = rec["symbol"], rec["name"]
            key = (day, symbol, sig.rule)
            # **수량과 자금 판정을 분리한다.**
            #
            # 종전에는 position_size 가 남은 현금까지 한 번에 잘라, 자금이 모자라면
            # 주문이 아예 만들어지지 않았다. 그래서 발굴엔진이 올린 고가주는
            # 승인대기 목록에 흔적조차 남지 않았다 — 사용자는 신호가 났다는 사실도
            # 모른다. 이제 '리스크·비중 기준 수량' 을 먼저 정하고, 살 수 있는지는
            # 따로 표시해 매매 데스크가 판정한다.
            # 약세 국면의 인버스는 별도 비중 상한(inv_w)을 쓴다 — 일반 상한에
            # 갇히면 주력 수단이 '작게' 이긴다. 그 외에는 종전 상한 그대로.
            is_inv = symbol in inv
            eff_w = inv_w if (is_inv and inv_w > 0) else max_w
            qty = risk.position_size(self.equity, risk_pct, sig.entry, sig.stop,
                                     max_weight_pct=eff_w)
            # 비중 상한이 0주를 내는 경우(1주 값 > 상한인 고가주)에도 **1주로
            # 제안은 만든다** — 보이지 않으면 판단할 수 없다. 다만 그 사실을
            # over_weight 로 남겨 자동승인이 집어가지 못하게 한다.
            over_weight = False
            if qty < 1 and risk.position_size(self.equity, risk_pct,
                                              sig.entry, sig.stop) >= 1:
                qty, over_weight = 1, True
            need = qty * sig.entry
            fundable = qty >= 1 and need <= avail
            rec["fundable"] = fundable
            rec["need_cash"] = int(need)
            if over_weight:
                rec["over_weight"] = True
            rec["qty"] = qty
            actionable = False
            # 진입 시간창 게이트(시간대 분석 2026-08-01) — 신호는 기록, 발주만 차단
            window_note = rules.entry_window_note(
                sig.rule, datetime.now(KST), settings.RULES)
            if window_note:
                rec["note"] = window_note
            # 국면 게이트: 인버스 ETF(직접 또는 숏→인버스 매핑)는 강세장 매수 보류.
            elif self._inverse_blocked(symbol, sig.side):
                rec["note"] = f"국면 게이트 — {self.regime}장이라 인버스 매수 보류"
            # 롱 전용 모드: 현물 계좌는 개별주 공매도가 불가하므로 숏 신호는
            # (감사용으로 기록만 하고) 발주하지 않는다 — 규칙 종류와 무관하게 차단.
            elif settings.RISK.get("long_only", False) and sig.side != "long":
                rec["note"] = "롱 전용 모드 — 숏 미발주(현물 계좌 개별주 공매도 불가)"
            elif held is not None and symbol in held:
                rec["note"] = ("종목 중복 — 이미 보유·진입한 종목입니다"
                               "(한 종목의 같은 움직임에 두 번 걸리지 않도록 차단)")
            else:
                # **계좌 상태(수량 0·포지션 한도·손실 가드)는 대기열 생성을 막지
                # 않는다**(사용자 결정 2026-08-03: "계좌가 발주 불가능한 상태여도
                # 승인 대기열에는 무조건 올려야 함"). 종전에는 오늘 하루에만
                # 포지션 한도 16건이 기록만 남고 대기열에 흔적이 없었다 — 사용자는
                # 자리를 정리하면 살 수 있었다는 사실 자체를 몰랐다.
                #
                # 위의 전략 게이트(시간창·국면·롱온리·중복)는 종전대로 미생성이다
                # — 그건 계좌 상태가 아니라 '지금은 사지 않는다'는 결정 자체다.
                #
                # manual_only 는 자동발주가 집어가지 못하게 하는 플래그다.
                # approve_and_send 는 한도를 재검사하지 않으므로(사람 결정 우선)
                # 사용자가 자리를 정리하고 승인하면 그대로 발주된다.
                actionable = True
                if qty < 1:
                    qty = 1
                    rec["qty"] = qty
                    rec["need_cash"] = int(sig.entry)
                    rec["fundable"] = fundable = sig.entry <= avail
                    rec["manual_only"] = True
                    rec["note"] = ("리스크 기준 수량 0주 — 1주로 제안. "
                                   + self._zero_qty_note(sig, risk_pct, eff_w, avail))
                # 약세 국면 인버스 예약 슬롯 — 롱 후보만 예약 잔여분을 뺀
                # 한도로 판정한다. 인버스가 이미 예약분만큼 열려 있으면 잔여 0.
                reserve = 0 if is_inv else max(0, reserved - inverse_open)
                ok, why = self.state.can_open(reserve)
                if not ok:
                    rec["manual_only"] = True
                    rec["note"] = f"{why} — 자동발주 제외, 수동 승인만"
                    log.info("신호 수동 대기 %s %s: %s", symbol, sig.rule, why)
            # 중복 처리: 비발주 상태가 그대로면 기록도 반복하지 않는다. 단 '차단→해제'
            # (입금·포지션 정리·국면 전환 등)로 발주 가능해진 신호는 다시 살아난다.
            # (이미 발주된 신호 prev is True 는 1단계에서 걸러졌다.)
            prev = self._fired.get(key)
            if prev is not None and not actionable:
                continue
            if actionable:
                rec["order_id"] = orders.propose(sig, qty)
                rec["actionable"] = True
                # 승인대기 주문이 생기는 **바로 이 지점**에서 알린다. 자금이
                # 모자란 건도 보낸다 — 사용자가 입금하거나 다른 포지션을 정리해
                # 살 수 있게 만드는 판단을 하려면 알아야 한다.
                await slack.notify(slack.pending_order_text(rec))
                if guard_warn:
                    # 가드 도달 상태에서 만든 승인대기 주문 — 화면·일지에 표시해
                    # 사용자가 '오늘 한도를 넘긴 뒤의 진입'임을 알고 승인하게 한다.
                    rec["guard_warn"] = guard_warn
                if fundable and not rec.get("manual_only"):
                    avail = max(0.0, avail - need)   # 남은 현금에서 차감
                    # 같은 사이클에서 한도를 넘어 발주하지 않도록 즉시 반영(보수적
                    # 계산 — 승인 대기 상태여도 자리를 점유한 것으로 본다). 다음
                    # 사이클에 _sync_open_positions 가 장부 실제값으로 다시 맞춘다.
                    self.state.open_positions += 1
                    if is_inv:
                        # 같은 사이클에서 인버스가 자리를 잡으면 예약 소진으로
                        # 반영 — 뒤에 오는 롱이 남은 자리를 쓸 수 있다.
                        inverse_open += 1
                    if held is not None:
                        held.add(symbol)
                elif not fundable and not rec.get("note"):
                    # 자금이 모자란 제안은 자리를 점유하지 않는다. 점유시키면
                    # 살 수도 없는 주문이 뒤에 오는 살 수 있는 신호를 막는다.
                    # (manual_only 사유가 이미 있으면 덮지 않는다 — 한도·수량 0
                    # 사유가 자금 문구에 지워지면 사용자가 원인을 잘못 읽는다.)
                    rec["note"] = (f"자금 부족 — 필요 {need:,.0f}원 / 가용 "
                                   f"{avail:,.0f}원. 승인대기에는 남습니다")
                if over_weight:
                    rec["note"] = (f"종목당 비중 상한 초과 — 1주 {sig.entry:,.0f}원. "
                                   "자동승인 대상이 아닙니다(수동 승인만)")
                # 완전 자동 발주: 승인 없이 즉시 발주(소액 계좌 + 안전장치 전제).
                # **자금이 되고 비중 상한을 넘지 않는 것만** 집어간다 — 데스크의
                # 스크리닝이 여기다. 나머지는 대기열에 남아 사람이 판단한다.
                if (settings.RISK.get("auto_approve") and fundable
                        and not over_weight and not rec.get("manual_only")
                        and not self.guard["halted"]):
                    try:
                        res = await orders.approve_and_send(rec["order_id"])
                        rec["auto_status"] = res.get("status")
                        rec["auto_message"] = res.get("message")
                        log.info("자동 발주 %s(%s) %s → %s: %s", name, symbol,
                                 sig.rule, res.get("status"), res.get("message"))
                    except Exception:  # noqa: BLE001 - 자동발주 실패는 대기열에 남는다
                        log.exception("자동 발주 실패 %s %s", symbol, sig.rule)
                        rec["auto_status"] = "error"
            self._fired[key] = actionable
            found.append(rec)
            self._log_signal(day, rec)
            log.info("신호 등록 %s(%s) %s %s qty=%d 우선순위=%.0f%s", name, symbol,
                     sig.rule, sig.side, qty, rec.get("priority", 0),
                     "" if rec["actionable"] else " · 승인대기 미생성")
        self.last_run = datetime.now(KST).isoformat(timespec="seconds")
        if found:
            self.last_signals = (found + self.last_signals)[:50]
        return found

    def market_status(self) -> dict:
        """지금 감시(신호 스캔)가 도는 상태인지 — 매매 데스크 표시용.

        엔진 루프와 같은 조건(평일 09:00-15:30 + 키 설정)을 그대로 계산한다.
        phase: closed(장 마감·주말) | pre(개장 전) | open(감시 중)
             | halted(자동 발주 모드에서 일일 가드가 신규 진입을 막는 중)
             | disabled(키 없음)
        """
        now = datetime.now(KST)
        hhmm = now.strftime("%H:%M")
        weekday = now.weekday() < 5
        if not settings.KIWOOM_APP_KEY:
            phase, label = "disabled", "API 키 미설정 — 감시 중지"
        elif not weekday:
            phase, label = "closed", "휴장(주말)"
        elif hhmm < "09:00":
            phase, label = "pre", "개장 전 대기"
        elif hhmm <= "15:30":
            phase, label = "open", "장중 감시 중"
        else:
            phase, label = "closed", "장 마감"
        # 일일 가드 — 직전 사이클이 계산한 값(조회 비용 없이 재사용).
        # 자동 발주 모드에서만 신규 진입을 막으므로, '막고 있는지'까지 구분해 알린다.
        guard = self.guard or {}
        halted = bool(guard.get("halted"))
        auto = bool(settings.RISK.get("auto_approve", False))
        blocking = halted and auto and phase == "open"
        if blocking:
            # 사유 문구가 이미 '… 도달 — 신규 진입 중단' 형태라 접두를 덧붙이지 않는다.
            phase = "halted"
            label = guard.get("reason") or "일일 가드 도달 — 신규 진입 중단"
        # 마지막 스캔 이후 경과(초) — 루프가 실제로 돌고 있는지의 근거
        age = None
        if self.last_run:
            try:
                age = int((now - datetime.fromisoformat(self.last_run)).total_seconds())
            except ValueError:
                age = None
        interval = self.scan_interval()
        return {
            "phase": phase,
            "label": label,
            # scanning = 신호를 평가·발주하는가 / collecting = 루프가 돌며 분봉을
            # 모으는가. 가드가 걸리면 앞은 멈추고 뒤는 계속된다 — 구분해서 알린다.
            "scanning": phase == "open",
            "collecting": phase in ("open", "halted"),
            "entries_blocked": blocking,
            "guard": {
                "halted": halted,
                "reason": guard.get("reason", ""),
                "pct": guard.get("pct"),
                "auto_approve": auto,
                # 가드 도달 + 수동 승인 = 사용자 책임으로 매매 계속
                "manual_override": halted and not auto,
            },
            "scan_interval_sec": interval,
            "last_scan_age_sec": age,
            # 다음 스캔까지 남은 초 — 화면 카운트다운의 기준점(브라우저가 1초씩 감산).
            # 스캔이 이미 지연됐으면 0(= 진행 중/지연).
            "next_scan_sec": None if age is None else max(0, interval - age),
            "watch_count": len(settings.WATCHLIST) - len(settings.COLLECT_ONLY),
            "session": "09:00~15:30",
        }

    def scan_interval(self) -> int:
        """감시 주기(초) — UI 에서 바꾸면 다음 사이클부터 즉시 적용(재시작 불필요)."""
        try:
            n = int(settings.RISK.get("scan_interval_sec", 60))
        except (TypeError, ValueError):
            return 60
        return max(settings.SCAN_INTERVAL_MIN_SEC,
                   min(settings.SCAN_INTERVAL_MAX_SEC, n))

    async def loop(self, interval_sec: int | None = None) -> None:
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
            await asyncio.sleep(interval_sec or self.scan_interval())

    def _backfill_batch(self, targets: list[str]) -> list[str]:
        """이번 사이클에 분봉을 갱신할 종목 — **한 번에 다 부르지 않는다.**

        사용자 결정(2026-07-30): 모든 주기의 우선순위는 매매 데스크다. 데스크가
        2초를 지키려면 같은 예산을 쓰는 다른 호출을 줄여야 하고, 줄이는 방식은
        **동시 조회 종목 수를 낮추는 것**이다.

        실측 배경: 매매 tier 34종목 × 30초 사이클 = chart 68콜/분. 로스터·심층
        백필이 겹쳐 149콜/분까지 올라 API 사용률이 90%가 됐고, 그 큐 뒤에서
        데스크가 밀렸다.

        라운드로빈이라 어떤 종목도 굶지 않는다 — n종목·배치 k면 각 종목이
        ceil(n/k) 사이클마다 갱신된다. 보유 종목은 **항상 앞에 둔다**: 청산
        판정이 걸린 종목의 분봉이 낡는 것은 다른 종목이 낡는 것과 무게가 다르다.

        `backfill_per_cycle: 0` 이면 종전처럼 전부 부른다(되돌리기).
        """
        cap = int(settings.CONFIG.get("collection", {}).get("backfill_per_cycle", 0))
        if cap <= 0 or len(targets) <= cap:
            return targets
        held = self._backfill_held or set()
        first = [s for s in targets if s in held]
        rest = [s for s in targets if s not in held]
        room = max(0, cap - len(first))
        if not rest or room == 0:
            return first[:cap] if first else []
        start = self._backfill_cursor % len(rest)
        picked = (rest + rest)[start:start + room]
        self._backfill_cursor = (start + room) % len(rest)
        return first + picked

    @staticmethod
    def _collect_targets() -> list[str]:
        """장중 실시간 백필 대상. 기본은 매매 대상만(수집전용 제외).
        collection.realtime_trading_only=false 면 종전대로 감시목록 전체."""
        cfg = settings.CONFIG.get("collection", {})
        if not cfg.get("realtime_trading_only", True):
            return list(settings.WATCHLIST)
        return [s for s in settings.WATCHLIST if s not in settings.COLLECT_ONLY]

    async def deep_backfill_pending(self, limit: int = 0) -> int:
        """아직 과거를 확보하지 못한 감시 종목을 연속조회로 채운다(종목당 1회).

        신규 편입 종목은 단발 조회로 최근 900봉(≈2.4거래일)밖에 못 받는다.
        분봉 API 는 언제 부르든 '최근' 900봉이라 마감 후로 미뤄도 과거가 늘지
        않으므로, 편입 직후 연속조회로 끌어와 첫날부터 평가 대상이 되게 한다.

        수집전용(COLLECT_ONLY)도 포함한다 — 매매는 안 해도 백테스트가 쓴다.
        비용은 종목당 pages 콜 × 1회뿐이라 실시간 제외 대상이 아니다.
        사이클당 처리 종목 수를 제한해 레이트리밋을 지킨다(최초 배포 시 감시목록
        전체가 대상이 되므로 한 번에 몰아 부르지 않는다).
        반환: 이번에 처리한 종목 수.
        """
        cfg = settings.CONFIG.get("collection", {})
        if not cfg.get("deep_backfill", True):
            return 0
        pages = int(cfg.get("deep_backfill_pages", 5))
        cap = int(limit or cfg.get("deep_backfill_per_cycle", 3))
        done = store.deep_backfilled()
        pending = [s for s in settings.WATCHLIST if s not in done]
        n = 0
        for symbol in pending[:cap]:
            try:
                bars = await collector.deep_backfill(symbol, pages)
            except Exception:  # noqa: BLE001 - 실패는 기록하지 않고 다음 주기 재시도
                log.warning("심층 백필 실패 %s — 다음 주기 재시도", symbol)
                continue
            store.mark_deep_backfill(symbol, bars)
            n += 1
        if n:
            log.info("심층 백필 %d종목 (%d페이지), 남은 대상 %d",
                     n, pages, max(0, len(pending) - n))
        return n

    async def eod_backfill_once(self) -> int:
        """장 마감 후 감시목록 **전체**를 1회 백필해 그날 분봉을 확정한다.

        두 가지를 한 번에 메운다.
        1. 수집전용 종목 — 장중에 실시간 백필을 하지 않으므로 여기서 하루치를 받는다.
        2. 매매 대상의 마지막 구간 — 장중 루프는 15:30 에 멈추므로 그 이후
           체결(종가 동시호가 등)이 저장에 빠질 수 있다.
        1회 호출이 900봉(≈2.3거래일)을 주므로 하루를 통째로 덮어쓴다.
        반환: 백필한 종목 수.
        """
        # 대상은 현재 감시목록 **+ 유예기간 내 이탈 종목**이다. 감시목록만 훑으면
        # 15:30 직전에 순위에서 밀려 빠진 종목의 그날 분봉이 영영 확정되지 않아
        # 백테스트 표본에 구멍이 생긴다. 로스터 루프는 15분 주기라 마지막 구간을
        # 놓칠 수 있으므로 여기서 함께 메운다.
        from ..data import roster

        cfg0 = settings.CONFIG.get("collection", {})
        targets = dict.fromkeys(settings.WATCHLIST)
        try:
            days = int(cfg0.get("roster_retention_days", 30))
            targets.update(dict.fromkeys(roster.active(days)))
        except Exception:  # noqa: BLE001 - 로스터 실패가 마감 확정을 막지 않는다
            log.warning("로스터 조회 실패 — 감시목록만 마감 백필")
        # 유동성 유니버스까지 넓힌다 — 지금 1분봉이 있는 종목은 **395개**뿐이고
        # (일봉은 3,941), 장중 차트 지표는 그 395개에만 계산할 수 있다.
        # "전체 종목 베이스 + 장중 지표" 가 양립하지 않는 이유가 이것이었다.
        #
        # 406 은 기술적 상한이 아니라 **대상 집합**이었다. 마감 백필이 감시목록
        # + 로스터만 훑었을 뿐이다. ka10080 은 1콜에 900봉(≈2.4거래일)을 주므로
        # 종목당 1콜이면 하루가 통째로 덮인다 — 4 rps 에서 1,000종목이 약 4분,
        # 그리고 15:35 부터 야간 발굴(17:30)까지 예산은 통째로 비어 있다.
        univ = int(cfg0.get("eod_universe_max", 0))
        if univ > 0:
            try:
                from ..scout.nightly import liquid_universe
                before = len(targets)
                targets.update(dict.fromkeys(liquid_universe(univ)))
                log.info("마감 백필 대상 확장: %d → %d종목", before, len(targets))
            except Exception:  # noqa: BLE001 - 확장 실패가 기존 확정을 막지 않는다
                log.warning("유동성 유니버스 조회 실패 — 감시목록·로스터만 백필")
        n = 0
        for symbol in targets:
            try:
                await collector.backfill_minutes(symbol)
                n += 1
            except Exception:  # noqa: BLE001 - 개별 실패는 나머지를 막지 않는다
                log.exception("마감 백필 실패 %s", symbol)
        log.info("마감 백필 완료: %d종목 (수집전용 포함)", n)
        # 장중에 실패했거나 사이클 상한에 밀린 신규 편입분을 여기서 회수한다.
        # 장이 끝나 실시간 호출이 없으므로 상한을 넉넉히 준다.
        cfg = settings.CONFIG.get("collection", {})
        await self.deep_backfill_pending(
            limit=int(cfg.get("deep_backfill_eod_max", 30)))
        return n

    async def eod_backfill_loop(self, interval_sec: int = 120) -> None:
        """평일 마감 후 1회. **재시작해도** 당일 중복 실행하지 않는다.

        완료 표시를 디스크에 둔다. 지역변수로 들고 있던 종전 구현은 재시작마다
        초기화됐다 — 실측 2026-07-31: 배포 5회에 마감 백필이 5번 돌아 398종목
        × 5 ≈ 2,000콜을 버렸다.
        """
        while True:
            try:
                cfg = settings.CONFIG.get("collection", {})
                now = datetime.now(KST)
                today = now.date().isoformat()
                if (
                    cfg.get("eod_backfill", True)
                    and settings.KIWOOM_APP_KEY
                    and now.weekday() < 5
                    and now.strftime("%H:%M") >= cfg.get("eod_backfill_after", "15:35")
                    and not await asyncio.to_thread(store.job_done, "eod_backfill", today)
                ):
                    await self.eod_backfill_once()
                    await asyncio.to_thread(store.mark_job_done, "eod_backfill", today)
            except Exception:  # noqa: BLE001
                log.exception("마감 백필 루프 오류")
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
