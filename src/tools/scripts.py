"""스크립트 도구: run_backup(Medium), run_script(High).

둘 다 화이트리스트 레지스트리에 등록된 스크립트만 백그라운드 잡으로 실행한다.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from ..context import AppContext
from ..jobs import JobRejection, Step
from ..policy import check_confirm
from ..registry import ScriptEntry


def _launch_script(ctx: AppContext, tool: str, risk: str, entry: ScriptEntry) -> dict:
    job = ctx.jobs.submit(
        kind=tool,
        label=f"script {entry.name}",
        steps=[Step(argv=[entry.path])],
        timeout=entry.timeout_seconds,
    )
    if isinstance(job, JobRejection):
        ctx.audit.log(
            tool=tool,
            params={"script_name": entry.name},
            confirm=True,
            risk=risk,
            outcome="rejected",
            result_summary=job.reason,
        )
        return job.to_dict()
    ctx.audit.log(
        tool=tool,
        params={"script_name": entry.name},
        confirm=True,
        risk=risk,
        outcome="job_started",
        job_id=job.id,
    )
    return {
        "status": "started",
        "job_id": job.id,
        "script": entry.name,
        "hint": "get_job_status(job_id) 로 진행 상황을 확인하세요.",
    }


def register(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool()
    def run_backup(confirm: bool = False) -> dict:
        """레지스트리에 지정된 백업 스크립트를 백그라운드로 실행한다.

        confirm: 위험도 Medium — 사용자 승인 후 true 로 재호출해야 실행된다.
        """
        entry = ctx.registry.backup()
        if entry is None:
            ctx.audit.log(tool="run_backup", outcome="rejected", risk="medium")
            return {
                "status": "rejected",
                "reason": "레지스트리에 backup_script 가 설정되어 있지 않습니다.",
            }
        action = f"백업 스크립트 실행: {entry.path}"
        denial = check_confirm("run_backup", confirm, action)
        if denial:
            ctx.audit.log(
                tool="run_backup",
                confirm=False,
                risk="medium",
                outcome="approval_required",
            )
            return denial
        return _launch_script(ctx, "run_backup", "medium", entry)

    @mcp.tool()
    def run_script(script_name: str, confirm: bool = False) -> dict:
        """화이트리스트에 등록된 스크립트만 백그라운드로 실행한다.

        script_name: config/registry.yaml 의 scripts 에 등록된 이름 (자유 텍스트 불가).
        confirm: 위험도 High — 사용자 승인 후 true 로 재호출해야 실행된다.
        """
        entry = ctx.registry.script(script_name)
        if entry is None:
            ctx.audit.log(
                tool="run_script",
                params={"script_name": script_name},
                outcome="rejected",
                risk="high",
            )
            return {
                "status": "rejected",
                "reason": f"등록되지 않은 스크립트: {script_name!r}",
                "known_scripts": ctx.registry.script_names,
            }
        action = f"스크립트 실행: {entry.path}"
        denial = check_confirm("run_script", confirm, action)
        if denial:
            ctx.audit.log(
                tool="run_script",
                params={"script_name": script_name},
                confirm=False,
                risk="high",
                outcome="approval_required",
            )
            return denial
        return _launch_script(ctx, "run_script", "high", entry)
