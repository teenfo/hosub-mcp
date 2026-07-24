"""비용 반영 백테스터.

저장된 1분봉을 날짜별로 재생하며 rules.evaluate_all 을 매 봉마다 호출하고,
체결은 다음 봉 시가(슬리피지 반영), 청산은 손절/목표 터치 또는 당일 종가.
숏은 '가격이 그만큼 움직였다면' 의 수익률로 계산한다(인버스 ETF 근사).
"""
from dataclasses import dataclass, field

import pandas as pd

from .. import settings
from ..signals import rules


@dataclass
class Trade:
    symbol: str
    rule: str
    side: str
    entry_ts: pd.Timestamp
    entry: float
    stop: float
    target: float
    exit_ts: pd.Timestamp | None = None
    exit: float | None = None
    exit_reason: str = ""

    def pnl_pct(self, costs: dict) -> float:
        """비용 반영 '주가 순수 변동률(%)'. 포지션 사이징 미반영(참고용)."""
        if self.exit is None:
            return 0.0
        raw = (self.exit - self.entry) / self.entry * 100
        if self.side == "short":
            raw = -raw
        commission = costs.get("commission_pct", 0.015) * 2
        tax = costs.get("sell_tax_pct", 0.15)
        slip = costs.get("slippage_bp", 5) / 100 * 2
        return raw - commission - tax - slip

    def stop_dist_pct(self) -> float:
        return abs(self.entry - self.stop) / self.entry * 100 if self.entry else 0.0

    def r_multiple(self, costs: dict) -> float:
        """손절 거리 대비 손익 배수(R). 손절가면 ≈ -1, 목표(1.5R)면 ≈ +1.5."""
        d = self.stop_dist_pct()
        return self.pnl_pct(costs) / d if d > 0 else 0.0

    def account_pct(self, costs: dict, risk_pct: float) -> float:
        """계좌 대비 손익%(포지션 사이징 반영). 손절 1회 = -risk_pct.
        고변동성·와이드스탑 종목의 큰 주가 변동률을 실제 계좌 영향으로 환산한다."""
        return self.r_multiple(costs) * risk_pct


@dataclass
class Result:
    trades: list[Trade] = field(default_factory=list)

    def stats(self, costs: dict | None = None, risk_pct: float | None = None) -> dict:
        """계좌 리스크 기준 통계(포지션 사이징 반영). avg_pnl_pct·누적·MDD·PF 는
        모두 '계좌 대비 %'. avg_price_pct 는 참고용 주가 변동률."""
        costs = costs or settings.COSTS
        risk_pct = risk_pct if risk_pct is not None else settings.RISK.get(
            "risk_per_trade_pct", 0.5)
        closed = [t for t in self.trades if t.exit is not None]
        if not closed:
            return {"trades": 0}
        accts = [t.account_pct(costs, risk_pct) for t in closed]   # 계좌 대비 %
        rs = [t.r_multiple(costs) for t in closed]
        price_pnls = [t.pnl_pct(costs) for t in closed]
        wins = [a for a in accts if a > 0]
        losses = [a for a in accts if a <= 0]
        equity, peak, mdd = 1.0, 1.0, 0.0
        for a in accts:
            equity *= 1 + a / 100
            peak = max(peak, equity)
            mdd = max(mdd, (peak - equity) / peak)
        pairs = list(zip(closed, accts))
        return {
            "trades": len(closed),
            "win_rate": round(len(wins) / len(closed) * 100, 1),
            "avg_pnl_pct": round(sum(accts) / len(accts), 3),      # 계좌 기준 평균 손익%
            "avg_r": round(sum(rs) / len(rs), 3),                  # 평균 R(리스크 대비 배수)
            "avg_price_pct": round(sum(price_pnls) / len(price_pnls), 3),  # 주가 변동률(참고)
            "risk_per_trade_pct": risk_pct,
            # 손실이 없으면 손익비는 수학적으로 무한 → JSON 직렬화 불가하므로 None(∞ 표시용)
            "profit_factor": round(
                sum(wins) / abs(sum(losses)), 2
            ) if losses and sum(losses) != 0 else None,
            "total_return_pct": round((equity - 1) * 100, 2),      # 계좌 기준 누적
            "max_drawdown_pct": round(mdd * 100, 2),               # 계좌 기준 MDD
            "by_rule": {
                r: round(sum(a for t, a in pairs if t.rule == r)
                         / max(1, len([1 for t, _ in pairs if t.rule == r])), 3)
                for r in {t.rule for t in closed}
            },
        }


def run(symbol: str, df: pd.DataFrame, rules_cfg: dict | None = None,
        sides: tuple[str, ...] | None = None) -> Result:
    """df: 여러 날짜의 1분봉 전체. 하루 1규칙 1회 진입, 동시 1포지션.
    sides: 체결할 방향 제한(예: ("long",) — 롱 전용 실전과 정합). None=제한 없음."""
    rules_cfg = rules_cfg or settings.RULES
    slip = settings.COSTS.get("slippage_bp", 5) / 10000
    result = Result()
    for day, day_df in df.groupby(df.index.normalize()):
        prev = df[df.index.normalize() < day]
        prev_close = float(prev["close"].iloc[-1]) if not prev.empty else None
        fired: set[str] = set()
        open_trade: Trade | None = None
        bars = list(day_df.itertuples())
        for i in range(10, len(bars)):
            bar = bars[i]
            # 1) 보유 포지션 청산 체크 (손절 우선 — 보수적)
            if open_trade:
                t = open_trade
                if t.side == "long":
                    hit_stop, hit_target = bar.low <= t.stop, bar.high >= t.target
                else:
                    hit_stop, hit_target = bar.high >= t.stop, bar.low <= t.target
                if hit_stop:
                    t.exit, t.exit_reason, t.exit_ts = t.stop, "stop", bar.Index
                elif hit_target:
                    t.exit, t.exit_reason, t.exit_ts = t.target, "target", bar.Index
                if t.exit is not None:
                    result.trades.append(t)
                    open_trade = None
                continue
            # 2) 신규 신호 평가 (현재 봉까지의 데이터만 사용)
            window = day_df.iloc[: i + 1]
            for sig in rules.evaluate_all(window, rules_cfg, prev_close):
                if sig.rule in fired or i + 1 >= len(bars):
                    continue
                if sides and sig.side not in sides:
                    continue   # 실전에서 집행 불가한 방향(예: 롱 전용의 숏)은 미체결
                fired.add(sig.rule)
                nxt = bars[i + 1]
                fill = nxt.open * (1 + slip if sig.side == "long" else 1 - slip)
                open_trade = Trade(symbol, sig.rule, sig.side, nxt.Index,
                                   fill, sig.stop, sig.target)
                break
        # 장 마감 강제 청산
        if open_trade and bars:
            open_trade.exit = float(bars[-1].close)
            open_trade.exit_reason, open_trade.exit_ts = "eod", bars[-1].Index
            result.trades.append(open_trade)
    return result
