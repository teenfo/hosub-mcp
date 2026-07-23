"""인프로세스 백그라운드 잡 매니저.

오래 걸리는 작업(백업, 배포, 스크립트, background=True 명령)은 즉시 job_id 를
반환하고 워커 스레드에서 실행한다. 잡 상태는 인메모리이며 서버 재시작 시
소실된다 — 영구 기록은 감사 DB 가 담당한다.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from .runner import CommandRunner

_OUTPUT_MAX = 8192
_HISTORY_MAX = 50


class JobState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"


_TERMINAL = {JobState.SUCCEEDED, JobState.FAILED, JobState.TIMEOUT}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Step:
    """실행할 단일 명령. shell=True 면 argv 는 ["bash","-lc",cmd] 형태."""

    argv: list[str]
    cwd: str | None = None
    shell: bool = False


@dataclass
class Job:
    id: str
    kind: str
    label: str
    state: JobState = JobState.PENDING
    created_at: datetime = field(default_factory=_utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    output_tail: str = ""
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "state": self.state.value,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "exit_code": self.exit_code,
            "output_tail": self.output_tail,
            "error": self.error,
        }


@dataclass
class JobRejection:
    reason: str

    def to_dict(self) -> dict:
        return {"status": "rejected", "reason": self.reason}


class JobManager:
    def __init__(
        self,
        runner: CommandRunner,
        audit=None,
        *,
        max_concurrent: int = 2,
        max_pending: int = 4,
    ) -> None:
        self._runner = runner
        self._audit = audit
        self._max_pending = max_pending
        self._executor = ThreadPoolExecutor(max_workers=max_concurrent)
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []  # 생성 순 (오래된 것부터)

    def submit(
        self,
        *,
        kind: str,
        label: str,
        steps: list[Step],
        timeout: int,
    ) -> Job | JobRejection:
        with self._lock:
            active = sum(
                1
                for jid in self._order
                if self._jobs[jid].state in (JobState.PENDING, JobState.RUNNING)
            )
            if active >= self._max_pending:
                return JobRejection(
                    reason=f"동시 실행/대기 잡이 한도({self._max_pending})에 도달했습니다. "
                    "잠시 후 다시 시도하세요."
                )
            job = Job(id=uuid.uuid4().hex[:12], kind=kind, label=label)
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._prune_locked()

        self._executor.submit(self._run_job, job, steps, timeout)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, limit: int = 10) -> list[Job]:
        limit = max(1, min(limit, _HISTORY_MAX))
        with self._lock:
            ids = list(reversed(self._order))[:limit]
            return [self._jobs[i] for i in ids]

    # --- 내부 ---
    def _prune_locked(self) -> None:
        while len(self._order) > _HISTORY_MAX:
            oldest = self._order[0]
            if self._jobs[oldest].state not in _TERMINAL:
                break  # 아직 실행 중인 건 남긴다
            self._order.pop(0)
            self._jobs.pop(oldest, None)

    def _run_job(self, job: Job, steps: list[Step], timeout: int) -> None:
        with self._lock:
            job.state = JobState.RUNNING
            job.started_at = _utcnow()

        buf: list[str] = []
        final = JobState.SUCCEEDED
        exit_code = 0
        error: str | None = None

        for idx, step in enumerate(steps):
            result = self._runner.run(
                step.argv, timeout=timeout, cwd=step.cwd, shell=step.shell
            )
            if result.combined_output:
                buf.append(result.combined_output)
            exit_code = result.exit_code
            if result.timed_out:
                final = JobState.TIMEOUT
                error = f"스텝 {idx + 1} 타임아웃"
                break
            if not result.ok:
                final = JobState.FAILED
                error = f"스텝 {idx + 1} 실패 (exit={result.exit_code})"
                break

        tail = "\n".join(buf)[-_OUTPUT_MAX:]
        with self._lock:
            job.state = final
            job.finished_at = _utcnow()
            job.exit_code = exit_code
            job.output_tail = tail
            job.error = error

        if self._audit is not None:
            self._audit.log(
                tool="__job_finished",
                params={"kind": job.kind, "label": job.label},
                outcome=final.value,
                result_summary=(error + " | " if error else "")
                + f"exit={exit_code} :: {tail[-200:]}",
                job_id=job.id,
            )
