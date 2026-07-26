"""공용 픽스처 — 실제 맥/Ollama 없이 전체 흐름을 검증한다."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import RoleConfig, ServiceConfig  # noqa: E402
from app.ollama import BackendError, GenerateResult  # noqa: E402
from app.store import Store  # noqa: E402

ROLES_RAW = {
    "backend": {"base_url": "http://mac.test:11434", "keep_alive": "10m"},
    "mem_budget_gb": 40,
    "model_sizes_gb": {"small": 5, "mid": 10, "big": 30, "bge-m3": 1.2},
    "roles": {
        "vec":   {"model": "bge-m3", "kind": "embed", "timeout": 60},
        "fast":  {"model": "small", "lane": "interactive", "timeout": 30,
                  "system": "기본 지시", "options": {"temperature": 0.3}},
        "chat":  {"model": "mid", "lane": "interactive", "timeout": 60},
        "heavy": {"model": "big", "lane": "batch", "timeout": 120},
        "bulk":  {"model": "mid", "lane": "batch", "timeout": 60},
        "tiny":  {"model": "small", "lane": "batch", "timeout": 30,
                  "max_prompt_chars": 50},
    },
}

SERVICES_RAW = {
    "services": {
        "alpha": {"token_env": "TOK_ALPHA", "allow_roles": ["*"], "rate_limit_per_min": 100},
        "beta":  {"token_env": "TOK_BETA", "allow_roles": ["fast"], "rate_limit_per_min": 100},
        "slow":  {"token_env": "TOK_SLOW", "allow_roles": ["*"], "rate_limit_per_min": 2},
    }
}

TOKEN_ALPHA = "a" * 40
TOKEN_BETA = "b" * 40
TOKEN_SLOW = "s" * 40


class FakeOllama:
    """제어 가능한 가짜 백엔드.

    - delay: 각 generate 호출이 걸리는 시간
    - fail_times: 앞의 N번은 실패(재시도 검증)
    - calls: 호출된 모델 순서 기록(모델 전환 횟수 검증)
    """

    def __init__(self, delay: float = 0.0, fail_times: int = 0,
                 retryable: bool = True, models: list[str] | None = None,
                 delays: dict[str, float] | None = None) -> None:
        self.delay = delay
        self.delays = delays or {}      # 모델별 지연(없으면 delay)
        self.fail_times = fail_times
        self.retryable = retryable
        self.calls: list[str] = []
        self.concurrent = 0
        self.max_concurrent = 0
        self._models = models if models is not None else ["small", "mid", "big", "bge-m3"]
        self.tags_fail = False
        # pull 제어: 설치된 모델 기록 + 실패시킬 모델 + 지연
        self.pulled: list[str] = []
        self.pull_fail: set[str] = set()
        self.pull_delay = 0.0
        # embed 제어
        self.embed_calls: list[tuple] = []
        self.embed_fail = False
        self.embed_delay = 0.0

    async def generate(self, *, model, prompt, system=None, options=None,
                       timeout=180, client=None) -> GenerateResult:
        self.calls.append(model)
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            if self.fail_times > 0:
                self.fail_times -= 1
                raise BackendError("가짜 실패", retryable=self.retryable)
            delay = self.delays.get(model, self.delay)
            if delay:
                await asyncio.sleep(delay)
            return GenerateResult(
                response=f"[{model}] {prompt[:20]}", eval_count=10, duration_ms=5
            )
        finally:
            self.concurrent -= 1

    async def embed(self, *, model, inputs, timeout=60):
        from app.ollama import EmbedResult
        self.embed_calls.append((model, list(inputs)))
        if self.embed_fail:
            raise BackendError("가짜 임베딩 실패", retryable=self.retryable)
        if self.embed_delay:
            await asyncio.sleep(self.embed_delay)
        return EmbedResult(embeddings=[[0.1, 0.2, 0.3] for _ in inputs],
                           prompt_eval_count=len(inputs), duration_ms=3)

    async def tags(self, timeout: int = 5) -> list[str]:
        if self.tags_fail:
            raise BackendError("백엔드 다운")
        return sorted(self._models)

    async def pull(self, model, *, progress_cb=None, timeout: int = 3600) -> None:
        self.pulled.append(model)
        if self.pull_delay:
            await asyncio.sleep(self.pull_delay)
        if model in self.pull_fail:
            raise BackendError(f"pull 실패: {model}", retryable=False)
        for pct in (0, 50, 100):
            if progress_cb:
                progress_cb(pct)
        self._models.append(model)

    @property
    def model_switches(self) -> int:
        """연속 호출에서 모델이 바뀐 횟수."""
        return sum(1 for a, b in zip(self.calls, self.calls[1:]) if a != b)


@pytest.fixture
def roles():
    return RoleConfig.from_dict(ROLES_RAW)


@pytest.fixture
def services(monkeypatch):
    monkeypatch.setenv("TOK_ALPHA", TOKEN_ALPHA)
    monkeypatch.setenv("TOK_BETA", TOKEN_BETA)
    monkeypatch.setenv("TOK_SLOW", TOKEN_SLOW)
    return ServiceConfig.from_dict(SERVICES_RAW)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "test.db")


@pytest.fixture
def fake_ollama():
    return FakeOllama()
