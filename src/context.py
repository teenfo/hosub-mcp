"""도구가 공유하는 애플리케이션 컨텍스트.

registry / runner / jobs / audit 를 한 객체로 묶어 각 도구 등록 함수에 주입한다.
LLM 은 게이트웨이(src/gateway.py)를 통해 HTTP 로만 접근하므로 여기에 들어오지 않는다.
테스트는 FakeRunner 와 임시 레지스트리로 컨텍스트를 조립할 수 있다.
"""

from __future__ import annotations

from dataclasses import dataclass

from .audit import AuditLog
from .jobs import JobManager
from .registry import Registry
from .runner import CommandRunner


@dataclass
class AppContext:
    registry: Registry
    runner: CommandRunner
    jobs: JobManager
    audit: AuditLog
