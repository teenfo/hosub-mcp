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
- **미설치 모델 자동 요청**: 새 소비자가 아직 맥에 없는 모델을 쓰면, 그 잡을
  레인에서 건너뛰고(다른 잡을 막지 않는다) 설치 요청을 만든다. 사용자가
  대시보드에서 승인하면 게이트웨이가 /api/pull 로 설치하고, 대기하던 잡이
  자동으로 이어서 실행된다.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .config import RoleConfig, model_matches
from .notify import SlackNotifier
from .ollama import BackendError, OllamaClient
from .store import (
    FAILED,
    MR_FAILED,
    MR_PENDING,
    MR_PULLING,
    MR_READY,
    MR_REJECTED,
    QUEUED,
    SUCCEEDED,
    Store,
)

log = logging.getLogger("llmgw.scheduler")

LANES = ("interactive", "batch")

# pending·approved·pulling 인 모델을 기다리는 잡은 큐에 그대로 둔다(곧 설치될 수 있다).
# 반면 아래 상태면 더 기다려도 소용없다 — 잡을 즉시 실패시켜 호출자에게 알린다.
DEAD_STATUSES = {MR_REJECTED, MR_FAILED}


# model_matches 는 config 로 옮겼다(역할 조회·삭제 차단도 같은 규칙을 써야 한다).
# 기존 임포트 경로를 깨지 않도록 여기서 재수출한다.
__all__ = ["LANES", "DEAD_STATUSES", "Scheduler", "model_matches"]


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
        auto_install: bool = True,
        models_refresh: float = 30.0,
        retention_days: int = 30,
        notifier: SlackNotifier | None = None,
    ) -> None:
        self.store = store
        self.roles = roles
        self.client = client
        self.max_retries = max_retries
        self.starvation_seconds = starvation_seconds
        self.poll_interval = poll_interval
        self.backoff_base = backoff_base
        self.auto_install = auto_install
        self.models_refresh = models_refresh
        self.retention_days = retention_days
        # 사람이 개입해야 하는 순간(승인 대기·백엔드 다운)에만 알린다.
        # 웹훅이 없으면 no-op 이라 호출부가 분기할 필요가 없다.
        self.notifier = notifier or SlackNotifier()

        # 잡 완료 알림 (동기 대기 중인 요청을 깨운다)
        self._events: dict[str, asyncio.Event] = {}
        # 현재 실행 중: lane -> (job_id, model, size_gb)
        self._running: dict[str, tuple[str, str, float]] = {}
        # 마지막으로 사용한 모델 (모델 친화 판단용)
        self._last_model: str | None = None
        # 맥이 보유한 모델. None = 아직 모름 → 아무 잡도 건너뛰지 않는다
        # (백엔드가 잠깐 죽었다고 큐 전체를 멈추면 안 되기 때문)
        self._available: list[str] | None = None
        # 마지막으로 읽은 모델 상세(크기·양자화). 백엔드가 죽어도 직전 값을 남긴다.
        self._models_detail: list[dict] = []
        # 설치 대기 중이라 건너뛴 모델들 (상태 표시용)
        self._pulling: dict[str, int] = {}
        # _triage_missing(동기)이 만든 알림 대기열 — _models_loop 에서 비운다
        self._pending_notices: list[dict] = []
        self._tasks: list[asyncio.Task] = []
        self._stopping = False

    # ---------- 수명주기 ----------
    async def start(self) -> None:
        recovered = self.store.recover_running()
        if recovered:
            log.info("크래시 복구: running %d건을 queued 로 되돌림", recovered)
        self._stopping = False
        if self.auto_install:
            # 레인을 열기 전에 보유 목록을 먼저 읽는다. 그러지 않으면 기동 직후
            # 미설치 모델 잡이 낙관적으로 실행돼 404 로 죽는다(설치 요청 대신).
            await self._refresh_models()
        self._purge_once()
        self._tasks = [asyncio.create_task(self._lane_loop(l)) for l in LANES]
        self._tasks.append(asyncio.create_task(self._retention_loop()))
        if self.auto_install:
            self._tasks.append(asyncio.create_task(self._models_loop()))

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

    # ---------- 보존 ----------
    def _purge_once(self) -> None:
        if self.retention_days <= 0:
            return
        try:
            n = self.store.purge_old(self.retention_days)
        except Exception:
            log.exception("보존 정리 실패")
            return
        if n["jobs"] or n["usage"]:
            log.info("보존 정리: 잡 %d건, 사용량 %d건 삭제(%d일 초과)",
                     n["jobs"], n["usage"], self.retention_days)

    async def _retention_loop(self) -> None:
        """하루 한 번 정리. 안 하면 /data 가 계속 커진다."""
        while not self._stopping:
            await asyncio.sleep(24 * 3600)
            if self._stopping:
                return
            self._purge_once()

    # ---------- 대기/알림 ----------
    def event_for(self, job_id: str) -> asyncio.Event:
        ev = self._events.get(job_id)
        if ev is None:
            ev = asyncio.Event()
            self._events[job_id] = ev
        return ev

    def notify_cancelled(self, job_id: str) -> None:
        """외부(API)에서 잡을 취소했을 때 대기 중인 요청을 깨운다."""
        self._notify(job_id)

    def model_ready(self, model: str) -> bool:
        """맥에 이 모델이 있나(공개용). 목록을 모르면 낙관적으로 True."""
        return self._model_ready(model)

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

    @property
    def available_models(self) -> list[str] | None:
        return self._available

    @property
    def models_detail(self) -> list[dict]:
        return list(self._models_detail)

    def pulling_snapshot(self) -> dict[str, int]:
        return dict(self._pulling)

    def _model_ready(self, model: str) -> bool:
        """맥에 이 모델이 있나. 목록을 모르면(백엔드 다운) 낙관적으로 True."""
        if self._available is None:
            return True
        return any(model_matches(model, m) for m in self._available)

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

        # 예산을 통째로 비워도 안 들어가는 모델은 영원히 못 돈다. 조용히 큐에
        # 쌓아두지 말고 이유를 붙여 실패시킨다(설정 실수를 빨리 드러낸다).
        runnable = []
        for j in candidates:
            size = self.roles.model_size_gb(j["model"])
            if size > self.roles.mem_budget_gb:
                self._fail_job(j, (
                    f"모델 '{j['model']}' (추정 {size}GB)이 메모리 예산 "
                    f"{self.roles.mem_budget_gb}GB 를 넘어 실행할 수 없습니다. "
                    "MEM_BUDGET_GB 를 올리거나 더 작은 모델을 쓰세요."
                ))
            else:
                runnable.append(j)

        # 아직 설치되지 않은 모델의 잡은 건너뛴다 — 레인을 막지 않는 것이 핵심.
        # (설치 요청 생성/거부 처리는 _models_loop 이 담당)
        candidates = [j for j in runnable if self._model_ready(j["model"])]
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

    # ---------- 모델 자동 설치 ----------
    async def _models_loop(self) -> None:
        """보유 모델 목록을 갱신하고, 승인된 설치 요청을 처리한다."""
        last_refresh = 0.0
        while not self._stopping:
            try:
                if time.monotonic() - last_refresh >= self.models_refresh:
                    last_refresh = time.monotonic()
                    await self._refresh_models()
                    self._triage_missing()
                    # _triage_missing 은 동기라 알림을 모아 뒀다 여기서 보낸다
                    while self._pending_notices:
                        await self.notifier.model_approval_needed(
                            self._pending_notices.pop(0))
                if await self._pull_next():
                    last_refresh = 0.0  # 설치 직후 목록을 즉시 다시 읽는다
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("모델 관리 루프 오류")
            await asyncio.sleep(min(2.0, self.models_refresh))

    async def _refresh_models(self) -> None:
        try:
            detail = await self.client.tags_detailed()
        except BackendError as exc:
            # 목록을 모르면 "없다"고 단정하지 않는다. 백엔드가 잠깐 죽었다고
            # 큐 전체를 설치 대기로 돌려버리면 안 되기 때문.
            log.warning("모델 목록 조회 실패 — 자동 설치 판단 보류: %s", exc)
            self._available = None
            # 맥이 잠들거나 정전으로 꺼지면 야간 배치가 조용히 실패한다.
            # 전이 시점에만 한 번 알린다(30초마다 알리면 아무도 안 본다).
            await self.notifier.backend_offline(self.client.base_url, str(exc))
        else:
            self._available = [d["name"] for d in detail]
            self._models_detail = detail
            # 크기를 몰라 20GB 로 가정하는 일이 줄어든다(config.model_size_gb 4순위).
            self.roles.set_measured_sizes(
                {d["name"]: d["size_gb"] for d in detail if d.get("size_gb")}
            )
            await self.notifier.backend_online(self.client.base_url)

    def _triage_missing(self) -> None:
        """대기 잡 중 모델이 없는 것들을 설치 요청으로 올리거나 실패시킨다."""
        if self._available is None:
            return
        queued = self.store.list_jobs(status=QUEUED, limit=200)
        if not queued:
            return
        statuses = self.store.model_request_statuses()
        for job in queued:
            model = job["model"]
            if self._model_ready(model):
                continue
            if self.roles.model_size_gb(model) > self.roles.mem_budget_gb:
                continue  # 설치해도 못 돈다 — _pick 이 실패 처리한다
            st = statuses.get(model)
            if st in DEAD_STATUSES:
                reason = "설치가 거부됨" if st == MR_REJECTED else "설치 실패"
                self._fail_job(job, f"모델 '{model}' 을 쓸 수 없습니다 ({reason})")
                continue
            if st is None or st == MR_READY:
                # ready 였는데 사라졌다면 다시 승인을 받는다(맥에서 삭제된 경우)
                req = self.store.ensure_model_request(
                    model,
                    requested_by=job["service"],
                    roles=self.roles.roles_using(model),
                    est_size_gb=self.roles.model_size_gb(model),
                )
                statuses[model] = req["status"]
                if req["status"] == MR_PENDING:
                    self._pending_notices.append(req)
                log.warning(
                    "미설치 모델 '%s' (요청: %s, 역할: %s) — 승인 대기",
                    model, job["service"], ", ".join(req["roles"]) or "-",
                )

    async def _pull_next(self) -> bool:
        """승인된 요청 하나를 실제로 설치한다. 처리했으면 True."""
        req = self.store.claim_approved_model()
        if req is None:
            return False
        model = req["model"]
        log.info("모델 설치 시작: %s (~%sGB)", model, req.get("est_size_gb"))
        self._pulling[model] = 0
        last = -10

        def on_progress(pct: int) -> None:
            nonlocal last
            if pct == last or (pct < 100 and pct - last < 5):
                return  # DB 쓰기 폭주 방지 — 5%p 단위로만 기록
            last = pct
            self._pulling[model] = pct
            self.store.set_model_request_status(model, MR_PULLING, progress=pct)

        try:
            await self.client.pull(model, progress_cb=on_progress)
        except BackendError as exc:
            log.error("모델 설치 실패 %s: %s", model, exc)
            self.store.set_model_request_status(model, MR_FAILED, error=str(exc))
            await self.notifier.model_install_finished(model, ok=False, error=str(exc))
        except Exception as exc:
            log.exception("모델 설치 중 예외 %s", model)
            self.store.set_model_request_status(model, MR_FAILED, error=str(exc))
            await self.notifier.model_install_finished(model, ok=False, error=str(exc))
        else:
            self.store.set_model_request_status(model, MR_READY, progress=100)
            log.info("모델 설치 완료: %s — 대기하던 잡이 이어서 실행됩니다", model)
            await self.notifier.model_install_finished(model, ok=True)
        finally:
            self._pulling.pop(model, None)
        return True

    def _fail_job(self, job: dict, error: str) -> None:
        if not self.store.fail_if_queued(job["id"], error):
            return  # 그 사이 실행되었거나 취소됨
        self.store.record_usage(
            service=job["service"], role=job["role"], model=job["model"],
            eval_count=None, duration_ms=None, status=FAILED,
        )
        self._notify(job["id"])

    async def _run(self, lane: str, job: dict) -> None:
        model = job["model"]
        size = self.roles.model_size_gb(model)
        self._running[lane] = (job["id"], model, size)
        self._last_model = model
        started = time.monotonic()
        # 잡은 자기완결형이다 — 생성 시점의 options/timeout 을 쓴다.
        # 역할을 실행 시점에 다시 읽으면 큐에 있던 잡이 "옛 모델 + 새 옵션" 으로
        # 돈다(모델은 이미 job 에 스냅샷돼 있으므로). 스냅샷이 없는 옛 행만
        # 현재 역할로 폴백한다.
        role = self.roles.role(job["role"])
        options = job.get("options")
        if options is None and role is not None:
            options = role.options
        timeout = job.get("timeout_s")
        if timeout is None:
            timeout = role.timeout if role else 180
        try:
            result = await self.client.generate(
                model=model,
                prompt=job["prompt"],
                system=job["system"],
                options=options,
                timeout=timeout,
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
        # attempts 는 claim 에서 이미 +1 된 값이라 **최초 실행을 포함**한다.
        # max_retries 를 "재시도 횟수"로 읽으려면 최초 1회를 더해야 한다
        # (MAX_RETRIES=3 → 최초 + 재시도 3회, 백오프 2→4→8초).
        attempts = job["attempts"]
        if exc.retryable and attempts <= self.max_retries:
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
