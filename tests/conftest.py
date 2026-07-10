"""공용 테스트 픽스처."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 저장소 루트를 import 경로에 추가 (src.* 패키지 임포트용)
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.runner import RunResult  # noqa: E402


class FakeRunner:
    """미리 정의한 응답을 돌려주는 러너.

    responses: argv 튜플 → RunResult 매핑. 매칭 안 되면 default 반환.
    calls: 실행된 (argv, cwd, shell) 기록.
    """

    def __init__(
        self,
        responses: dict[tuple[str, ...], RunResult] | None = None,
        default: RunResult | None = None,
    ) -> None:
        self.responses = responses or {}
        self.default = default or RunResult(exit_code=0, stdout="ok", stderr="")
        self.calls: list[tuple[tuple[str, ...], str | None, bool]] = []

    def run(self, argv, *, timeout, cwd=None, shell=False) -> RunResult:
        self.calls.append((tuple(argv), cwd, shell))
        return self.responses.get(tuple(argv), self.default)


@pytest.fixture
def fake_runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def audit(tmp_path):
    from src.audit import AuditLog

    return AuditLog(tmp_path / "audit.db")
