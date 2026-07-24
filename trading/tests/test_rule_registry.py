"""규칙 레지스트리 — 등록·순회·격리 테스트."""
import pandas as pd

from app.signals import rules


def _df():
    idx = pd.date_range("2026-07-20 09:00", periods=30, freq="1min")
    return pd.DataFrame([{"open": 100, "high": 101, "low": 99, "close": 100,
                          "volume": 1000}] * 30, index=idx)


def test_all_builtin_rules_registered():
    assert set(rules.REGISTRY) >= {"orb", "gap", "momentum", "pullback",
                                   "bounce_fade", "breakdown_retest"}
    # gap 만 전일 종가를 요구한다
    assert rules.REGISTRY["gap"][1] is True
    assert rules.REGISTRY["orb"][1] is False


def test_new_rule_via_decorator_is_evaluated():
    @rules.register("_test_rule")
    def _test_rule(df, cfg):
        return rules.Signal("_test_rule", "long", 100.0, 99.0, 101.5, "테스트")
    try:
        out = rules.evaluate_all(_df(), {"_test_rule": {"enabled": True}})
        assert [s.rule for s in out] == ["_test_rule"]   # 등록만으로 평가됨
        # 비활성이면 실행 안 됨
        assert rules.evaluate_all(_df(), {"_test_rule": {"enabled": False}}) == []
    finally:
        rules.REGISTRY.pop("_test_rule", None)


def test_broken_rule_does_not_block_others():
    @rules.register("_boom")
    def _boom(df, cfg):
        raise RuntimeError("의도적 오류")

    @rules.register("_ok")
    def _ok(df, cfg):
        return rules.Signal("_ok", "long", 100.0, 99.0, 101.5, "정상")
    try:
        out = rules.evaluate_all(_df(), {"_boom": {"enabled": True},
                                         "_ok": {"enabled": True}})
        assert [s.rule for s in out] == ["_ok"]          # 오류 규칙 격리
    finally:
        rules.REGISTRY.pop("_boom", None)
        rules.REGISTRY.pop("_ok", None)
