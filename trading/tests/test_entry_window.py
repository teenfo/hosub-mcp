"""진입 시간창 게이트 — 시간대 분석(2026-08-01)의 적용."""
from datetime import datetime
from zoneinfo import ZoneInfo

from app.signals import rules

KST = ZoneInfo("Asia/Seoul")
CFG = {"entry_cutoff": "15:00",
       "pullback": {"enabled": True, "no_entry": ["10:00-11:30"]}}


def _t(hhmm: str) -> datetime:
    h, m = hhmm.split(":")
    return datetime(2026, 7, 30, int(h), int(m), tzinfo=KST)


def test_컷오프_이후_전_규칙_차단():
    assert rules.entry_window_note("orb", _t("15:00"), CFG) is not None
    assert rules.entry_window_note("momentum", _t("15:10"), CFG) is not None
    assert rules.entry_window_note("orb", _t("14:59"), CFG) is None


def test_규칙별_창은_그_규칙만_차단():
    assert rules.entry_window_note("pullback", _t("10:00"), CFG) is not None
    assert rules.entry_window_note("pullback", _t("11:29"), CFG) is not None
    # 경계: 창 끝은 열림 — 11:30 은 통과
    assert rules.entry_window_note("pullback", _t("11:30"), CFG) is None
    assert rules.entry_window_note("pullback", _t("09:59"), CFG) is None
    # 다른 규칙은 같은 시각에도 통과
    assert rules.entry_window_note("momentum", _t("10:30"), CFG) is None


def test_형식_오류_창은_통과_기본값은_무제한():
    bad = {"pullback": {"no_entry": ["1030~1130", 123]}}
    assert rules.entry_window_note("pullback", _t("10:30"), bad) is None
    assert rules.entry_window_note("pullback", _t("10:30"), {}) is None
