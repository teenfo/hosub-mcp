"""발주 우선순위 동률 회귀 방지.

2026-07-27 실측: 신호 17건이 **전부 priority 65.0** 이었다. 원인 둘.
  · 모든 규칙이 `target_r: 1.5` 라 RR 항(min(rr,3)×10 = 15)이 상수
  · priority 키가 없는 규칙 6개가 조용히 기본값 0.5 로 떨어져
    튜닝된 gap·pullback(0.5)과 동률
동점은 종목코드 오름차순으로 갈리므로, `priority.py` 가 없애려고 만들어진
'종목코드 순 자금 편중'이 그대로 살아 있었다.
"""
import pytest

from app import settings
from app.signals import priority


def _sig(rule, symbol, entry=10_000, stop=9_800, target=10_300):
    return {"rule": rule, "symbol": symbol, "entry": entry,
            "stop": stop, "target": target}


# --- ① 모든 규칙이 명시적 priority 를 갖는다 ---

def test_every_rule_has_explicit_priority():
    """빠뜨리면 기본값으로 떨어져 튜닝된 규칙과 뒤섞인다 — 그게 이번 결함이었다."""
    from app.signals.rules import REGISTRY

    missing = [n for n in REGISTRY
               if "priority" not in (settings.RULES.get(n) or {})]
    assert not missing, f"config.yaml rules.<name>.priority 누락: {missing}"


def test_default_priority_below_every_configured_rule():
    """기본값이 튜닝값과 같으면 누락이 조용히 묻힌다."""
    configured = [float(c["priority"]) for c in settings.RULES.values()
                  if isinstance(c, dict) and "priority" in c]
    assert configured
    assert priority.DEFAULT_PRIORITY < min(configured)


def test_priority_order_follows_sweep_ranking():
    """주간 스윕 avg_r 순서와 우선순위가 어긋나면 잡는다.
    (실측 2026-07-25: orb +0.53 > momentum +0.25 > gap +0.04
                      > vwap_reclaim −0.43 > rsi_dip −0.66 > range_break_retest −1.63)"""
    p = {n: float(settings.RULES[n]["priority"]) for n in
         ("orb", "momentum", "gap", "vwap_reclaim", "rsi_dip", "range_break_retest")}
    assert p["orb"] > p["momentum"] > p["gap"]
    assert p["gap"] > p["vwap_reclaim"] > p["rsi_dip"] > p["range_break_retest"]


# --- ② 서로 다른 규칙의 신호가 동률이 아니다 ---

def test_distinct_rules_are_not_tied():
    """오늘 17건이 전부 65.0 이던 상태의 직접 회귀."""
    rules_cfg = settings.RULES
    scores = {r: priority.score(r, 10_000, 9_800, 10_300, rules_cfg)
              for r in ("orb", "momentum", "vwap_reclaim", "rsi_dip",
                        "range_break_retest")}
    assert len(set(scores.values())) == len(scores), f"동률 발생: {scores}"


def test_unconfigured_rule_ranks_last():
    rules_cfg = settings.RULES
    known = priority.score("orb", 10_000, 9_800, 10_300, rules_cfg)
    unknown = priority.score("__없는규칙__", 10_000, 9_800, 10_300, rules_cfg)
    assert unknown < known


def test_order_puts_high_priority_first_regardless_of_symbol_code():
    """종목코드가 앞서도 우선순위가 낮으면 뒤로 가야 한다."""
    out = priority.order([_sig("rsi_dip", "000001"), _sig("orb", "999999")],
                         settings.RULES)
    assert [c["symbol"] for c in out] == ["999999", "000001"]


# --- ③ 기존 계약 유지 ---

def test_same_rule_breaks_tie_deterministically():
    """같은 규칙·같은 RR 이면 매 사이클 같은 순서여야 한다(재현성)."""
    cands = [_sig("orb", "000300"), _sig("orb", "000100"), _sig("orb", "000200")]
    out = priority.order(cands, settings.RULES)
    assert [c["symbol"] for c in out] == ["000100", "000200", "000300"]


def test_rr_still_orders_within_same_rule():
    """target_r 이 규칙별로 달라지면 RR 항이 본래 역할을 해야 한다."""
    wide = _sig("orb", "000001", entry=10_000, stop=9_800, target=10_600)  # RR 3.0
    thin = _sig("orb", "000002", entry=10_000, stop=9_800, target=10_100)  # RR 0.5
    out = priority.order([thin, wide], settings.RULES)
    assert [c["symbol"] for c in out] == ["000001", "000002"]


def test_rr_bonus_is_capped():
    huge = priority.score("orb", 10_000, 9_990, 20_000, settings.RULES)
    at_cap = priority.score("orb", 10_000, 9_800, 10_600, settings.RULES)
    assert huge == at_cap


def test_zero_stop_distance_is_safe():
    assert priority.score("orb", 10_000, 10_000, 10_300, settings.RULES) > 0


def test_bad_priority_value_falls_back(monkeypatch):
    monkeypatch.setitem(settings.RULES, "orb", {"priority": "쓰레기"})
    s = priority.score("orb", 10_000, 9_800, 10_300, settings.RULES)
    assert s == pytest.approx(priority.DEFAULT_PRIORITY * 100 + 15, abs=0.01)


def test_order_does_not_mutate_input_order():
    cands = [_sig("rsi_dip", "000001"), _sig("orb", "000002")]
    priority.order(cands, settings.RULES)
    assert [c["rule"] for c in cands] == ["rsi_dip", "orb"]   # 원본 순서 보존
    assert all("priority" in c for c in cands)                # 근거는 채워 넣는다
