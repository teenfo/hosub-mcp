"""인프로세스 백그라운드 잡 매니저.

오래 걸리는 작업(백업, 배포, 스크립트, background=True 명령)은 즉시 job_id 를
반환하고 워커 스레드에서 실행한다. 잡은 이 프로세스의 스레드·자식 프로세스로
돌기 때문에 **서버가 재시작되면 실행 중이던 잡은 함께 죽는다.**

state_path 가 주어지면 잡 목록을 런스테이트 파일(JSON)로 계속 떨궈 둔다.
두 가지 용도다:
  1. deploy/update.sh 가 active_jobs 를 읽어 실행 중 잡이 있으면 재시작을 미룬다.
     같은 파일의 rev(이 프로세스가 기동한 커밋)로 "재시작이 필요한 변경인가"도 판정한다.
  2. 재시작 뒤 새 프로세스가 이전 파일을 읽어, 끝나지 못한 잡을 lost_on_restart
     로 되살린다 — "모르는 잡(unknown_job)"과 "재시작에 죽은 잡"을 구분하기 위해.
"""

from __future__ import annotations

import json
import os
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
    # 이전 프로세스에서 pending/running 이던 잡 — 서버 재시작으로 죽었다.
    LOST = "lost_on_restart"


_TERMINAL = {JobState.SUCCEEDED, JobState.FAILED, JobState.TIMEOUT, JobState.LOST}


def _parse_ts(v) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v)
    except (TypeError, ValueError):
        return None


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
    # 이전 프로세스에서 복원된 잡이면 그 사실과 감지 시각
    restored: bool = False
    lost_at: datetime | None = None

    def to_dict(self) -> dict:
        d = {
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
        if self.restored:
            d["restored"] = True
        if self.lost_at:
            d["lost_at"] = self.lost_at.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        try:
            state = JobState(d.get("state"))
        except ValueError:
            state = JobState.LOST
        return cls(
            id=str(d["id"]),
            kind=str(d.get("kind", "")),
            label=str(d.get("label", "")),
            state=state,
            created_at=_parse_ts(d.get("created_at")) or _utcnow(),
            started_at=_parse_ts(d.get("started_at")),
            finished_at=_parse_ts(d.get("finished_at")),
            exit_code=d.get("exit_code"),
            output_tail=str(d.get("output_tail") or ""),
            error=d.get("error"),
            restored=True,
            lost_at=_parse_ts(d.get("lost_at")),
        )


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
        state_path: str | os.PathLike | None = None,
        rev: str | None = None,
    ) -> None:
        self._runner = runner
        self._audit = audit
        self._max_pending = max_pending
        self._executor = ThreadPoolExecutor(max_workers=max_concurrent)
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []  # 생성 순 (오래된 것부터)
        self._state_path = os.fspath(state_path) if state_path else None
        self._rev = rev
        self._started_at = _utcnow()
        if self._state_path:
            self._restore_previous()
            with self._lock:
                self._write_state_locked()

    def submit(
        self,
        *,
        kind: str,
        label: str,
        steps: list[Step],
        timeout: int,
    ) -> Job | JobRejection:
        with self._lock:
            if self._active_locked() >= self._max_pending:
                return JobRejection(
                    reason=f"동시 실행/대기 잡이 한도({self._max_pending})에 도달했습니다. "
                    "잠시 후 다시 시도하세요."
                )
            job = Job(id=uuid.uuid4().hex[:12], kind=kind, label=label)
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._prune_locked()
            self._write_state_locked()

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

    def active_count(self) -> int:
        with self._lock:
            return self._active_locked()

    # --- 내부 ---
    def _active_locked(self) -> int:
        return sum(
            1
            for jid in self._order
            if self._jobs[jid].state in (JobState.PENDING, JobState.RUNNING)
        )

    def _restore_previous(self) -> None:
        """이전 프로세스의 런스테이트에서 잡 이력을 되살린다.

        끝나지 못한 잡(pending/running)은 lost_on_restart 로 바꾼다. 실패해도
        기동을 막지 않는다 — 이력 복원은 편의 기능이다.
        """
        try:
            with open(self._state_path, encoding="utf-8") as f:
                prev = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(prev, dict) or prev.get("pid") == os.getpid():
            return
        now = _utcnow()
        lost = 0
        for d in prev.get("jobs") or []:
            try:
                job = Job.from_dict(d)
            except (KeyError, TypeError):
                continue
            if job.id in self._jobs:
                continue
            if job.state in (JobState.PENDING, JobState.RUNNING):
                job.state = JobState.LOST
                job.lost_at = now
                job.finished_at = job.finished_at or now
                job.error = (
                    f"서버 재시작으로 소실됨 (이전 pid={prev.get('pid')}, "
                    f"감지 {now.isoformat()})"
                )
                lost += 1
            self._jobs[job.id] = job
            self._order.append(job.id)
        self._order.sort(key=lambda i: self._jobs[i].created_at)
        self._prune_locked()
        if lost and self._audit is not None:
            self._audit.log(
                tool="__jobs_lost_on_restart",
                params={"previous_pid": prev.get("pid")},
                outcome="lost_on_restart",
                result_summary=f"{lost}개 잡이 재시작으로 소실",
            )

    def _write_state_locked(self) -> None:
        """런스테이트를 원자적으로 기록한다 (tmp → rename). 호출자는 _lock 보유."""
        if not self._state_path:
            return
        data = {
            "pid": os.getpid(),
            "rev": self._rev,
            "started_at": self._started_at.isoformat(),
            "updated_at": _utcnow().isoformat(),
            "active_jobs": self._active_locked(),
            "jobs": [self._jobs[i].to_dict() for i in self._order],
        }
        tmp = f"{self._state_path}.tmp"
        try:
            os.makedirs(os.path.dirname(self._state_path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self._state_path)
        except OSError:
            pass  # 런스테이트 기록 실패가 잡 실행을 막아선 안 된다

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
            self._write_state_locked()

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
            self._write_state_locked()

        if self._audit is not None:
            self._audit.log(
                tool="__job_finished",
                params={"kind": job.kind, "label": job.label},
                outcome=final.value,
                result_summary=(error + " | " if error else "")
                + f"exit={exit_code} :: {tail[-200:]}",
                job_id=job.id,
            )
