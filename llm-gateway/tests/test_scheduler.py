"""스케줄러 테스트 — 레인 분리·메모리 예산·모델 친화·재시도.

설계서 13절이 요구하는 회귀 테스트 2종을 포함한다:
  1) head-of-line: batch 장시간 잡이 interactive 를 막지 않는다
  2) 모델 전환 최소화: 친화 스케줄링이 실제로 전환 횟수를 줄인다
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.scheduler import Scheduler
from app.store import FAILED, QUEUED, SUCCEEDED

pytestmark = pytest.mark.asyncio


def mk_job(store, role, model, lane, **kw):
    return store.create_job(service="alpha", role=role, model=model, lane=lane,
                            prompt=kw.pop("prompt", "p"), system=None, **kw)


async def wait_until(fn, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        await asyncio.sleep(interval)
    return False


async def test_runs_job_to_success(store, roles, fake_ollama):
    sched = Scheduler(store, roles, fake_ollama, poll_interval=0.01)
    await sched.start()
    try:
        j = mk_job(store, "fast", "small", "interactive")
        assert await wait_until(lambda: store.get_job(j["id"])["status"] == SUCCEEDED)
        assert "[small]" in store.get_job(j["id"])["response"]
    finally:
        await sched.stop()


async def test_head_of_line_blocking_is_avoided(store, roles):
    """회귀 테스트 1 — batch 장시간 잡이 interactive 를 막지 않아야 한다."""
    from tests.conftest import FakeOllama

    # batch 잡은 오래(2초), interactive 잡은 짧게(0.05초)
    fake = FakeOllama(delays={"big": 2.0, "small": 0.05})
    sched = Scheduler(store, roles, fake, poll_interval=0.01)
    await sched.start()
    try:
        # batch 레인에 긴 잡을 먼저 넣고, 확실히 실행에 들어가게 한다
        heavy = mk_job(store, "heavy", "big", "batch")
        assert await wait_until(
            lambda: store.get_job(heavy["id"])["status"] == "running", timeout=2
        )
        # 그 상태에서 interactive 요청 투입
        quick = mk_job(store, "fast", "small", "interactive")
        # heavy 가 끝나기 전에 quick 이 완료되어야 한다
        assert await wait_until(
            lambda: store.get_job(quick["id"])["status"] == SUCCEEDED, timeout=1.5
        )
        assert store.get_job(heavy["id"])["status"] == "running"  # 아직 진행 중
        assert fake.max_concurrent == 2                           # 두 레인 동시 실행
    finally:
        await sched.stop()


async def test_lane_concurrency_is_one(store, roles):
    """같은 레인 안에서는 한 번에 하나만 실행된다."""
    from tests.conftest import FakeOllama

    fake = FakeOllama(delay=0.15)
    sched = Scheduler(store, roles, fake, poll_interval=0.01)
    await sched.start()
    try:
        ids = [mk_job(store, "fast", "small", "interactive")["id"] for _ in range(4)]
        assert await wait_until(
            lambda: all(store.get_job(i)["status"] == SUCCEEDED for i in ids), timeout=5
        )
        # interactive 만 썼으므로 동시 실행은 1을 넘지 않아야 한다
        assert fake.max_concurrent == 1
    finally:
        await sched.stop()


async def test_memory_budget_blocks_oversized_pair(store, roles):
    """예산(40GB)을 넘는 조합(big 30 + mid 10 = 40 은 OK, big+big 은 불가)."""
    from tests.conftest import FakeOllama

    fake = FakeOllama(delay=0.3)
    # 예산을 35로 낮춰 big(30) + mid(10) 조합이 막히게 한다
    roles.mem_budget_gb = 35
    sched = Scheduler(store, roles, fake, poll_interval=0.01)
    await sched.start()
    try:
        heavy = mk_job(store, "heavy", "big", "batch")           # 30GB
        assert await wait_until(
            lambda: store.get_job(heavy["id"])["status"] == "running", timeout=2
        )
        chat = mk_job(store, "chat", "mid", "interactive")       # 10GB → 합 40 > 35
        await asyncio.sleep(0.15)
        # 예산 초과라 아직 시작하지 못해야 한다
        assert store.get_job(chat["id"])["status"] == QUEUED
        # heavy 가 끝나면 실행된다
        assert await wait_until(
            lambda: store.get_job(chat["id"])["status"] == SUCCEEDED, timeout=3
        )
    finally:
        await sched.stop()


async def test_model_affinity_reduces_switches(store, roles):
    """회귀 테스트 2 — 같은 모델 잡을 묶어 처리해 모델 전환을 줄인다."""
    from tests.conftest import FakeOllama

    fake = FakeOllama(delay=0.01)
    sched = Scheduler(store, roles, fake, poll_interval=0.01)
    # 모델을 번갈아 주입 (batch 레인: bulk=mid, tiny=small)
    for i in range(4):
        mk_job(store, "bulk", "mid", "batch")
        mk_job(store, "tiny", "small", "batch")

    await sched.start()
    try:
        assert await wait_until(lambda: len(fake.calls) == 8, timeout=6)
    finally:
        await sched.stop()

    # 친화 스케줄링이 없으면 번갈아 실행되어 전환이 7회에 가깝다.
    # 묶어서 처리하면 훨씬 적어야 한다.
    assert fake.model_switches <= 3, f"모델 전환 과다: {fake.calls}"


async def test_starvation_override(store, roles):
    """오래 기다린 잡은 모델 친화를 무시하고 우선 실행된다."""
    from tests.conftest import FakeOllama

    fake = FakeOllama(delay=0.01)
    sched = Scheduler(store, roles, fake, poll_interval=0.01, starvation_seconds=0)
    # starvation_seconds=0 이면 모든 잡이 '굶주림' 상태 → 순수 FIFO 로 동작
    first = mk_job(store, "tiny", "small", "batch")
    second = mk_job(store, "bulk", "mid", "batch")
    await sched.start()
    try:
        assert await wait_until(lambda: len(fake.calls) == 2, timeout=5)
    finally:
        await sched.stop()
    assert fake.calls == ["small", "mid"]   # 생성 순서 그대로


async def test_retry_then_success(store, roles):
    """재시도 가능한 실패는 백오프 후 재시도된다."""
    from tests.conftest import FakeOllama

    fake = FakeOllama(fail_times=2, retryable=True)
    sched = Scheduler(store, roles, fake, poll_interval=0.01,
                      max_retries=3, backoff_base=1.0)  # 백오프 1s^n = 1s
    await sched.start()
    try:
        j = mk_job(store, "fast", "small", "interactive")
        assert await wait_until(
            lambda: store.get_job(j["id"])["status"] == SUCCEEDED, timeout=8
        )
        assert store.get_job(j["id"])["attempts"] == 3
    finally:
        await sched.stop()


async def test_non_retryable_fails_immediately(store, roles):
    """모델 없음 같은 오류는 재시도하지 않는다."""
    from tests.conftest import FakeOllama

    fake = FakeOllama(fail_times=99, retryable=False)
    sched = Scheduler(store, roles, fake, poll_interval=0.01, max_retries=3)
    await sched.start()
    try:
        j = mk_job(store, "fast", "small", "interactive")
        assert await wait_until(
            lambda: store.get_job(j["id"])["status"] == FAILED, timeout=3
        )
        assert store.get_job(j["id"])["attempts"] == 1     # 재시도 없음
    finally:
        await sched.stop()


async def test_gives_up_after_max_retries(store, roles):
    from tests.conftest import FakeOllama

    fake = FakeOllama(fail_times=99, retryable=True)
    sched = Scheduler(store, roles, fake, poll_interval=0.01,
                      max_retries=2, backoff_base=1.0)
    await sched.start()
    try:
        j = mk_job(store, "fast", "small", "interactive")
        assert await wait_until(
            lambda: store.get_job(j["id"])["status"] == FAILED, timeout=8
        )
        assert store.get_job(j["id"])["attempts"] == 2
    finally:
        await sched.stop()


async def test_wait_for_returns_on_completion(store, roles, fake_ollama):
    sched = Scheduler(store, roles, fake_ollama, poll_interval=0.01)
    await sched.start()
    try:
        j = mk_job(store, "fast", "small", "interactive")
        assert await sched.wait_for(j["id"], timeout=3) is True
    finally:
        await sched.stop()


async def test_wait_for_times_out(store, roles):
    from tests.conftest import FakeOllama

    fake = FakeOllama(delay=2.0)
    sched = Scheduler(store, roles, fake, poll_interval=0.01)
    await sched.start()
    try:
        j = mk_job(store, "fast", "small", "interactive")
        assert await sched.wait_for(j["id"], timeout=0.2) is False
        assert store.get_job(j["id"])["status"] in ("queued", "running")
    finally:
        await sched.stop()


async def test_recovers_orphaned_running_jobs_on_start(store, roles, fake_ollama):
    """재시작 시 running 에 멈춘 잡을 이어서 처리한다."""
    j = mk_job(store, "fast", "small", "interactive")
    store.claim(j["id"])                     # 크래시 직전 상태
    sched = Scheduler(store, roles, fake_ollama, poll_interval=0.01)
    await sched.start()
    try:
        assert await wait_until(
            lambda: store.get_job(j["id"])["status"] == SUCCEEDED, timeout=3
        )
    finally:
        await sched.stop()
