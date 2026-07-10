"""임의 명령 실행 도구: run_command (High).

서버 전체 제어 계층. 화이트리스트와 무관하게 임의 셸 명령을 실행하므로,
방어선은 confirm 게이트 + 전체 감사 로그뿐이다. 명령 자체는 막지 않는다.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from ..context import AppContext
from ..jobs import JobRejection, Step
from ..policy import check_confirm

_MAX_TIMEOUT = 3600
_SYNC_OUTPUT_MAX = 16384


def _build_argv(command: str, use_sudo: bool) -> list[str]:
    inner = f"sudo -n bash -lc {_shq(command)}" if use_sudo else command
    return ["bash", "-lc", inner]


def _shq(s: str) -> str:
    """단일 인용부호로 안전하게 감싼다."""
    return "'" + s.replace("'", "'\\''") + "'"


def register(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool()
    def run_command(
        command: str,
        use_sudo: bool = False,
        timeout: int = 300,
        workdir: str | None = None,
        background: bool = False,
        confirm: bool = False,
    ) -> dict:
        """임의의 셸 명령을 서버에서 실행한다 (서버 전체 제어).

        command: 실행할 셸 명령 (bash -lc 로 해석되어 파이프/리다이렉트 등 허용).
        use_sudo: true 면 sudo 로 실행 (root 권한 필요 작업).
        timeout: 초 단위 제한 (기본 300, 최대 3600).
        workdir: 작업 디렉터리 (선택).
        background: true 면 즉시 job_id 를 반환하고 백그라운드로 실행.
                    긴 작업(빌드, 대용량 다운로드 등)에 사용.
        confirm: 위험도 High — 사용자 승인 후 true 로 재호출해야 실행된다.
        """
        timeout = max(1, min(int(timeout), _MAX_TIMEOUT))
        action = ("[sudo] " if use_sudo else "") + command
        denial = check_confirm("run_command", confirm, action)
        if denial:
            ctx.audit.log(
                tool="run_command",
                params={"command": command, "use_sudo": use_sudo, "background": background},
                confirm=False,
                risk="high",
                outcome="approval_required",
            )
            return denial

        argv = _build_argv(command, use_sudo)

        if background:
            job = ctx.jobs.submit(
                kind="run_command",
                label=(command[:60] + "…") if len(command) > 60 else command,
                steps=[Step(argv=argv, cwd=workdir, shell=True)],
                timeout=timeout,
            )
            if isinstance(job, JobRejection):
                ctx.audit.log(
                    tool="run_command",
                    params={"command": command, "use_sudo": use_sudo},
                    confirm=True,
                    risk="high",
                    outcome="rejected",
                    result_summary=job.reason,
                )
                return job.to_dict()
            ctx.audit.log(
                tool="run_command",
                params={"command": command, "use_sudo": use_sudo, "background": True},
                confirm=True,
                risk="high",
                outcome="job_started",
                job_id=job.id,
            )
            return {
                "status": "started",
                "job_id": job.id,
                "hint": "get_job_status(job_id) 로 진행 상황을 확인하세요.",
            }

        result = ctx.runner.run(argv, timeout=timeout, cwd=workdir, shell=True)
        output = result.combined_output[-_SYNC_OUTPUT_MAX:]
        outcome = "timeout" if result.timed_out else ("ok" if result.ok else "error")
        ctx.audit.log(
            tool="run_command",
            params={"command": command, "use_sudo": use_sudo},
            confirm=True,
            risk="high",
            outcome=outcome,
            result_summary=f"exit={result.exit_code} :: {output[-300:]}",
        )
        return {
            "status": outcome,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "output": output,
        }
