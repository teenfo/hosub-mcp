"""포트폴리오 재생기 — 여러 종목을 **같은 시계로** 재생해 자리·자금 경쟁을 재현한다.

## 왜 (ChatGPT 개발 요청서 Phase 2, 채택 2026-09-27)

`runner.run` 은 종목별 독립 재생이다 — 종목마다 '동시 1포지션' 이고 계좌가 없다.
실전은 한 계좌에서 여러 종목의 신호가 **같은 사이클에** 나오고, 우선순위
(`priority.order`) 순으로 동시 포지션 한도(max_positions)·종목당 비중 상한·
남은 현금·종목 중복 금지·진입 시간창·일일 손실 가드를 통과한 것만 발주된다.
8/3 에 "인버스 신호가 포지션 한도 경쟁에서 롱에 밀려 미발주" 를 실측한 바로
그 구조가 백테스트에 없었다. 규칙 단독 성적이 좋아도 실제 계좌에서는 자리를
못 얻어 표본이 달라질 수 있다 — 이 재생기가 그 차이를 잰다.

## 재현하는 것 / 하지 않는 것

재현: 분 단위 공통 시계 · 규칙 평가(`rules.evaluate_all`) · 우선순위 정렬 ·
진입 시간창(`rules.entry_window_note`) · long_only · 종목 중복 금지 ·
max_positions · 리스크 기반 수량(`risk.position_size`, 비중 상한) · 남은 현금 ·
일일 손실 가드(실현 기준, 도달 시 그날 신규 진입 중단) · 다음 봉 시가 체결(슬리피지) ·
청산은 실전 공식(`runner._live_step` = `trade/exit_policy`).

하지 않음(한계 — 결과 해석 시 병기): 국면 게이트·인버스 우선권/예약 슬롯(인버스
ETF 분봉이 재생 대상에 없다) · 발굴 tier 변동(감시목록은 재생 기간 내내 고정) ·
평가손익을 포함한 가드(실현만 본다) · 부분 체결·체결 실패 · 승인 대기(전부 자동
발주로 본다). 비용은 `settings.COSTS` 모델 비용이다.

## 쓰는 법

순수 함수 `replay(dfs, rules_cfg, pp, exit_params)` — DB 를 읽지 않는다.
서버 실행은 `python -m app.backtest.job portfolio` (저장 분봉 전체, 결과는
stdout JSON — 원장에 쓰지 않는다).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .. import settings
from ..signals import priority, rules
from ..trade import exit_policy, risk
from . import runner


@dataclass(frozen=True)
class PortfolioParams:
    equity: float = 10_000_000.0
    risk_pct: float = 0.5
    max_positions: int = 3
    max_weight_pct: float = 20.0
    daily_loss_limit_pct: float = 2.0
    one_per_symbol: bool = True
    long_only: bool = True

    @classmethod
    def from_settings(cls, equity: float | None = None) -> PortfolioParams:
        r = settings.RISK or {}

        def _f(k, d):
            try:
                return float(r.get(k, d) or 0)
            except (TypeError, ValueError):
                return d

        w = _f("max_position_weight_pct", 0.0)
        return cls(
            equity=float(equity or 10_000_000.0),
            risk_pct=_f("risk_per_trade_pct", 0.5),
            max_positions=int(_f("max_positions", 3)),
            max_weight_pct=w if 0 < w <= 100 else 0.0,
            daily_loss_limit_pct=_f("daily_loss_limit_pct", 0.0),
            one_per_symbol=bool(r.get("one_position_per_symbol", True)),
            long_only=bool(r.get("long_only", True)),
        )


@dataclass
class PTrade(runner.Trade):
    qty: int = 0
    model_entry: float = 0.0      # 신호가(체결 전) — 체결 후 재검증의 기준

    def pnl_krw(self, costs: dict) -> float:
        return self.pnl_pct(costs) / 100 * self.entry * self.qty


@dataclass
class PortfolioResult:
    trades: list[PTrade] = field(default_factory=list)
    blocked: dict[str, int] = field(default_factory=dict)
    days: int = 0
    start_equity: float = 0.0
    end_equity: float = 0.0

    def stats(self, costs: dict | None = None) -> dict:
        costs = costs or settings.COSTS
        closed = [t for t in self.trades if t.exit is not None]
        out: dict = {"days": self.days, "trades": len(closed),
                     "blocked": dict(sorted(self.blocked.items())),
                     "start_equity": round(self.start_equity),
                     "end_equity": round(self.end_equity)}
        if not closed:
            return out
        rs = [t.r_multiple(costs) for t in closed]
        krw = [t.pnl_krw(costs) for t in closed]
        wins = [k for k in krw if k > 0]
        losses = [k for k in krw if k <= 0]
        by_rule: dict[str, dict] = {}
        for t, r in zip(closed, rs):
            b = by_rule.setdefault(t.rule, {"n": 0, "sum_r": 0.0})
            b["n"] += 1
            b["sum_r"] += r
        mix: dict[str, int] = {}
        for t in closed:
            mix[t.exit_reason] = mix.get(t.exit_reason, 0) + 1
        out.update({
            "win_rate": round(100 * len(wins) / len(closed), 1),
            "avg_r": round(sum(rs) / len(rs), 3),
            "pnl_krw": round(sum(krw)),
            "return_pct": round((self.end_equity / self.start_equity - 1) * 100, 2)
            if self.start_equity else None,
            "profit_factor": round(sum(wins) / abs(sum(losses)), 2)
            if losses and sum(losses) != 0 else None,
            "by_rule": {k: {"n": v["n"], "avg_r": round(v["sum_r"] / v["n"], 3)}
                        for k, v in sorted(by_rule.items())},
            "exits": mix,
        })
        return out


def _block(res: PortfolioResult, why: str, key: tuple | None = None,
           seen: set | None = None) -> None:
    """차단 사유 집계 — 같은 (종목, 규칙) 신호가 매 분 다시 막혀도 **하루 한 번**만
    센다(key·seen). 그래야 '자리 때문에 놓친 신호 수' 로 읽힌다."""
    if key is not None and seen is not None:
        if (key, why) in seen:
            return
        seen.add((key, why))
    res.blocked[why] = res.blocked.get(why, 0) + 1


def replay(dfs: dict[str, pd.DataFrame], rules_cfg: dict | None = None,
           pp: PortfolioParams | None = None,
           exit_params: exit_policy.Params | None = None,
           costs: dict | None = None) -> PortfolioResult:
    """{종목: 여러 날의 1분봉} → 한 계좌 재생 결과."""
    rules_cfg = rules_cfg or settings.RULES
    pp = pp or PortfolioParams.from_settings()
    exit_params = exit_params or exit_policy.Params.from_settings()
    costs = costs or settings.COSTS
    slip = costs.get("slippage_bp", 5) / 10000
    res = PortfolioResult(start_equity=pp.equity)
    equity = cash = pp.equity

    # 날짜별로 자른다 — 전일 종가(prev_close)는 종목별 직전 날의 마지막 종가
    by_day: dict[pd.Timestamp, dict[str, pd.DataFrame]] = {}
    prev_close: dict[str, dict[pd.Timestamp, float | None]] = {}
    for sym, df in dfs.items():
        if df is None or df.empty:
            continue
        last: float | None = None
        pc: dict[pd.Timestamp, float | None] = {}
        for day, ddf in df.groupby(df.index.normalize()):
            by_day.setdefault(day, {})[sym] = ddf
            pc[day] = last
            last = float(ddf["close"].iloc[-1])
        prev_close[sym] = pc

    for day in sorted(by_day):
        res.days += 1
        day_dfs = by_day[day]
        start_eq = equity
        realized_today = 0.0
        halted = False
        fired: set[tuple[str, str]] = set()
        seen: set = set()                         # 차단 집계 중복 방지(하루)
        open_pos: dict[str, PTrade] = {}          # symbol -> 보유
        pending: list[tuple[str, PTrade]] = []    # 다음 봉 시가 체결 대기
        pos_of = {s: {ts: i for i, ts in enumerate(d.index)} for s, d in day_dfs.items()}
        clock = sorted(set().union(*[set(d.index) for d in day_dfs.values()]))

        def _close(sym: str, t: PTrade) -> None:
            nonlocal cash, equity, realized_today
            pnl = t.pnl_krw(costs)
            cash += t.entry * t.qty + pnl
            equity += pnl
            realized_today += pnl
            res.trades.append(t)
            del open_pos[sym]

        for ts in clock:
            # 0) 직전 봉에서 발주한 진입 — 이 봉 시가로 체결
            for sym, t in pending:
                i = pos_of[sym].get(ts)
                if i is None:
                    cash += t.entry * t.qty          # 봉이 없다 — 체결 불가, 현금 복원
                    _block(res, "no_next_bar")
                    continue
                bar = day_dfs[sym].iloc[i]
                fill = float(bar["open"]) * (1 + slip)
                cash += t.entry * t.qty - fill * t.qty
                t.entry, t.entry_ts = fill, ts
                if exit_params.refit:
                    fit = exit_policy.refit_lines(
                        t.side, t.model_entry, fill, t.stop, t.target,
                        exit_params.min_stop_pct, exit_params.max_stop_pct)
                    if fit:
                        t.stop_live, t.target_live = fit
                open_pos[sym] = t
            pending = []

            # 1) 보유 청산 — 실전 공식
            for sym in list(open_pos):
                i = pos_of[sym].get(ts)
                if i is None:
                    continue
                row = day_dfs[sym].iloc[i]
                bar = runner_bar(ts, row)
                t = open_pos[sym]
                if runner._live_step(t, bar, exit_params,
                                     exit_policy.max_hold_min(t.rule, rules_cfg)):
                    _close(sym, t)
            if not halted and pp.daily_loss_limit_pct and start_eq > 0 and \
                    realized_today / start_eq * 100 <= -abs(pp.daily_loss_limit_pct):
                halted = True

            # 2) 신호 수집 — 이 봉까지의 데이터로
            cands: list[dict] = []
            for sym, ddf in day_dfs.items():
                i = pos_of[sym].get(ts)
                if i is None or i < 10 or i + 1 >= len(ddf):
                    continue
                window = ddf.iloc[: i + 1]
                for sig in rules.evaluate_all(window, rules_cfg, prev_close[sym][day]):
                    if (sym, sig.rule) in fired:
                        continue
                    cands.append({"symbol": sym, "rule": sig.rule, "side": sig.side,
                                  "entry": sig.entry, "stop": sig.stop,
                                  "target": sig.target, "_sig": sig})
            if not cands:
                continue

            # 3) 우선순위 → 게이트 → 수량·자금
            n_open = len(open_pos) + len(pending)
            held = set(open_pos) | {s for s, _ in pending}
            for c in priority.order(cands, rules_cfg):
                sig, sym = c["_sig"], c["symbol"]
                if rules.entry_window_note(sig.rule, ts.to_pydatetime(), rules_cfg):
                    fired.add((sym, sig.rule))          # 실전: 기록만, 재발주 없음
                    _block(res, "entry_window")
                    continue
                if pp.long_only and sig.side != "long":
                    fired.add((sym, sig.rule))
                    _block(res, "long_only")
                    continue
                if pp.one_per_symbol and sym in held:
                    _block(res, "duplicate_symbol", (sym, sig.rule), seen)
                    continue
                if halted:
                    _block(res, "daily_loss_guard", (sym, sig.rule), seen)
                    continue
                if n_open >= pp.max_positions:
                    _block(res, "max_positions", (sym, sig.rule), seen)       # 자리가 나면 다시 산다
                    continue
                qty = risk.position_size(equity, pp.risk_pct, sig.entry, sig.stop,
                                         max_weight_pct=pp.max_weight_pct,
                                         available=cash)
                if qty < 1:
                    _block(res, "cash_or_size", (sym, sig.rule), seen)
                    continue
                fired.add((sym, sig.rule))
                t = PTrade(sym, sig.rule, sig.side, ts, float(sig.entry),
                           float(sig.stop), float(sig.target), qty=qty,
                           model_entry=float(sig.entry))
                cash -= float(sig.entry) * qty            # 발주 시점 예약(보수적)
                pending.append((sym, t))
                held.add(sym)
                n_open += 1

        # 마감 — 남은 보유는 마지막 봉 종가(청산 공식의 eod_flat_at 이 먼저 잡는다)
        for sym, t in pending:
            cash += t.entry * t.qty
        for sym in list(open_pos):
            t = open_pos[sym]
            last = day_dfs[sym].iloc[-1]
            t.exit, t.exit_reason, t.exit_ts = float(last["close"]), "eod", day_dfs[sym].index[-1]
            _close(sym, t)

    res.end_equity = equity
    return res


def runner_bar(ts: pd.Timestamp, row: pd.Series):
    """`runner._live_step` 이 읽는 봉 형태(Index·low·high·close)."""
    return _Bar(ts, float(row["open"]), float(row["high"]), float(row["low"]),
                float(row["close"]))


@dataclass(frozen=True)
class _Bar:
    Index: pd.Timestamp
    open: float
    high: float
    low: float
    close: float


def run_once() -> dict:
    """서버 실행 — 저장 분봉 전체(분봉 ≥200봉 종목) × 현행 설정. 원장에 쓰지 않는다."""
    from ..data import store

    bars_limit = settings.CONFIG.get("sweep", {}).get("bars_limit", 100_000)
    dfs = {}
    for sym in list(settings.WATCHLIST):
        df = store.load_bars(sym, "1m", limit=bars_limit)
        if len(df) >= 200:
            dfs[sym] = df
    res = replay(dfs)
    out = res.stats()
    out["symbols"] = len(dfs)
    out["params"] = PortfolioParams.from_settings().__dict__
    return out

