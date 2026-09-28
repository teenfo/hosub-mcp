"""잡 조회 도구: get_job_status, list_jobs."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from ..context import AppContext
from ..jobs import JobState


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
                "note": "해당 잡을 찾을 수 없습니다. 재시작에 죽은 잡은 lost_on_restart 로 "
                "남으므로, 이 응답은 잘못된 id 이거나 이력 한도(50개)를 넘어 밀려난 "
                "경우입니다. 영구 기록은 감사 로그를 참조하세요.",
            }
        if job.state is JobState.LOST:
            return {
                "status": "lost_on_restart",
                "job": job.to_dict(),
                "lost_at": job.lost_at.isoformat() if job.lost_at else None,
                "note": "MCP 서버가 재시작되면서 이 잡(과 자식 프로세스)이 중단됐습니다. "
                "결과가 필요하면 다시 실행하세요. 30분 넘는 작업은 run_unit 으로 "
                "서비스 수명과 분리해 돌리는 것을 권합니다.",
            }
        return {"status": "ok", "job": job.to_dict()}

    @mcp.tool()
    def list_jobs(limit: int = 10) -> dict:
        """최근 백그라운드 잡 목록을 최신순으로 조회한다 (기본 10개)."""
        jobs = ctx.jobs.list(limit)
        return {"jobs": [j.to_dict() for j in jobs]}
