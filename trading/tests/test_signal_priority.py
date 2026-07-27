"""신호 발주 우선순위 — 선착순(종목코드 순) 대신 신호 품질 순으로 자금 배분."""
from app.signals import priority

CFG = {
    "orb": {"enabled": True, "priority": 1.0},
    "momentum": {"enabled": True, "priority": 0.6},
    "pullback": {"enabled": True},          # priority 미지정 → 기본값
}


def _c(symbol, rule, entry=10_000, stop=9_000, target=11_500):
    return {"symbol": symbol, "rule": rule, "entry": entry,
            "stop": stop, "target": target}


def test_rule_weight_dominates():
    """규칙 가중치가 1차 기준 — 뒤 종목코드라도 좋은 규칙이 먼저."""
    out = priority.order([_c("900000", "momentum"), _c("000100", "orb")], CFG)
    assert [c["rule"] for c in out] == ["orb", "momentum"]


def test_rr_breaks_tie_within_rule():
    """같은 규칙끼리는 손익비(RR)가 높은 신호가 먼저."""
    lo = _c("000100", "orb", entry=10_000, stop=9_000, target=11_000)   # RR 1.0
    hi = _c("000200", "orb", entry=10_000, stop=9_000, target=12_500)   # RR 2.5
    assert [c["symbol"] for c in priority.order([lo, hi], CFG)] == \
        ["000200", "000100"]


def test_missing_priority_uses_default():
    out = priority.order([_c("000100", "pullback"), _c("000200", "momentum")], CFG)
    assert [c["rule"] for c in out] == ["momentum", "pullback"]   # 0.6 > 0.5


def test_unknown_rule_and_bad_value_are_safe():
    """config 에 없는 규칙·잘못된 priority 값도 기본 가중치로 처리(예외 없음)."""
    cfg = dict(CFG, weird={"priority": "높음"})
    out = priority.order([_c("000100", "weird"), _c("000200", "nope")], cfg)
    assert len(out) == 2
    assert out[0]["priority"] == out[1]["priority"]      # 둘 다 기본값


def test_deterministic_tiebreak():
    """완전 동점이면 종목코드 → 규칙명 순 — 매 사이클 같은 순서."""
    a, b = _c("000200", "orb"), _c("000100", "orb")
    assert [c["symbol"] for c in priority.order([a, b], CFG)] == \
        [c["symbol"] for c in priority.order([b, a], CFG)] == ["000100", "000200"]


def test_zero_stop_distance_scores_without_error():
    c = _c("000100", "orb", entry=10_000, stop=10_000, target=10_000)
    assert priority.order([c], CFG)[0]["priority"] == 100.0   # RR 보너스 0


def test_rr_bonus_capped():
    """극단적 손익비가 규칙 가중치를 뒤집지 못하도록 상한(3배)을 둔다."""
    huge = _c("000100", "pullback", entry=10_000, stop=9_990, target=99_000)
    orb = _c("000200", "orb")
    assert [c["rule"] for c in priority.order([huge, orb], CFG)] == \
        ["orb", "pullback"]


def test_order_does_not_mutate_input_order():
    src = [_c("900000", "momentum"), _c("000100", "orb")]
    priority.order(src, CFG)
    assert [c["rule"] for c in src] == ["momentum", "orb"]   # 원본 순서 유지
