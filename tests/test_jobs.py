import time

from src.jobs import JobManager, JobRejection, JobState, Step
from src.runner import RunResult
from tests.conftest import FakeRunner


def _wait_terminal(mgr, job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = mgr.get(job_id)
        if job and job.state in (
            JobState.SUCCEEDED,
            JobState.FAILED,
            JobState.TIMEOUT,
        ):
            return job
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish")


def test_success(audit):
    runner = FakeRunner(default=RunResult(0, "done", ""))
    mgr = JobManager(runner, audit)
    job = mgr.submit(kind="run_script", label="x", steps=[Step(["echo", "hi"])], timeout=5)
    assert not isinstance(job, JobRejection)
    done = _wait_terminal(mgr, job.id)
    assert done.state is JobState.SUCCEEDED
    assert done.exit_code == 0
    assert "done" in done.output_tail
    # 감사에 종결 행 기록됨
    assert any(r["job_id"] == job.id for r in audit.recent(10))


def test_multistep_abort_on_failure(audit):
    runner = FakeRunner(
        responses={
            ("step", "1"): RunResult(0, "ok1", ""),
            ("step", "2"): RunResult(3, "", "boom"),
            ("step", "3"): RunResult(0, "ok3", ""),
        }
    )
    mgr = JobManager(runner, audit)
    job = mgr.submit(
        kind="deploy_service",
        label="x",
        steps=[Step(["step", "1"]), Step(["step", "2"]), Step(["step", "3"])],
        timeout=5,
    )
    done = _wait_terminal(mgr, job.id)
    assert done.state is JobState.FAILED
    assert done.exit_code == 3
    # 스텝 3 은 실행되지 않음
    assert ("step", "3") not in [c[0] for c in runner.calls]


def test_timeout(audit):
    runner = FakeRunner(default=RunResult(-1, "", "", timed_out=True))
    mgr = JobManager(runner, audit)
    job = mgr.submit(kind="run_command", label="x", steps=[Step(["sleep", "999"])], timeout=1)
    done = _wait_terminal(mgr, job.id)
    assert done.state is JobState.TIMEOUT


def test_concurrency_rejection(audit):
    # 러너가 잠깐 블록하도록
    class SlowRunner:
        def run(self, argv, *, timeout, cwd=None, shell=False):
            time.sleep(0.3)
            return RunResult(0, "ok", "")

    mgr = JobManager(SlowRunner(), audit, max_concurrent=1, max_pending=2)
    jobs = [
        mgr.submit(kind="run_command", label=str(i), steps=[Step(["x"])], timeout=5)
        for i in range(2)
    ]
    assert all(not isinstance(j, JobRejection) for j in jobs)
    # 3번째는 거부
    third = mgr.submit(kind="run_command", label="3", steps=[Step(["x"])], timeout=5)
    assert isinstance(third, JobRejection)


def test_output_truncation(audit):
    runner = FakeRunner(default=RunResult(0, "A" * 20000, ""))
    mgr = JobManager(runner, audit)
    job = mgr.submit(kind="run_command", label="x", steps=[Step(["x"])], timeout=5)
    done = _wait_terminal(mgr, job.id)
    assert len(done.output_tail) <= 8192


def test_unknown_job_id(audit):
    mgr = JobManager(FakeRunner(), audit)
    assert mgr.get("nonexistent") is None
