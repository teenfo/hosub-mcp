"""청산 공식 — 실전(데스크·원장)과 백테스트가 **같은 함수**를 쓴다 (2026-09-28).

## 왜

백테스터(`backtest/runner.py`)는 손절·목표 터치와 당일 종가 청산만 재현했다.
실전은 그 사이에 네 가지를 더 한다:

  ① 체결 후 손절폭 재검증(refit)   — 슬리피지가 폭을 대역 밖으로 밀면 경계로 당김
  ② 손절선 추종·이익 확정(trailing) — 데스크(desk.json: tighten_at·lock_gain_pct)
  ③ 익절 상한(max_gain_pct)         — 목표가를 진입가 +N% 로 끌어내림
  ④ 시간 손절(max_hold_min)·마감 정리(eod_flat_at 15:20)

그래서 백테스트의 R 과 실거래의 R 은 **다른 청산 규칙**의 결과였다. 8/1 에 보류로
남긴 '09:00 버킷 합성 vs 실거래 정반대' 의 유력한 원인 후보이고, 외부 리뷰
(ChatGPT 개발 요청서 §6.2)가 지적한 것도 이것이다. 공식을 한 곳에 두고 양쪽이
부르게 하면, 실전 규칙이 바뀌는 순간 백테스트도 같이 바뀐다.

## 규약

- 이 모듈은 **순수 함수**만 둔다 — DB·네트워크·시계 없음. 설정 스냅샷은
  `Params.from_settings()` 가 한 번 읽어 넘긴다(백테스트가 재생 중 설정 변경에
  흔들리지 않게).
- 실전 호출부(`desk.update_lines`, `ledger.refit_lines`·`max_hold_min`)의 동작은
  이 추출로 **바뀌지 않는다** — 기존 테스트가 그대로 통과하는 것이 그 증거다.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Params:
    """청산 공식의 설정 스냅샷. 기본값은 각 원천(desk.DEFAULTS·config)과 같다."""
    trailing: bool = True
    tighten_at: float = 0.5
    lock_gain_pct: float = 30.0
    trail_target: bool = False
    max_gain_pct: float = 3.0
    refit: bool = True
    min_stop_pct: float = 0.0
    max_stop_pct: float = 0.0
    eod_flat_at: str = "15:20"

    @classmethod
    def from_settings(cls) -> Params:
        """지금 실전이 쓰는 값 — 데스크 런타임 오버라이드(desk.json)까지 반영.

        추종은 데스크가 켜져 있을 때만 돈다(`desk.trailing()`) — 그 조건도 같이 본다.
        """
        from .. import settings
        from . import desk

        c = desk.cfg()
        rules = settings.RULES or {}
        ex = settings.CONFIG.get("execution", {}) or {}

        def _f(v, d):
            try:
                return float(v)
            except (TypeError, ValueError):
                return d

        return cls(
            trailing=desk.trailing(),
            tighten_at=_f(c.get("tighten_at"), 0.5),
            lock_gain_pct=_f(c.get("lock_gain_pct"), 30.0),
            trail_target=bool(c.get("trail_target", False)),
            max_gain_pct=_f(c.get("max_gain_pct"), 3.0),
            refit=bool(rules.get("refit_stop_on_fill", True)),
            min_stop_pct=_f(rules.get("min_stop_pct"), 0.0),
            max_stop_pct=_f(rules.get("max_stop_pct"), 0.0),
            eod_flat_at=str(ex.get("eod_flat_at", "15:20")),
        )


# --------------------------------------------------------------------------
# ① 체결 후 재검증
# --------------------------------------------------------------------------
def refit_lines(side: str, model_entry: float, entry: float, stop: float,
                target: float, lo: float, hi: float) -> tuple[float, float] | None:
    """체결가가 확정된 뒤 손절·목표를 **실측 진입가 기준**으로 다시 본다.

    신호 단계의 손절폭 대역 검사(`rules.evaluate_all`)는 모델 진입가로 한다.
    체결가는 그 뒤에 정해지고, 슬리피지가 폭을 대역 밖으로 밀어낼 수 있다.
    **진입 전이라면 폐기했을 폭을 진입 후에는 아무도 다시 보지 않는다.**

    실측 2026-07-27~31: 107건 중 17건이 체결 후 1.0~2.5% 밖이었고, 슬리피지가
    폭을 최대 0.94%p 밀어냈다. 대역의 양쪽 끝은 각각 "비용을 못 이긴다"와
    "목표가 하루 변동폭 밖이다" 를 뜻하므로, 밖으로 나간 폭은 그대로 두면
    진입 시점에 이미 결론이 난 거래가 된다.

    대역 **안이면 손절선을 그대로 둔다** — 대개 구조적 수준(레인지 저점·스윙
    저점)이고 옮기면 근거가 사라진다. 밖일 때만 가장 가까운 경계로 당기고,
    목표는 원래 설계한 손익비 R 을 유지하도록 다시 만든다.

    반환: 새 (손절선, 목표가). 손댈 필요가 없으면 None.
    """
    if not (lo or hi):
        return None
    if not (model_entry and entry and stop) or entry <= 0 or model_entry <= 0:
        return None
    long = side == "long"
    # 체결가가 이미 손절선을 넘어 버렸다면 옮기지 않는다. 손절선을 다시 그으면
    # '즉시 청산될 자리' 가 '한 번 더 잃을 자리' 로 바뀐다 — 실거래에 닿는
    # 판단은 보수적인 쪽으로 둔다.
    if (long and entry <= stop) or (not long and entry >= stop):
        return None
    width = round(abs(entry - stop) / entry * 100, 4)
    if (not lo or width >= lo) and (not hi or width <= hi):
        return None
    want = min(max(width, lo or width), hi or width)
    # 원 설계의 손익비. 목표가 없거나 손절폭이 0이면 R 을 복원할 수 없다 —
    # 그때는 목표를 건드리지 않는다(폭만 고친다).
    base = abs(model_entry - stop)
    r = abs(target - model_entry) / base if base > 0 and target else None
    new_stop = entry * (1 - want / 100) if long else entry * (1 + want / 100)
    new_target = target
    if r is not None:
        new_target = (entry * (1 + want * r / 100) if long
                      else entry * (1 - want * r / 100))
    return round(new_stop, 2), round(float(new_target), 2)


# --------------------------------------------------------------------------
# ②③ 추종·익절 상한
# --------------------------------------------------------------------------
def trail_lines(side: str, entry: float, stop0: float, target0: float,
                cur_stop: float, cur_target: float, price: float,
                p: Params) -> tuple[float, float]:
    """현재가에 맞춘 새 (손절선, 익절선). 추종 규칙은 `desk.update_lines` docstring
    (사용자 지침 2026-07-29) — 갭 추종 + 목표 tighten_at 도달 후 상승분의
    lock_gain_pct% 확정, 익절선은 기본 고정, max_gain_pct 천장. 하향은 없다.

    반환값은 항상 두 숫자(바뀌지 않았으면 cur 그대로) — '쓸지 말지' 는 호출자 몫.
    """
    at, lock = p.tighten_at, p.lock_gain_pct / 100.0
    cap_pct = p.max_gain_pct / 100.0
    if side == "long":
        s_gap, t_gap = entry - stop0, target0 - entry
        cand = price - s_gap if s_gap > 0 else cur_stop
        if t_gap > 0 and lock > 0 and price >= entry + t_gap * at:
            cand = max(cand, entry + (price - entry) * lock)   # 상승분의 x% 확정
        new_stop = max(cur_stop, cand)
        new_target = (max(cur_target, price + t_gap)
                      if (p.trail_target and t_gap > 0) else cur_target)
        if cap_pct > 0:
            new_target = min(new_target, entry * (1 + cap_pct))
    else:
        s_gap, t_gap = stop0 - entry, entry - target0
        cand = price + s_gap if s_gap > 0 else cur_stop
        if t_gap > 0 and lock > 0 and price <= entry - t_gap * at:
            cand = min(cand, entry - (entry - price) * lock)
        new_stop = min(cur_stop, cand)
        new_target = (min(cur_target, price - t_gap)
                      if (p.trail_target and t_gap > 0) else cur_target)
        if cap_pct > 0:
            new_target = max(new_target, entry * (1 - cap_pct))
    return round(new_stop, 2), round(new_target, 2)


def stop_moved_in_profit(side: str, entry: float, prev_stop: float,
                         new_stop: float) -> bool:
    """시간 손절 시계를 다시 돌릴 조건 — 손절선이 **이익 구간에서** 움직였다.

    손실 구간 반등으로 손절선이 한 칸 오른 것은 리셋하지 않는다(사용자 결정
    2026-07-29, `ledger.set_lines` 주석: 그 전이 6건이 5,245원을 깎았다).
    """
    if new_stop == prev_stop or entry <= 0:
        return False
    return new_stop > entry if side == "long" else new_stop < entry


# --------------------------------------------------------------------------
# ④ 판정
# --------------------------------------------------------------------------
def max_hold_min(rule: str, rules_cfg: dict) -> int:
    """그 규칙의 최대 보유 시간(분). 0 이면 시간 손절 없음.
    규칙별 설정이 전역 기본값(rules.max_hold_min)을 덮어쓴다."""
    r = rules_cfg.get(rule)
    v = r.get("max_hold_min") if isinstance(r, dict) and "max_hold_min" in r \
        else rules_cfg.get("max_hold_min", 0)
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def line_hit(side: str, low: float, high: float, stop: float,
             target: float) -> str | None:
    """봉(또는 틱: low=high=가격)이 손절·목표에 닿았는가. **손절 우선**(보수적)."""
    if side == "long":
        if low <= stop:
            return "stop"
        return "target" if high >= target else None
    if high >= stop:
        return "stop"
    return "target" if low <= target else None
