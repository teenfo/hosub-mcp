"""포지션 사이징과 리스크 한도."""
import math
from dataclasses import dataclass


def day_guard(realized_pct: float, target_pct: float, loss_limit_pct: float,
              unrealized_pct: float = 0.0,
              include_unrealized: bool = True) -> tuple[bool, str]:
    """당일 손익률(%)로 신규 진입 허용 여부 판단.
    - 손실 한도 도달 → 손실 차단(우선)
    - 목표 도달 → 이익 확정 마감
    반환: (중단 여부, 사유). 목표/한도가 0 이하면 해당 조건 미적용.

    **손실 판정에는 평가손익을 함께 센다**(2026-07-31). 종전에는 실현분만 봐서,
    물려 있는 포지션이 아무리 깊어도 가드가 침묵했다 — 실측 2026-07-30, 한도
    1.5% 인 날 계좌는 −4.26% 까지 내려갔다. 손실은 청산할 때 생기는 것이 아니라
    청산할 때 확정될 뿐이다.

    **이익 목표는 실현분만 본다.** 평가이익으로 목표를 선언하면 아직 손에 쥐지
    않은 돈으로 그날 장사를 접게 된다 — 두 판정은 성격이 다르다.
    """
    loss_basis = realized_pct + (unrealized_pct if include_unrealized else 0.0)
    if loss_limit_pct and loss_basis <= -abs(loss_limit_pct):
        why = f"일일 손실 한도(-{loss_limit_pct:g}%) 도달 — 신규 진입 중단"
        if include_unrealized and unrealized_pct:
            why += (f" (실현 {realized_pct:+.2f}% + 평가 {unrealized_pct:+.2f}%"
                    f" = {loss_basis:+.2f}%)")
        return True, why
    if target_pct and realized_pct >= target_pct:
        return True, f"일일 목표(+{target_pct:g}%) 도달 — 이익 확정, 신규 진입 중단"
    return False, ""


def flatten_call(realized_pct: float, unrealized_pct: float,
                 flatten_pct: float) -> tuple[bool, str]:
    """보유분까지 정리해야 하는 수준인가 — 신규 진입 차단보다 한 단계 위.

    두 문턱을 나눠 두는 이유: "더 벌이지 않는다" 와 "지금 다 접는다" 는 다른
    결정이다. 한도(`daily_loss_limit_pct`)는 앞을 막고, 이 값은 이미 벌어진
    것을 끊는다. `flatten_pct` 가 0 이면 미적용 — **기본값은 0(꺼짐)** 이고,
    켜는 것은 실거래 청산을 자동 발주하는 결정이므로 사용자가 한다.
    """
    if not flatten_pct:
        return False, ""
    total = realized_pct + unrealized_pct
    if total <= -abs(flatten_pct):
        return True, (f"일일 손실 정리선(-{flatten_pct:g}%) 도달 "
                      f"({total:+.2f}%) — 보유분 정리")
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

    def can_open(self, reserve: int = 0) -> tuple[bool, str]:
        """reserve: 이 후보가 쓸 수 없는 예약 슬롯 수.

        약세 국면의 인버스 전용 슬롯(사용자 결정 2026-08-03) — 롱 후보는
        `max_positions - reserve` 까지만, 인버스 후보는 reserve=0 으로 전체
        한도를 쓴다. 총 동시 포지션은 늘지 않는다(한도 **내** 예약).
        """
        if self.loss_limit_hit:
            return False, "일일 손실 한도 도달 — 신규 진입 차단"
        limit = self.max_positions - max(0, int(reserve))
        if self.open_positions >= limit:
            if self.open_positions < self.max_positions:
                return False, (f"롱 가용 슬롯({limit}/{self.max_positions}) 도달"
                               f" — 약세 국면 인버스 예약 {reserve}자리 제외")
            return False, f"최대 동시 포지션({self.max_positions}) 도달"
        return True, ""
