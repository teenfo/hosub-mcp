"""서비스 제어 도구: restart_service(Medium), deploy_service(High)."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .. import service_ops
from ..context import AppContext
from ..jobs import JobRejection, Step
from ..policy import check_confirm


def register(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool()
    def restart_service(service_name: str, confirm: bool = False) -> dict:
        """등록된 서비스를 systemctl restart 로 재시작한다 (동기).

        service_name: 레지스트리에 등록된 서비스 이름.
        confirm: 위험도 Medium — 사용자 승인 후 true 로 재호출해야 실제 실행된다.
        """
        entry = ctx.registry.service(service_name)
        if entry is None:
            ctx.audit.log(
                tool="restart_service",
                params={"service_name": service_name},
                outcome="rejected",
                risk="medium",
            )
            return {
                "status": "rejected",
                "reason": f"등록되지 않은 서비스: {service_name!r}",
                "known_services": ctx.registry.service_names,
            }
        action = f"sudo systemctl restart {entry.unit}"
        denial = check_confirm("restart_service", confirm, action)
        if denial:
            ctx.audit.log(
                tool="restart_service",
                params={"service_name": service_name},
                confirm=False,
                risk="medium",
                outcome="approval_required",
            )
            return denial

        result = service_ops.restart_service(ctx.runner, entry)
        ctx.audit.log(
            tool="restart_service",
            params={"service_name": service_name},
            confirm=True,
            risk="medium",
            outcome=result["status"],
            result_summary=result.get("message"),
        )
        return result

    @mcp.tool()
    def deploy_service(service_name: str, confirm: bool = False) -> dict:
        """등록된 서비스의 배포 절차(git pull + 재빌드 + 재시작)를 백그라운드로 실행한다.

        service_name: 레지스트리에 deploy 블록이 정의된 서비스 이름.
        confirm: 위험도 High — 사용자 승인 후 true 로 재호출해야 실행된다.
        """
        entry = ctx.registry.service(service_name)
        if entry is None:
            ctx.audit.log(
                tool="deploy_service",
                params={"service_name": service_name},
                outcome="rejected",
                risk="high",
            )
            return {
                "status": "rejected",
                "reason": f"등록되지 않은 서비스: {service_name!r}",
                "known_services": ctx.registry.service_names,
            }
        if entry.deploy is None:
            ctx.audit.log(
                tool="deploy_service",
                params={"service_name": service_name},
                outcome="rejected",
                risk="high",
            )
            return {
                "status": "rejected",
                "reason": f"서비스 {service_name!r} 에 deploy 설정이 없습니다.",
            }

        spec = entry.deploy
        action = f"deploy {service_name}: " + " ; ".join(" ".join(s) for s in spec.steps)
        denial = check_confirm("deploy_service", confirm, action)
        if denial:
            ctx.audit.log(
                tool="deploy_service",
                params={"service_name": service_name},
                confirm=False,
                risk="high",
                outcome="approval_required",
            )
            return denial

        steps = [Step(argv=list(s), cwd=spec.workdir) for s in spec.steps]
        if spec.restart_after:
            steps.append(
                Step(argv=["sudo", "-n", "systemctl", "restart", entry.unit])
            )
        job = ctx.jobs.submit(
            kind="deploy_service",
            label=f"deploy {service_name}",
            steps=steps,
            timeout=spec.timeout_seconds,
        )
        if isinstance(job, JobRejection):
            ctx.audit.log(
                tool="deploy_service",
                params={"service_name": service_name},
                confirm=True,
                risk="high",
                outcome="rejected",
                result_summary=job.reason,
            )
            return job.to_dict()

        ctx.audit.log(
            tool="deploy_service",
            params={"service_name": service_name},
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
