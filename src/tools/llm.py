"""LLM 도구 — 전부 공유 게이트웨이(llm-gateway)를 거친다.

hosub 는 맥의 Ollama 를 직접 부르지 않는다. 게이트웨이를 거쳐야 다른 소비자
(roxlogy·TNM·trading)와 같은 큐·레인·재시도·사용량 집계를 공유한다.

- llm_list_roles / llm_status / llm_generate / llm_job: 조회·추론이라 위험도 Low.
- llm_model_requests / llm_decide_model: 게이트웨이가 만든 **모델 설치 요청**을
  확인하고 승인/거부한다. 승인은 맥 디스크에 수 GB 를 내려받는 상태 변경이므로
  Medium — confirm 게이트를 통과해야 한다.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .. import gateway
from ..context import AppContext
from ..policy import check_confirm

_SUMMARY_MAX = 300


def register(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool()
    def llm_list_roles() -> dict:
        """사용 가능한 LLM 역할(기능) 목록과 각 역할이 쓰는 모델을 조회한다.

        역할 이름을 llm_generate 의 role 인자로 넘기면 해당 모델이 실행된다.
        """
        result = gateway.list_roles()
        if not result.get("roles"):
            result.setdefault(
                "hint",
                "llm-gateway/config/roles.yaml 에 역할을 정의하면 여기에 표시됩니다.",
            )
        return result

    @mcp.tool()
    def llm_status() -> dict:
        """LLM 백엔드(맥)의 연결 상태·보유 모델·대기열·사용량을 조회한다."""
        result = gateway.status()
        ctx.audit.log(tool="llm_status", outcome=result.get("status", "ok"), risk="low")
        return result

    @mcp.tool()
    def llm_generate(
        prompt: str,
        role: str = "general",
        system: str | None = None,
        wait: int = 120,
    ) -> dict:
        """맥의 LLM 으로 프롬프트를 실행한다 (게이트웨이 경유).

        prompt: 모델에 보낼 입력 텍스트.
        role: 기능 이름 (llm_list_roles 로 확인. 예: summarize, log_analyze,
              translate, code, general). 역할마다 모델·레인·기본 지시문이 다르다.
        system: 역할의 기본 시스템 프롬프트를 이번 호출만 다른 값으로 대체(선택).
        wait: 결과를 기다릴 초(0~300). 안에 못 끝나면 status=pending 과 job_id 를
              돌려주며, 잡은 계속 돈다 — llm_job 으로 나중에 수령한다.

        status 는 ok(완료) | pending(대기 중) | failed(실패) 중 하나다.
        """
        result = gateway.generate(role, prompt, system=system,
                                  wait=max(0, min(int(wait), 300)))
        status = result.get("status", "error")
        if status == "pending":
            result["hint"] = (
                f"아직 실행 중입니다. llm_job(job_id='{result.get('job_id')}') 로 "
                "결과를 확인하세요. 모델이 아직 설치되지 않아 대기 중일 수도 있습니다 "
                "— llm_model_requests 로 확인할 수 있습니다."
            )
        summary = (result.get("response") or result.get("error") or "")[:_SUMMARY_MAX]
        ctx.audit.log(
            tool="llm_generate",
            params={"role": role, "prompt_chars": len(prompt), "wait": wait},
            risk="low",
            outcome=status,
            result_summary=f"{result.get('model', '?')} :: {summary}",
        )
        return result

    @mcp.tool()
    def llm_job(job_id: str) -> dict:
        """llm_generate 가 pending 으로 돌려준 잡의 결과를 조회한다.

        게이트웨이는 잡을 영속 저장하므로, 맥이나 게이트웨이가 재시작해도
        결과는 남아 있다.
        """
        return gateway.get_job(job_id)

    @mcp.tool()
    def llm_model_requests(status: str | None = None) -> dict:
        """공유 LLM 게이트웨이의 모델 설치 요청 목록을 조회한다.

        외부 서비스(roxlogy·TNM 등)가 아직 맥에 없는 모델을 쓰면 게이트웨이가
        pending 요청을 만들어 둔다. 그 요청을 승인해야 실제로 설치된다.

        status: pending|approved|pulling|ready|rejected|failed 로 필터(선택).
        """
        result = gateway.list_model_requests(status)
        pending = [r for r in result.get("requests", []) if r.get("status") == "pending"]
        if pending:
            result["hint"] = (
                f"승인 대기 {len(pending)}건. llm_decide_model 로 승인하면 "
                "게이트웨이가 맥에 자동 설치하고, 대기하던 잡이 이어서 실행됩니다."
            )
        ctx.audit.log(tool="llm_model_requests", risk="low",
                      outcome=result.get("status", "error"))
        return result

    @mcp.tool()
    def llm_decide_model(model: str, action: str = "approve",
                         confirm: bool = False) -> dict:
        """모델 설치 요청을 승인하거나 거부한다.

        model: 요청 목록의 모델 이름 (예: "qwen2.5:32b").
        action: "approve" 면 게이트웨이가 맥에 내려받고, "reject" 면 그 모델을
                기다리던 잡들이 명확한 오류로 종료된다.
        confirm: 승인은 맥 디스크에 수 GB 를 받는 작업이라 사용자 동의가 필요하다.
        """
        if action not in ("approve", "reject"):
            return {"status": "rejected",
                    "reason": "action 은 approve 또는 reject 여야 합니다."}
        req = None
        listed = gateway.list_model_requests()
        for r in listed.get("requests", []):
            if r.get("model") == model:
                req = r
                break
        if req is None:
            return {
                "status": "rejected",
                "reason": f"모델 설치 요청을 찾을 수 없습니다: {model!r}",
                "known_models": [r.get("model") for r in listed.get("requests", [])],
                "error": listed.get("error"),
            }

        size = req.get("est_size_gb")
        desc = (
            f"모델 '{model}' 설치 {'승인' if action == 'approve' else '거부'}"
            + (f" (약 {size}GB 다운로드, 역할: {', '.join(req.get('roles') or []) or '-'})"
               if action == "approve" else "")
        )
        if (pending := check_confirm("llm_decide_model", confirm, desc)):
            ctx.audit.log(tool="llm_decide_model", params={"model": model, "action": action},
                          risk="medium", outcome="approval_required")
            return pending

        result = gateway.decide_model_request(model, action)
        ctx.audit.log(
            tool="llm_decide_model", params={"model": model, "action": action},
            confirm=True, risk="medium", outcome=result.get("status", "error"),
            result_summary=str(result.get("request") or result.get("error") or "")[:_SUMMARY_MAX],
        )
        return result
