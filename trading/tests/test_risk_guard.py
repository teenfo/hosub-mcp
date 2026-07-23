from app.trade import risk


def test_day_guard_target_halts():
    halted, why = risk.day_guard(1.2, target_pct=1.0, loss_limit_pct=2.0)
    assert halted and "목표" in why


def test_day_guard_loss_halts_first():
    halted, why = risk.day_guard(-2.5, target_pct=1.0, loss_limit_pct=2.0)
    assert halted and "손실" in why


def test_day_guard_normal_allows():
    assert risk.day_guard(0.3, 1.0, 2.0) == (False, "")


def test_day_guard_zero_disables():
    # 목표/한도 0 이면 해당 조건 미적용
    assert risk.day_guard(5.0, 0, 0) == (False, "")
