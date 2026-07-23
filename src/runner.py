"""명령 실행 추상화 계층.

모든 OS 명령은 이 인터페이스를 통과한다. 테스트에서는 FakeRunner 를 주입해
systemd/journalctl 이 없는 개발 환경에서도 도구 로직을 검증할 수 있다.
"""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from typing import Protocol


@dataclass
class RunResult:
    """명령 실행 결과."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def combined_output(self) -> str:
        parts = []
        if self.stdout:
            parts.append(self.stdout)
        if self.stderr:
            parts.append(self.stderr)
        return "\n".join(parts).strip()


class CommandRunner(Protocol):
    """명령 실행기 프로토콜."""

    def run(
        self,
        argv: list[str],
        *,
        timeout: int,
        cwd: str | None = None,
        shell: bool = False,
    ) -> RunResult:
        ...


class SubprocessRunner:
    """실제 subprocess 로 명령을 실행하는 러너.

    shell=False: argv 리스트를 그대로 실행 (화이트리스트 도구용, 셸 확장 없음).
    shell=True:  argv 는 ["bash", "-lc", "<command>"] 형태로 넘어오며 셸 확장 허용
                 (run_command 전용). 어느 경우든 프로세스 그룹을 새로 만들어
                 타임아웃 시 자식까지 정리한다.
    """

    def run(
        self,
        argv: list[str],
        *,
        timeout: int,
        cwd: str | None = None,
        shell: bool = False,
    ) -> RunResult:
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                text=True,
                start_new_session=True,  # 새 프로세스 그룹 → killpg 로 자식까지 종료
            )
        except FileNotFoundError as exc:
            return RunResult(exit_code=127, stdout="", stderr=str(exc))
        except OSError as exc:
            return RunResult(exit_code=126, stdout="", stderr=str(exc))

        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return RunResult(exit_code=proc.returncode, stdout=stdout, stderr=stderr)
        except subprocess.TimeoutExpired:
            self._kill_group(proc)
            # 죽인 뒤의 재수거에도 반드시 timeout 을 건다. 명령이 setsid/daemon 으로
            # double-fork 한 손자 프로세스는 우리 프로세스 그룹을 벗어나 killpg 로
            # 죽지 않으면서 stdout/stderr 파이프의 write-end 를 계속 쥐고 있다.
            # timeout 없는 communicate() 는 이 파이프의 EOF 를 영원히 기다려
            # 워커 스레드를 영구히 잠근다(= run_command 교착의 원인).
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""  # 탈출한 자식이 파이프를 쥔 상태 — 수거 포기
            return RunResult(
                exit_code=proc.returncode if proc.returncode is not None else -1,
                stdout=stdout or "",
                stderr=(stderr or "") + f"\n[timeout after {timeout}s]",
                timed_out=True,
            )

    @staticmethod
    def _kill_group(proc: subprocess.Popen) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
