"""Slack 알림 — 전이에서만 보내고, 실패해도 파이프라인을 죽이지 않는다."""

from __future__ import annotations

import asyncio

import pytest

from app.notify import SlackNotifier
from app.scheduler import Scheduler
from tests.conftest import FakeOllama


class RecordingNotifier(SlackNotifier):
    """실제 HTTP 대신 기록만 한다."""

    def __init__(self) -> None:
        super().__init__("https://hooks.slack.test/x", enabled=True)
        self.sent: list[str] = []

    async def _post(self, text: str, blocks=None) -> None:
        self.sent.append(text)


def test_disabled_without_webhook():
    """웹훅이 없으면 no-op — 호출부가 분기할 필요가 없어야 한다."""
    assert SlackNotifier("").enabled is False
    assert SlackNotifier("   ").enabled is False
    assert SlackNotifier("https://hooks.slack.test/x").enabled is True


async def test_disabled_notifier_sends_nothing():
    n = SlackNotifier("")           # 비활성
    await n.model_approval_needed({"model": "m", "roles": [], "est_size_gb": 1})
    await n.backend_offline("http://x", "boom")   # 예외 없이 통과하면 성공


async def test_backend_alert_only_on_transition():
    n = RecordingNotifier()
    for _ in range(5):
        await n.backend_offline("http://mac", "연결 실패")
    assert len(n.sent) == 1, "오프라인이 이어지는 동안 반복 알림하면 아무도 안 본다"

    for _ in range(3):
        await n.backend_online("http://mac")
    assert len(n.sent) == 2 and "복구" in n.sent[1]

    await n.backend_offline("http://mac", "또 끊김")
    assert len(n.sent) == 3          # 다시 끊기면 다시 알린다


async def test_first_online_is_silent():
    """기동할 때마다 '복구됐다'고 알리면 시끄럽다."""
    n = RecordingNotifier()
    await n.backend_online("http://mac")
    assert n.sent == []


async def test_model_approval_notified_once_per_state():
    n = RecordingNotifier()
    req = {"model": "qwen3:32b", "status": "pending", "roles": ["novel"],
           "requested_by": "roxlogy", "est_size_gb": 21.0}
    for _ in range(3):
        await n.model_approval_needed(req)
    assert len(n.sent) == 1
    assert "qwen3:32b" in n.sent[0]

    await n.model_install_finished("qwen3:32b", ok=True)
    assert len(n.sent) == 2 and "완료" in n.sent[1]


async def test_notification_failure_does_not_break_caller():
    """Slack 이 죽어도 잡은 계속 돌아야 한다."""
    n = SlackNotifier("http://127.0.0.1:9/nope", enabled=True)   # 즉시 연결 거부
    await n.backend_offline("http://mac", "x")                   # 예외가 새면 실패


async def test_scheduler_alerts_on_backend_down_and_recovery(store, roles):
    """실제 스케줄러 흐름에서 전이 알림이 나오는지."""
    fake = FakeOllama()
    n = RecordingNotifier()
    sched = Scheduler(store, roles, fake, poll_interval=0.01,
                      models_refresh=0.05, notifier=n)
    fake.tags_fail = True                       # 처음부터 다운
    await sched.start()
    try:
        deadline = asyncio.get_event_loop().time() + 3
        while not n.sent and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.05)
        assert n.sent and "응답 없음" in n.sent[0]

        fake.tags_fail = False                  # 복구
        deadline = asyncio.get_event_loop().time() + 3
        while len(n.sent) < 2 and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.05)
        assert len(n.sent) >= 2 and "복구" in n.sent[1]
    finally:
        await sched.stop()


async def test_scheduler_alerts_on_model_approval_needed(store, mroles_factory):
    fake = FakeOllama(models=["small"])
    n = RecordingNotifier()
    sched = Scheduler(store, mroles_factory, fake, poll_interval=0.01,
                      models_refresh=0.05, notifier=n)
    await sched.start()
    try:
        store.create_job(service="beta", role="novel", model="newbie",
                         lane="interactive", prompt="p", system=None)
        deadline = asyncio.get_event_loop().time() + 3
        while not n.sent and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.05)
        assert n.sent and "승인 필요" in n.sent[0] and "newbie" in n.sent[0]
    finally:
        await sched.stop()


@pytest.fixture
def mroles_factory():
    from app.config import RoleConfig
    return RoleConfig.from_dict({
        "backend": {"base_url": "http://mac.test:11434"},
        "mem_budget_gb": 40,
        "model_sizes_gb": {"small": 5, "newbie": 9},
        "roles": {
            "fast": {"model": "small", "lane": "interactive", "timeout": 30},
            "novel": {"model": "newbie", "lane": "interactive", "timeout": 30},
        },
    })
