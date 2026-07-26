"""LLM 게이트웨이.

hosub 는 추론을 직접 하지 않고, 역할(role) 이름을 실제 백엔드/모델로 라우팅한다.
백엔드는 주로 Tailscale 로 연결된 Mac 의 Ollama.

호출부(MCP 도구·대시보드)는 role 만 알면 되고, 모델 교체는 레지스트리 YAML 한 줄로
끝난다. HTTP 호출은 동기(httpx.Client)로 하며, 비동기 컨텍스트(대시보드)에서는
run_in_threadpool 로 감싸 쓴다.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

DEFAULT_TIMEOUT = 180
_OUTPUT_MAX = 100_000


class LLMConfigError(ValueError):
    """레지스트리 형식 오류."""


@dataclass(frozen=True)
class Backend:
    name: str
    base_url: str
    type: str = "ollama"


@dataclass(frozen=True)
class Role:
    name: str
    backend: str
    model: str
    description: str = ""
    system: str | None = None
    options: dict = field(default_factory=dict)
    timeout: int = DEFAULT_TIMEOUT

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "backend": self.backend,
            "model": self.model,
            "description": self.description,
            "timeout": self.timeout,
        }


class LLMRegistry:
    """역할 → 백엔드/모델 매핑."""

    def __init__(
        self,
        backends: dict[str, Backend],
        roles: dict[str, Role],
        fallback_role: str | None = None,
    ) -> None:
        self._backends = backends
        self._roles = roles
        self._fallback_role = fallback_role

    # --- 조회 ---
    def role(self, name: str) -> Role | None:
        return self._roles.get(name)

    def backend(self, name: str) -> Backend | None:
        return self._backends.get(name)

    @property
    def role_names(self) -> list[str]:
        return sorted(self._roles)

    @property
    def roles(self) -> list[Role]:
        return [self._roles[n] for n in self.role_names]

    @property
    def backends(self) -> list[Backend]:
        return [self._backends[n] for n in sorted(self._backends)]

    @property
    def fallback_role(self) -> str | None:
        return self._fallback_role

    # --- 로드 ---
    @classmethod
    def load(cls, path: str | os.PathLike) -> "LLMRegistry":
        p = Path(path)
        if not p.is_file():
            # 레지스트리가 없으면 빈 상태로 동작 (도구는 "설정 없음" 안내)
            return cls({}, {}, None)
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise LLMConfigError(f"LLM 레지스트리 파싱 실패: {exc}") from exc
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> "LLMRegistry":
        if not isinstance(raw, dict):
            raise LLMConfigError("최상위는 매핑이어야 함")

        backends: dict[str, Backend] = {}
        for name, cfg in (raw.get("backends") or {}).items():
            if not isinstance(cfg, dict) or not cfg.get("base_url"):
                raise LLMConfigError(f"백엔드 '{name}' 에 base_url 이 필요함")
            backends[name] = Backend(
                name=name,
                base_url=str(cfg["base_url"]).rstrip("/"),
                type=str(cfg.get("type", "ollama")),
            )

        default_backend = raw.get("default_backend")
        roles: dict[str, Role] = {}
        for name, cfg in (raw.get("roles") or {}).items():
            if not isinstance(cfg, dict):
                raise LLMConfigError(f"역할 '{name}' 항목은 매핑이어야 함")
            model = cfg.get("model")
            if not model:
                raise LLMConfigError(f"역할 '{name}' 에 model 이 필요함")
            backend = cfg.get("backend") or default_backend
            if not backend:
                raise LLMConfigError(f"역할 '{name}' 의 backend 를 정할 수 없음")
            if backend not in backends:
                raise LLMConfigError(f"역할 '{name}' 이 모르는 백엔드 참조: {backend}")
            roles[name] = Role(
                name=name,
                backend=backend,
                model=str(model),
                description=str(cfg.get("description", "")),
                system=cfg.get("system"),
                options=dict(cfg.get("options") or {}),
                timeout=int(cfg.get("timeout", DEFAULT_TIMEOUT)),
            )

        fallback = raw.get("fallback_role")
        if fallback and fallback not in roles:
            raise LLMConfigError(f"fallback_role '{fallback}' 이 roles 에 없음")

        return cls(backends=backends, roles=roles, fallback_role=fallback)


# --- 백엔드 호출 (Ollama) ---

def generate(
    registry: LLMRegistry,
    role_name: str,
    prompt: str,
    *,
    system: str | None = None,
    timeout: int | None = None,
    client_factory=httpx.Client,
) -> dict:
    """역할에 매핑된 모델로 프롬프트를 실행하고 결과 dict 를 반환한다.

    실패해도 예외 대신 status/error 를 담은 dict 를 돌려준다(도구가 그대로 반환).
    """
    role = registry.role(role_name)
    if role is None:
        return {
            "status": "rejected",
            "reason": f"등록되지 않은 역할: {role_name!r}",
            "known_roles": registry.role_names,
        }
    backend = registry.backend(role.backend)
    if backend is None:
        return {"status": "error", "error": f"백엔드 '{role.backend}' 를 찾을 수 없음"}

    payload = {
        "model": role.model,
        "prompt": prompt,
        "stream": False,
    }
    sys_prompt = system if system is not None else role.system
    if sys_prompt:
        payload["system"] = sys_prompt
    if role.options:
        payload["options"] = role.options

    url = f"{backend.base_url}/api/generate"
    try:
        with client_factory(timeout=timeout or role.timeout) as client:
            res = client.post(url, json=payload)
            res.raise_for_status()
            data = res.json()
    except Exception as exc:
        return {
            "status": "error",
            "role": role_name,
            "backend": role.backend,
            "model": role.model,
            "error": f"{type(exc).__name__}: {exc}",
            "hint": f"{backend.base_url} 에 접근 가능한지, Mac 의 Ollama 가 "
            "OLLAMA_HOST=0.0.0.0 으로 떠 있는지 확인하세요.",
        }

    text = (data.get("response") or "")[:_OUTPUT_MAX]
    return {
        "status": "ok",
        "role": role_name,
        "backend": role.backend,
        "model": role.model,
        "response": text,
        "eval_count": data.get("eval_count"),
        "total_duration_ms": (
            round(data["total_duration"] / 1_000_000) if data.get("total_duration") else None
        ),
    }


def backend_status(
    registry: LLMRegistry, *, timeout: int = 4, client_factory=httpx.Client
) -> dict:
    """각 백엔드의 도달 여부와 보유 모델 목록을 조회한다.

    백엔드가 꺼져 있으면 타임아웃까지 기다리므로, 여러 백엔드를 병렬로 조회해
    전체 대기 시간을 timeout 수준으로 유지한다.
    """

    def probe(backend: Backend) -> dict:
        entry = {"name": backend.name, "base_url": backend.base_url, "type": backend.type}
        try:
            with client_factory(timeout=timeout) as client:
                res = client.get(f"{backend.base_url}/api/tags")
                res.raise_for_status()
                models = [m.get("name") for m in (res.json().get("models") or [])]
            entry.update({"online": True, "models": sorted(filter(None, models))})
        except Exception as exc:
            entry.update({"online": False, "models": [], "error": f"{type(exc).__name__}: {exc}"})
        return entry

    backends = registry.backends
    if not backends:
        out: list[dict] = []
    elif len(backends) == 1:
        out = [probe(backends[0])]
    else:
        with ThreadPoolExecutor(max_workers=min(len(backends), 4)) as pool:
            out = list(pool.map(probe, backends))

    # 역할별로 모델이 실제로 존재하는지 표시
    available: dict[str, list[str]] = {b["name"]: b["models"] for b in out}
    roles = []
    for role in registry.roles:
        d = role.to_dict()
        models = available.get(role.backend, [])
        # ollama 는 "qwen2.5:7b" 형태. 태그 없이 적었을 때도 매칭되도록 접두 비교.
        d["model_available"] = any(
            m == role.model or m.startswith(role.model + ":") for m in models
        )
        roles.append(d)
    return {"backends": out, "roles": roles, "fallback_role": registry.fallback_role}
