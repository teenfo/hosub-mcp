"""긴급 카테고리가 실제로 동작하는지 — 죽은 스위치 회귀 방지.

2026-07-27 발견. config 의 `alerts.urgent_categories: [정정공시, 거래정지]` 가
가리키는 두 값이 `classify.CATEGORIES` 에 **없었다**. 분류기가 낼 수 없는 값이라:

  · notify/rules.py:32 의 urgent 판정이 **항상 False** — 조용시간 즉시발송·
    일일상한 예외가 통째로 죽어 있었다
  · 거래정지 공시는 '기타'(w_category 0.2)로 떨어져 점수 ≈18~20 → 임계 60 미달
    → **알림도 편입도 되지 않았다**

보유 중 거래정지는 청산 자체가 불가능해지는 사건이다. 사각으로 둘 수 없다.
"""
import pytest

from app import settings
from app.notify import rules
from app.pipeline import classify, score


def _cfg(**over):
    return {"shadow_mode": False, "quiet_start": "22:00", "quiet_end": "07:00",
            "urgent_categories": ["정정공시", "거래정지"],
            "default_daily_cap": 5, **over}


# --- ① 설정이 가리키는 카테고리를 분류기가 낼 수 있어야 한다 ---

def test_urgent_categories_are_classifiable():
    """이 어긋남이 죽은 스위치의 원인이었다."""
    urgent = settings.ALERTS.get("urgent_categories") or []
    assert urgent, "urgent_categories 가 비었다"
    missing = [c for c in urgent if c not in classify.CATEGORIES]
    assert not missing, f"분류기가 낼 수 없는 긴급 카테고리: {missing}"


def test_urgent_categories_have_weights():
    """카테고리만 추가하고 가중치를 빼먹으면 기본값 0.2 로 떨어져 임계 미달된다."""
    w = settings.SCORE.get("w_category", {})
    for c in settings.ALERTS.get("urgent_categories") or []:
        assert w.get(c), f"{c} 에 w_category 가 없다"
        assert w[c] >= 1.0, f"{c} 가중치가 낮다({w[c]}) — 임계를 못 넘는다"


def test_system_prompt_lists_new_categories():
    """프롬프트에 없으면 모델이 그 값을 쓰지 않는다."""
    for c in ("거래정지", "정정공시"):
        assert c in classify.SYSTEM_PROMPT
    # 개수 안내가 하드코딩 10 으로 남아 있으면 모델이 혼란스러워한다
    assert f"{len(classify.CATEGORIES)}개" in classify.SYSTEM_PROMPT


def test_new_categories_survive_schema_validation():
    """스키마 검증이 새 카테고리를 거절하면 llm_failed 로 떨어진다."""
    for c in ("거래정지", "정정공시"):
        payload = {"category": c, "is_material": True,
                   "impact_direction": "negative", "impact_horizon": "immediate",
                   "confidence": 0.9, "reason": "매매거래정지 안내",
                   "summary": "거래가 정지되었다.", "contains_numbers": False}
        assert classify.validate(payload)["category"] == c


# --- ② 점수가 임계를 넘어야 알림·편입으로 이어진다 ---

def _score_of(category, source="dart", conf=0.9):
    a = {"category": category, "confidence": conf, "is_material": True}
    return score.compute(source, a, "new", settings.SCORE)[0]


def test_halt_scores_above_alert_threshold():
    """종전엔 '기타'로 떨어져 ≈18~20 이었다 — 임계 60 미달로 조용히 버려졌다."""
    threshold = int(settings.ALERTS.get("default_threshold", 60))
    assert _score_of("거래정지") >= threshold
    assert _score_of("정정공시") >= threshold


def test_halt_beats_generic_fallback():
    assert _score_of("거래정지") > _score_of("기타")


def test_regression_generic_fallback_is_below_threshold():
    """'기타'로 떨어지면 실제로 임계를 못 넘는다는 것 — 결함의 크기를 못박는다."""
    assert _score_of("기타") < int(settings.ALERTS.get("default_threshold", 60))


# --- ③ 긴급 판정이 실제로 조용시간·상한을 뚫는다 ---

def test_urgent_sends_during_quiet_hours():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    night = datetime(2026, 7, 27, 23, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    assert rules.decide(night, "거래정지", 0, _cfg()) == "send"
    assert rules.decide(night, "실적", 0, _cfg()) == "defer"   # 일반은 묶음


def test_urgent_ignores_daily_cap():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    day = datetime(2026, 7, 27, 11, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    assert rules.decide(day, "정정공시", 99, _cfg()) == "send"
    assert rules.decide(day, "실적", 99, _cfg()) == "suppress"


def test_shadow_mode_still_wins():
    """긴급이어도 Shadow 중에는 발송하지 않는다 — 게이트 순서가 뒤집히면 잡는다."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    day = datetime(2026, 7, 27, 11, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    assert rules.decide(day, "거래정지", 0, _cfg(shadow_mode=True)) == "shadow"


# --- ④ 기존 카테고리가 깨지지 않았는지 ---

@pytest.mark.parametrize("cat", ["실적", "공급계약", "증설투자", "지배구조",
                                 "자금조달", "소송규제", "인사", "시황해설",
                                 "단순재탕", "기타"])
def test_existing_categories_intact(cat):
    assert cat in classify.CATEGORIES
