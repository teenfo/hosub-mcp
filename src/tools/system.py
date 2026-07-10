"""조회 계층 도구: get_system_status, list_services, read_service_logs."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .. import service_ops, sysinfo
from ..context import AppContext


def register(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool()
    def get_system_status() -> dict:
        """서버의 CPU/메모리/스왑/디스크/업타임과 메모리 상위 프로세스를 조회한다."""
        data = sysinfo.collect_status()
        ctx.audit.log(tool="get_system_status", outcome="ok", risk="low")
        return data

    @mcp.tool()
    def list_services() -> dict:
        """레지스트리에 등록된 서비스들의 systemd 상태(active/sub/PID 등)를 조회한다."""
        services = service_ops.query_all(ctx.runner, ctx.registry)
        ctx.audit.log(tool="list_services", outcome="ok", risk="low")
        return {"services": services}

    @mcp.tool()
    def read_service_logs(service_name: str, lines: int = 100) -> dict:
        """등록된 서비스의 최근 journald 로그를 조회한다.

        service_name: 레지스트리에 등록된 서비스 이름.
        lines: 가져올 로그 줄 수 (1~1000, 기본 100).
        """
        entry = ctx.registry.service(service_name)
        if entry is None:
            ctx.audit.log(
                tool="read_service_logs",
                params={"service_name": service_name},
                outcome="rejected",
                risk="low",
            )
            return {
                "status": "rejected",
                "reason": f"등록되지 않은 서비스: {service_name!r}",
                "known_services": ctx.registry.service_names,
            }
        result = service_ops.read_logs(ctx.runner, entry, lines)
        ctx.audit.log(
            tool="read_service_logs",
            params={"service_name": service_name, "lines": lines},
            outcome="ok" if result["ok"] else "error",
            risk="low",
        )
        return result
