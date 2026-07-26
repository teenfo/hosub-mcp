"""역할(모델 정책)·서비스(인증) 설정 로드와 검증.

설계 원칙(docs/requests/llm-gateway-service.md 7절):
- 역할은 **모델 정책**만 정의한다(model/lane/timeout/options + 기본 system).
- 프롬프트는 **호출자 소유** — 요청의 system 이 있으면 그것이 우선한다.
  덕분에 roxlogy 같은 소비자가 자기 레포에서 프롬프트를 자유롭게 개선할 수 있다.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

LANES = ("interactive", "batch")
DEFAULT_LANE = "batch"
# 역할의 종류. generate = 텍스트 생성(잡 큐를 탄다), embed = 임베딩(동기 처리)
KINDS = ("generate", "embed")
DEFAULT_KIND = "generate"
DEFAULT_TIMEOUT = 180
# 모델 크기를 모를 때 쓰는 보수적 추정(메모리 예산 가드가 과하게 낙관하지 않도록)
DEFAULT_MODEL_SIZE_GB = 20.0

_ENV_REF = re.compile(r"^\$\{([A-Z0-9_]+)\}$")


class ConfigError(ValueError):
    """설정 형식/검증 오류."""


def _resolve_env(value):
    """"${VAR}" 형태면 환경변수로 치환한다."""
    if isinstance(value, str):
        m = _ENV_REF.match(value.strip())
        if m:
            return os.environ.get(m.group(1), "")
    return value


# --------------------------------------------------------------------------
# 역할 (모델 정책)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Role:
    name: str
    model: str
    kind: str = DEFAULT_KIND
    lane: str = DEFAULT_LANE
    timeout: int = DEFAULT_TIMEOUT
    system: str | None = None          # 호출자가 안 보냈을 때의 기본값
    options: dict = field(default_factory=dict)
    max_prompt_chars: int | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "model": self.model,
            "kind": self.kind,
            "lane": self.lane,
            "timeout": self.timeout,
            "has_default_system": bool(self.system),
            "max_prompt_chars": self.max_prompt_chars,
        }


@dataclass(frozen=True)
class Backend:
    base_url: str
    keep_alive: str = "10m"


class RoleConfig:
    def __init__(
        self,
        backend: Backend,
        roles: dict[str, Role],
        model_sizes: dict[str, float],
        mem_budget_gb: float,
    ) -> None:
        self.backend = backend
        self._roles = roles
        self._model_sizes = model_sizes
        self.mem_budget_gb = mem_budget_gb

    def role(self, name: str) -> Role | None:
        return self._roles.get(name)

    @property
    def role_names(self) -> list[str]:
        return sorted(self._roles)

    @property
    def roles(self) -> list[Role]:
        return [self._roles[n] for n in self.role_names]

    def roles_using(self, model: str) -> list[str]:
        """이 모델을 쓰는 역할 이름들(모델 설치 요청의 근거 표시용)."""
        return [n for n in self.role_names if self._roles[n].model == model]

    @property
    def embed_role_names(self) -> list[str]:
        return [n for n in self.role_names if self._roles[n].kind == "embed"]

    def model_size_gb(self, model: str) -> float:
        """모델 메모리 추정치(GB). 미상이면 보수적으로 큰 값."""
        if model in self._model_sizes:
            return float(self._model_sizes[model])
        # "qwen2.5:14b" 처럼 태그에서 파라미터 수를 추정 (b = billion)
        m = re.search(r"(\d+(?:\.\d+)?)\s*b\b", model.lower())
        if m:
            # 4bit 양자화 기준 대략 0.65GB/1B + 오버헤드
            return round(float(m.group(1)) * 0.65 + 1.5, 1)
        return DEFAULT_MODEL_SIZE_GB

    @classmethod
    def load(cls, path: str | os.PathLike) -> "RoleConfig":
        p = Path(path)
        if not p.is_file():
            raise ConfigError(f"역할 설정 파일이 없습니다: {p}")
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"역할 설정 파싱 실패: {exc}") from exc
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> "RoleConfig":
        if not isinstance(raw, dict):
            raise ConfigError("최상위는 매핑이어야 합니다")

        b = raw.get("backend") or {}
        base_url = str(_resolve_env(b.get("base_url", "")) or "").rstrip("/")
        if not base_url:
            raise ConfigError(
                "backend.base_url 이 비어 있습니다 (OLLAMA_URL 환경변수를 확인하세요)"
            )
        backend = Backend(base_url=base_url, keep_alive=str(b.get("keep_alive", "10m")))

        roles: dict[str, Role] = {}
        for name, cfg in (raw.get("roles") or {}).items():
            if not isinstance(cfg, dict):
                raise ConfigError(f"역할 '{name}' 항목은 매핑이어야 합니다")
            model = cfg.get("model")
            if not model:
                raise ConfigError(f"역할 '{name}' 에 model 이 필요합니다")
            lane = str(cfg.get("lane", DEFAULT_LANE))
            if lane not in LANES:
                raise ConfigError(f"역할 '{name}' 의 lane 이 잘못됨: {lane} (가능: {LANES})")
            kind = str(cfg.get("kind", DEFAULT_KIND))
            if kind not in KINDS:
                raise ConfigError(f"역할 '{name}' 의 kind 가 잘못됨: {kind} (가능: {KINDS})")
            roles[name] = Role(
                name=name,
                model=str(model),
                kind=kind,
                lane=lane,
                timeout=int(cfg.get("timeout", DEFAULT_TIMEOUT)),
                system=cfg.get("system"),
                options=dict(cfg.get("options") or {}),
                max_prompt_chars=(
                    int(cfg["max_prompt_chars"]) if cfg.get("max_prompt_chars") else None
                ),
            )
        if not roles:
            raise ConfigError("roles 가 비어 있습니다")

        sizes = {str(k): float(v) for k, v in (raw.get("model_sizes_gb") or {}).items()}
        budget = float(
            _resolve_env(raw.get("mem_budget_gb"))
            or os.environ.get("MEM_BUDGET_GB", 40)
        )
        return cls(backend=backend, roles=roles, model_sizes=sizes, mem_budget_gb=budget)


# --------------------------------------------------------------------------
# 서비스 (인증·권한)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Service:
    name: str
    token: str
    allow_roles: tuple[str, ...] = ("*",)
    rate_limit_per_min: int = 60
    # 모델 설치 요청을 승인/거부할 수 있는가. hosub(MCP·대시보드)에만 준다.
    admin: bool = False

    def may_use(self, role: str) -> bool:
        return "*" in self.allow_roles or role in self.allow_roles


class ServiceConfig:
    def __init__(self, services: dict[str, Service]) -> None:
        self._services = services
        # 토큰 → 서비스 (인증용 역인덱스)
        self._by_token = {s.token: s for s in services.values() if s.token}

    def by_token(self, token: str) -> Service | None:
        # 타이밍 공격 방지를 위해 호출부에서 compare_digest 로 재확인한다.
        return self._by_token.get(token)

    @property
    def names(self) -> list[str]:
        return sorted(self._services)

    def get(self, name: str) -> Service | None:
        return self._services.get(name)

    @classmethod
    def load(cls, path: str | os.PathLike) -> "ServiceConfig":
        p = Path(path)
        if not p.is_file():
            raise ConfigError(f"서비스 설정 파일이 없습니다: {p}")
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"서비스 설정 파싱 실패: {exc}") from exc
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> "ServiceConfig":
        services: dict[str, Service] = {}
        for name, cfg in (raw.get("services") or {}).items():
            if not isinstance(cfg, dict):
                raise ConfigError(f"서비스 '{name}' 항목은 매핑이어야 합니다")
            token_env = cfg.get("token_env")
            if not token_env:
                raise ConfigError(f"서비스 '{name}' 에 token_env 가 필요합니다")
            token = os.environ.get(str(token_env), "")
            # 토큰이 비면 그 서비스는 비활성(설정 누락으로 전체 기동을 막지는 않는다)
            allow = cfg.get("allow_roles") or ["*"]
            if not isinstance(allow, list):
                raise ConfigError(f"서비스 '{name}' 의 allow_roles 는 리스트여야 합니다")
            services[name] = Service(
                name=name,
                token=token,
                allow_roles=tuple(str(a) for a in allow),
                rate_limit_per_min=int(cfg.get("rate_limit_per_min", 60)),
                admin=bool(cfg.get("admin", False)),
            )
        if not services:
            raise ConfigError("services 가 비어 있습니다")
        return cls(services)
