"""승인대기 주문 슬랙 알림.

알림은 곁다리이고 주문이 본체다 — 발송이 실패해도 주문 흐름을 멈추면 안 된다.
그리고 사람이 폰에서 보고 승인할지 정할 수 있을 만큼은 담겨야 한다.
"""
from __future__ import annotations

import pytest

from app import settings
from app.notify import slack


def _rec(**over):
    return {"symbol": "005930", "name": "삼성전자", "rule": "orb", "side": "long",
            "qty": 3, "entry": 70_000, "stop": 68_600, "target": 73_000,
            "need_cash": 210_000, "fundable": True,
            "reason": "시가 범위 상단 돌파"} | over


def test_text_carries_what_you_need_to_decide():
    t = slack.pending_order_text(_rec())
    for token in ("삼성전자", "005930", "orb", "3주", "70,000", "210,000",
                  "68,600", "73,000", "시가 범위 상단 돌파"):
        assert token in t, token


def test_unfundable_is_visible_in_the_headline():
    """열어 봐야 아는 정보는 알림의 값을 깎는다 — 제목에 드러나야 한다."""
    t = slack.pending_order_text(_rec(fundable=False, note="자금 부족 — 필요 21만"))
    assert t.splitlines()[0].startswith("🟡")
    assert "자금 부족" in t


def test_over_weight_is_marked_manual_only():
    t = slack.pending_order_text(_rec(fundable=True, over_weight=True))
    assert "수동 승인만" in t.splitlines()[0]


def test_guard_warning_is_carried():
    t = slack.pending_order_text(_rec(guard_warn="일일 손실 한도 도달"))
    assert "일일 손실 한도 도달" in t


def test_not_configured_is_a_noop(monkeypatch):
    monkeypatch.setattr(settings, "SLACK_BOT_TOKEN", "")
    monkeypatch.setattr(settings, "SLACK_CHANNEL", "")
    assert slack.configured() is False


@pytest.mark.asyncio
async def test_send_failure_does_not_raise(monkeypatch):
    """알림이 안 갔다고 주문 흐름을 멈추면 안 된다."""
    monkeypatch.setattr(settings, "SLACK_BOT_TOKEN", "xoxb-x")
    monkeypatch.setattr(settings, "SLACK_CHANNEL", "#alerts")

    async def boom(text):
        return False, "channel_not_found"

    monkeypatch.setattr(slack, "post", boom)
    await slack.notify("아무거나")          # 예외가 올라오면 실패


@pytest.mark.asyncio
async def test_unconfigured_notify_skips_post(monkeypatch):
    monkeypatch.setattr(settings, "SLACK_BOT_TOKEN", "")
    called = []

    async def spy(text):
        called.append(text)
        return True, ""

    monkeypatch.setattr(slack, "post", spy)
    await slack.notify("아무거나")
    assert called == []
