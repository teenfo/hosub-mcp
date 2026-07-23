"""포지션 사이징과 리스크 한도."""
import math
from dataclasses import dataclass


def day_guard(realized_pct: float, target_pct: float,
              loss_limit_pct: float) -> tuple[bool, str]:
    """당일 실현손익률(%)로 신규 진입 허용 여부 판단.
    - 손실 한도 도달 → 손실 차단(우선)
    - 목표 도달 → 이익 확정 마감
    반환: (중단 여부, 사유). 목표/한도가 0 이하면 해당 조건 미적용."""
    if loss_limit_pct and realized_pct <= -abs(loss_limit_pct):
        return True, f"일일 손실 한도(-{loss_limit_pct:g}%) 도달 — 신규 진입 중단"
    if target_pct and realized_pct >= target_pct:
        return True, f"일일 목표(+{target_pct:g}%) 도달 — 이익 확정, 신규 진입 중단"
    return False, ""


def position_size(equity: float, risk_pct: float, entry: float, stop: float) -> int:
    """손절까지 가면 계좌의 risk_pct% 만 잃도록 수량 계산."""
    dist = abs(entry - stop)
    if dist <= 0 or entry <= 0:
        return 0
    qty = math.floor(equity * risk_pct / 100 / dist)
    max_affordable = math.floor(equity / entry)
    return max(0, min(qty, max_affordable))


@dataclass
class DailyRiskState:
    equity: float
    daily_loss_limit_pct: float
    realized_pnl: float = 0.0
    open_positions: int = 0
    max_positions: int = 3

    def record_pnl(self, pnl: float) -> None:
        self.realized_pnl += pnl

    @property
    def loss_limit_hit(self) -> bool:
        return self.realized_pnl <= -self.equity * self.daily_loss_limit_pct / 100

    def can_open(self) -> tuple[bool, str]:
        if self.loss_limit_hit:
            return False, "일일 손실 한도 도달 — 신규 진입 차단"
        if self.open_positions >= self.max_positions:
            return False, f"최대 동시 포지션({self.max_positions}) 도달"
        return True, ""
