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


def weight_cap_qty(equity: float, entry: float, max_weight_pct: float) -> int:
    """한 종목이 계좌에서 차지할 수 있는 최대 수량 (비중 상한). 0=미적용."""
    if not max_weight_pct or max_weight_pct <= 0 or entry <= 0:
        return 0
    return math.floor(equity * max_weight_pct / 100 / entry)


def position_size(equity: float, risk_pct: float, entry: float, stop: float,
                  max_weight_pct: float = 0.0,
                  available: float | None = None) -> int:
    """손절까지 가면 계좌의 risk_pct% 만 잃도록 수량 계산.

    max_weight_pct: 한 종목이 계좌에서 차지할 최대 비중(%). 0 이면 미적용.
        이게 없으면 손절폭이 좁은 신호일수록 수량이 커져 첫 진입 한두 건이
        예수금을 다 써 버린다(선착순 편중). 상한을 두면 뒤에 오는 신호에도
        자금이 남는다.
    available: 지금 실제로 쓸 수 있는 현금(미지정 시 equity). 같은 사이클 안에서
        앞서 발주한 금액을 빼고 계산하려고 호출자가 넘긴다. 비중·리스크 상한은
        '계좌 전체' 기준으로 계산하고, 마지막 매수여력만 이 값으로 자른다.
    """
    dist = abs(entry - stop)
    if dist <= 0 or entry <= 0:
        return 0
    qty = math.floor(equity * risk_pct / 100 / dist)
    if max_weight_pct and max_weight_pct > 0:
        # 상한이 켜져 있으면 결과가 0주라도 적용한다(1주 값 > 상한인 고가주 차단).
        qty = min(qty, weight_cap_qty(equity, entry, max_weight_pct))
    cash = equity if available is None else max(0.0, available)
    return max(0, min(qty, math.floor(cash / entry)))


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
