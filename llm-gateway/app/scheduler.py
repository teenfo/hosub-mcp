"""2레인 스케줄러 — 게이트웨이의 핵심.

설계 근거(docs/requests/llm-gateway-service.md 5절):

- **레인 분리**: 동시성 1 하나만 두면 3분짜리 batch 잡이 2초짜리 대화형 요청을
  막는다(head-of-line blocking). 우선순위는 큐 순서만 바꿀 뿐 실행 중인 잡을
  비우지 못하므로, interactive/batch 레인을 나누고 각각 1개씩 돌린다.
- **메모리 예산 가드**: 두 레인이 동시에 큰 모델을 잡으면 맥 메모리를 넘는다.
  실행 중 모델 크기 합이 예산을 넘으면 시작을 미룬다.
- **모델 친화**: 같은 레인에서 현재 로드된 모델과 같은 모델의 잡을 우선 처리해
  모델 전환(재로드) 횟수를 줄인다. 단 오래 기다린 잡은 무조건 우선(기아 방지).
- **재시도**: 연결 실패·타임아웃은 지수 백오프로 재시도. 맥이 재부팅 중이어도
  잡이 살아남는다.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .config import RoleConfig
from .ollama import BackendError, OllamaClient
from .store import FAILED, SUCCEEDED, Store

log = logging.getLogger("llmgw.scheduler")

LANES = ("interactive", "batch")


class Scheduler:
    def __init__(
        self,
        store: Store,
        roles: RoleConfig,
        client: OllamaClient,
        *,
        max_retries: int = 3,
        starvation_seconds: int = 300,
        poll_interval: float = 0.5,
        backoff_base: float = 2.0,
    ) -> None:
        self.store = store
        self.roles = roles
        self.client = client
        self.max_retries = max_retries
        self.starvation_seconds = starvation_seconds
        self.poll_interval = poll_interval
        self.backoff_base = backoff_base

        # 잡 완료 알림 (동기 대기 중인 요청을 깨운다)
        self._events: dict[str, asyncio.Event] = {}
        # 현재 실행 중: lane -> (job_id, model, size_gb)
        self._running: dict[str, tuple[str, str, float]] = {}
        # 마지막으로 사용한 모델 (모델 친화 판단용)
        self._last_model: str | None = None
        self._tasks: list[asyncio.Task] = []
        self._stopping = False

    # ---------- 수명주기 ----------
    async def start(self) -> None:
        recovered = self.store.recover_running()
        if recovered:
            log.info("크래시 복구: running %d건을 queued 로 되돌림", recovered)
        self._stopping = False
        self._tasks = [asyncio.create_task(self._lane_loop(l)) for l in LANES]

    async def stop(self) -> None:
        self._stopping = True
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._tasks = []

    # ---------- 대기/알림 ----------
    def event_for(self, job_id: str) -> asyncio.Event:
        ev = self._events.get(job_id)
        if ev is None:
            ev = asyncio.Event()
            self._events[job_id] = ev
        return ev

    def _notify(self, job_id: str) -> None:
        ev = self._events.pop(job_id, None)
        if ev:
            ev.set()

    async def wait_for(self, job_id: str, timeout: float) -> bool:
        """잡 완료를 timeout 까지 기다린다. 완료되면 True."""
        if timeout <= 0:
            return False
        job = self.store.get_job(job_id)
        if job and job["status"] in (SUCCEEDED, FAILED, "cancelled"):
            return True
        ev = self.event_for(job_id)
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ---------- 상태 ----------
    def running_snapshot(self) -> dict:
        return {
            lane: {"job_id": v[0], "model": v[1]}
            for lane, v in self._running.items()
        }

    @property
    def loaded_model(self) -> str | None:
        return self._last_model

    def _used_memory_gb(self, exclude_lane: str | None = None) -> float:
        return sum(
            size for lane, (_jid, _m, size) in self._running.items()
            if lane != exclude_lane
        )

    # ---------- 선택 로직 ----------
    def _pick(self, lane: str) -> dict | None:
        """레인에서 다음 잡을 고른다. 모델 친화 + 기아 방지 + 메모리 예산."""
        candidates = self.store.queued_jobs(lane)
        if not candidates:
            return None

        budget_left = self.roles.mem_budget_gb - self._used_memory_gb(exclude_lane=lane)
        now = time.time()

        def fits(job: dict) -> bool:
            return self.roles.model_size_gb(job["model"]) <= budget_left

        affordable = [j for j in candidates if fits(j)]
        if not affordable:
            return None

        # 기아 방지: 너무 오래 기다린 잡이 있으면 무조건 그것부터
        def waited(job: dict) -> float:
            try:
                from datetime import datetime
                created = datetime.fromisoformat(job["created_at"])
                return now - created.timestamp()
            except Exception:
                return 0.0

        starving = [j for j in affordable if waited(j) >= self.starvation_seconds]
        if starving:
            return starving[0]  # 이미 priority/created_at 정렬됨

        # 모델 친화: 현재 로드된 모델과 같은 모델을 우선
        if self._last_model:
            same = [j for j in affordable if j["model"] == self._last_model]
            if same:
                return same[0]
        return affordable[0]

    # ---------- 실행 루프 ----------
    async def _lane_loop(self, lane: str) -> None:
        while not self._stopping:
            try:
                job = self._pick(lane)
                if job is None:
                    await asyncio.sleep(self.poll_interval)
                    continue
                if not self.store.claim(job["id"]):
                    continue  # 다른 루프가 가져감 (레인이 달라도 방어적으로)
                # claim 이 attempts 를 증가시키므로 최신 상태를 다시 읽는다.
                # (낡은 dict 를 쓰면 재시도 횟수가 한 번 더 도는 버그)
                fresh = self.store.get_job(job["id"])
                await self._run(lane, fresh or job)
            except asyncio.CancelledError:
                raise
            except Exception:  # 루프는 어떤 경우에도 죽지 않는다
                log.exception("레인 %s 루프 오류", lane)
                await asyncio.sleep(1.0)

    async def _run(self, lane: str, job: dict) -> None:
        role = self.roles.role(job["role"])
        model = job["model"]
        size = self.roles.model_size_gb(model)
        self._running[lane] = (job["id"], model, size)
        self._last_model = model
        started = time.monotonic()
        try:
            result = await self.client.generate(
                model=model,
                prompt=job["prompt"],
                system=job["system"],
                options=(role.options if role else None),
                timeout=(role.timeout if role else 180),
            )
        except BackendError as exc:
            await self._handle_failure(job, exc)
        except Exception as exc:  # 예상 못한 오류도 잡 단위로 격리
            await self._handle_failure(job, BackendError(str(exc), retryable=False))
        else:
            self.store.finish(job["id"], status=SUCCEEDED, response=result.response)
            self.store.record_usage(
                service=job["service"], role=job["role"], model=model,
                eval_count=result.eval_count,
                duration_ms=result.duration_ms or round((time.monotonic() - started) * 1000),
                status=SUCCEEDED,
            )
            self._notify(job["id"])
        finally:
            self._running.pop(lane, None)

    async def _handle_failure(self, job: dict, exc: BackendError) -> None:
        attempts = job["attempts"]  # claim 에서 이미 +1 된 값
        if exc.retryable and attempts < self.max_retries:
            delay = self.backoff_base ** attempts
            log.warning(
                "잡 %s 실패(%s) — %.0fs 후 재시도 (%d/%d)",
                job["id"], exc, delay, attempts, self.max_retries,
            )
            self.store.requeue(job["id"])
            await asyncio.sleep(delay)
            return
        log.error("잡 %s 최종 실패: %s", job["id"], exc)
        self.store.finish(job["id"], status=FAILED, error=str(exc))
        self.store.record_usage(
            service=job["service"], role=job["role"], model=job["model"],
            eval_count=None, duration_ms=None, status=FAILED,
        )
        self._notify(job["id"])
