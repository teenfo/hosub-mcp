"""LLM 게이트웨이 도구: llm_list_roles, llm_status, llm_generate.

역할(role) 이름으로 요청하면 레지스트리가 Mac 등 백엔드의 모델로 라우팅한다.
조회성이라 위험도 Low — 서버 상태를 바꾸지 않고 추론만 수행한다.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .. import llm as llm_mod
from ..context import AppContext

_SUMMARY_MAX = 300


def register(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool()
    def llm_list_roles() -> dict:
        """사용 가능한 LLM 역할(기능) 목록과 각 역할이 쓰는 모델을 조회한다.

        역할 이름을 llm_generate 의 role 인자로 넘기면 해당 모델이 실행된다.
        """
        roles = [r.to_dict() | {"description": r.description} for r in ctx.llm.roles]
        if not roles:
            return {
                "roles": [],
                "hint": "config/llm_registry.yaml 에 roles 를 정의하면 여기에 표시됩니다.",
            }
        return {"roles": roles, "fallback_role": ctx.llm.fallback_role}

    @mcp.tool()
    def llm_status() -> dict:
        """LLM 백엔드(Mac 등)의 연결 상태와 보유 모델, 역할별 모델 준비 여부를 조회한다."""
        if not ctx.llm.role_names:
            return {"backends": [], "roles": [], "hint": "LLM 레지스트리가 비어 있습니다."}
        result = llm_mod.backend_status(ctx.llm)
        ctx.audit.log(tool="llm_status", outcome="ok", risk="low")
        return result

    @mcp.tool()
    def llm_generate(
        prompt: str,
        role: str = "general",
        system: str | None = None,
    ) -> dict:
        """로컬 LLM(Mac 등)으로 프롬프트를 실행한다.

        prompt: 모델에 보낼 입력 텍스트.
        role: 기능 이름 (llm_list_roles 로 확인. 예: summarize, log_analyze,
              translate, code, general). 역할마다 모델·기본 지시문이 다르다.
        system: 역할의 기본 시스템 프롬프트를 이번 호출만 다른 값으로 대체(선택).
        """
        if not ctx.llm.role_names:
            return {
                "status": "rejected",
                "reason": "LLM 레지스트리가 비어 있습니다. config/llm_registry.yaml 을 설정하세요.",
            }
        result = llm_mod.generate(ctx.llm, role, prompt, system=system)

        outcome = result.get("status", "error")
        summary = (result.get("response") or result.get("error") or "")[:_SUMMARY_MAX]
        ctx.audit.log(
            tool="llm_generate",
            params={"role": role, "prompt_chars": len(prompt)},
            risk="low",
            outcome=outcome,
            result_summary=f"{result.get('model', '?')} :: {summary}",
        )
        return result
