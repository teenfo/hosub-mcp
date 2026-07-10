"""위험도 정책 계층.

위험도는 도구별 데코레이터가 아닌 단일 TOOL_RISK 맵으로 선언한다 —
맵 자체가 리뷰 대상이 되도록. Medium/High 도구는 confirm=true 없이는
실행되지 않고, Claude 가 사용자에게 승인을 구하도록 유도하는 페이로드를
반환한다.
"""

from __future__ import annotations

from enum import Enum


class Risk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


TOOL_RISK: dict[str, Risk] = {
    # 조회 계층 (즉시 실행)
    "get_system_status": Risk.LOW,
    "list_services": Risk.LOW,
    "read_service_logs": Risk.LOW,
    "get_job_status": Risk.LOW,
    "list_jobs": Risk.LOW,
    "read_file": Risk.LOW,
    "list_directory": Risk.LOW,
    # 상태 변경 (confirm 필요)
    "restart_service": Risk.MEDIUM,
    "run_backup": Risk.MEDIUM,
    "deploy_service": Risk.HIGH,
    "run_script": Risk.HIGH,
    "run_command": Risk.HIGH,
    "write_file": Risk.HIGH,
}


def risk_of(tool: str) -> Risk:
    """도구의 위험도를 반환. 미등록 도구는 안전을 위해 HIGH 로 간주."""
    return TOOL_RISK.get(tool, Risk.HIGH)


def check_confirm(tool: str, confirm: bool, action_description: str) -> dict | None:
    """실행 가능 여부 판정.

    반환값이 None 이면 실행을 진행해도 된다. dict 이면 승인 요청 페이로드이며
    도구는 이를 그대로 반환해야 한다 (실행 금지).
    """
    risk = risk_of(tool)
    if risk is Risk.LOW or confirm:
        return None
    return {
        "status": "approval_required",
        "tool": tool,
        "risk": risk.value,
        "action": action_description,
        "message": (
            "이 작업은 아직 실행되지 않았습니다. 사용자에게 위 action 을 설명하고 "
            "명시적으로 동의를 받은 경우에만, 동일한 인자에 confirm=true 를 추가하여 "
            "이 도구를 다시 호출하세요."
        ),
    }
