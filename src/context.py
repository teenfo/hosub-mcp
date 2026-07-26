"""도구가 공유하는 애플리케이션 컨텍스트.

registry / runner / jobs / audit 를 한 객체로 묶어 각 도구 등록 함수에 주입한다.
테스트는 FakeRunner 와 임시 레지스트리로 컨텍스트를 조립할 수 있다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .audit import AuditLog
from .jobs import JobManager
from .llm import LLMRegistry
from .registry import Registry
from .runner import CommandRunner


@dataclass
class AppContext:
    registry: Registry
    runner: CommandRunner
    jobs: JobManager
    audit: AuditLog
    # LLM 게이트웨이 레지스트리 (역할 → Mac 등 백엔드 모델 라우팅)
    llm: LLMRegistry = field(default_factory=lambda: LLMRegistry({}, {}, None))
