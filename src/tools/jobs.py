"""잡 조회 도구: get_job_status, list_jobs."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from ..context import AppContext


def register(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool()
    def get_job_status(job_id: str) -> dict:
        """백그라운드 잡의 상태와 출력 일부를 조회한다.

        job_id: run_script / run_backup / deploy_service / run_command(background)
                호출 시 반환된 잡 식별자.
        """
        job = ctx.jobs.get(job_id)
        if job is None:
            return {
                "status": "unknown_job",
                "job_id": job_id,
                "note": "해당 잡을 찾을 수 없습니다. 잡 상태는 인메모리이며 "
                "서버 재시작 시 소실됩니다. 영구 기록은 감사 로그를 참조하세요.",
            }
        return {"status": "ok", "job": job.to_dict()}

    @mcp.tool()
    def list_jobs(limit: int = 10) -> dict:
        """최근 백그라운드 잡 목록을 최신순으로 조회한다 (기본 10개)."""
        jobs = ctx.jobs.list(limit)
        return {"jobs": [j.to_dict() for j in jobs]}
