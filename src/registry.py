"""화이트리스트 레지스트리 로드 및 검증.

이 모듈은 보안 경계다. registry.yaml 의 서비스/스크립트 항목만 화이트리스트
도구가 다룰 수 있으며, 형식 위반은 로드 시점에 즉시 예외로 거부한다.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

_UNIT_RE = re.compile(r"^[A-Za-z0-9_.@-]+\.service$")

_ALLOWED_TOP_KEYS = {"services", "scripts", "backup_script"}
_ALLOWED_SERVICE_KEYS = {"unit", "description", "deploy"}
_ALLOWED_SCRIPT_KEYS = {"path", "description", "timeout_seconds"}
_ALLOWED_DEPLOY_KEYS = {"workdir", "steps", "restart_after", "timeout_seconds"}


class RegistryError(ValueError):
    """레지스트리 형식/검증 오류."""


@dataclass(frozen=True)
class ScriptEntry:
    name: str
    path: str
    description: str = ""
    timeout_seconds: int = 600


@dataclass(frozen=True)
class DeploySpec:
    workdir: str | None
    steps: tuple[tuple[str, ...], ...]
    restart_after: bool
    timeout_seconds: int


@dataclass(frozen=True)
class ServiceEntry:
    name: str
    unit: str
    description: str = ""
    deploy: DeploySpec | None = None


class Registry:
    """로드·검증된 화이트리스트."""

    def __init__(
        self,
        services: dict[str, ServiceEntry],
        scripts: dict[str, ScriptEntry],
        backup_script: str | None,
    ) -> None:
        self._services = services
        self._scripts = scripts
        self._backup_script = backup_script

    # --- 조회 ---
    def service(self, name: str) -> ServiceEntry | None:
        return self._services.get(name)

    def script(self, name: str) -> ScriptEntry | None:
        return self._scripts.get(name)

    def backup(self) -> ScriptEntry | None:
        if self._backup_script is None:
            return None
        return self._scripts.get(self._backup_script)

    @property
    def service_names(self) -> list[str]:
        return sorted(self._services)

    @property
    def script_names(self) -> list[str]:
        return sorted(self._scripts)

    @property
    def services(self) -> list[ServiceEntry]:
        return [self._services[n] for n in self.service_names]

    # --- 로드 ---
    @classmethod
    def load(cls, path: str | os.PathLike, *, strict: bool = False) -> "Registry":
        p = Path(path)
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except FileNotFoundError as exc:
            raise RegistryError(f"레지스트리 파일 없음: {p}") from exc
        except yaml.YAMLError as exc:
            raise RegistryError(f"레지스트리 YAML 파싱 실패: {exc}") from exc
        return cls.from_dict(raw, strict=strict)

    @classmethod
    def from_dict(cls, raw: dict, *, strict: bool = False) -> "Registry":
        if not isinstance(raw, dict):
            raise RegistryError("레지스트리 최상위는 매핑이어야 함")
        unknown = set(raw) - _ALLOWED_TOP_KEYS
        if unknown:
            raise RegistryError(f"알 수 없는 최상위 키: {sorted(unknown)}")

        services = cls._parse_services(raw.get("services") or {})
        scripts = cls._parse_scripts(raw.get("scripts") or {}, strict=strict)

        backup_script = raw.get("backup_script")
        if backup_script is not None:
            if not isinstance(backup_script, str):
                raise RegistryError("backup_script 는 문자열이어야 함")
            if backup_script not in scripts:
                raise RegistryError(
                    f"backup_script '{backup_script}' 가 scripts 에 없음"
                )

        return cls(services=services, scripts=scripts, backup_script=backup_script)

    # --- 파서 ---
    @staticmethod
    def _parse_services(node: dict) -> dict[str, ServiceEntry]:
        if not isinstance(node, dict):
            raise RegistryError("services 는 매핑이어야 함")
        out: dict[str, ServiceEntry] = {}
        for name, cfg in node.items():
            if not isinstance(cfg, dict):
                raise RegistryError(f"서비스 '{name}' 항목은 매핑이어야 함")
            unknown = set(cfg) - _ALLOWED_SERVICE_KEYS
            if unknown:
                raise RegistryError(f"서비스 '{name}' 알 수 없는 키: {sorted(unknown)}")
            unit = cfg.get("unit")
            if not isinstance(unit, str) or not _UNIT_RE.match(unit):
                raise RegistryError(
                    f"서비스 '{name}' 의 unit 이 올바른 .service 형식이 아님: {unit!r}"
                )
            deploy = Registry._parse_deploy(name, cfg.get("deploy"))
            out[name] = ServiceEntry(
                name=name,
                unit=unit,
                description=str(cfg.get("description", "")),
                deploy=deploy,
            )
        return out

    @staticmethod
    def _parse_deploy(service_name: str, node) -> DeploySpec | None:
        if node is None:
            return None
        if not isinstance(node, dict):
            raise RegistryError(f"서비스 '{service_name}' 의 deploy 는 매핑이어야 함")
        unknown = set(node) - _ALLOWED_DEPLOY_KEYS
        if unknown:
            raise RegistryError(
                f"서비스 '{service_name}' deploy 알 수 없는 키: {sorted(unknown)}"
            )
        steps_raw = node.get("steps")
        if not isinstance(steps_raw, list) or not steps_raw:
            raise RegistryError(
                f"서비스 '{service_name}' deploy.steps 는 비어있지 않은 리스트여야 함"
            )
        steps: list[tuple[str, ...]] = []
        for i, step in enumerate(steps_raw):
            if not isinstance(step, list) or not step:
                raise RegistryError(
                    f"서비스 '{service_name}' deploy.steps[{i}] 는 argv 리스트여야 함 "
                    f"(셸 문자열 금지)"
                )
            if not all(isinstance(tok, str) for tok in step):
                raise RegistryError(
                    f"서비스 '{service_name}' deploy.steps[{i}] 의 모든 토큰은 문자열이어야 함"
                )
            steps.append(tuple(step))
        return DeploySpec(
            workdir=node.get("workdir"),
            steps=tuple(steps),
            restart_after=bool(node.get("restart_after", False)),
            timeout_seconds=int(node.get("timeout_seconds", 900)),
        )

    @staticmethod
    def _parse_scripts(node: dict, *, strict: bool) -> dict[str, ScriptEntry]:
        if not isinstance(node, dict):
            raise RegistryError("scripts 는 매핑이어야 함")
        out: dict[str, ScriptEntry] = {}
        for name, cfg in node.items():
            if not isinstance(cfg, dict):
                raise RegistryError(f"스크립트 '{name}' 항목은 매핑이어야 함")
            unknown = set(cfg) - _ALLOWED_SCRIPT_KEYS
            if unknown:
                raise RegistryError(
                    f"스크립트 '{name}' 알 수 없는 키: {sorted(unknown)}"
                )
            path = cfg.get("path")
            if not isinstance(path, str) or not path:
                raise RegistryError(f"스크립트 '{name}' 의 path 가 필요함")
            if not os.path.isabs(path):
                raise RegistryError(
                    f"스크립트 '{name}' 의 path 는 절대경로여야 함: {path!r}"
                )
            if strict:
                if not os.path.isfile(path):
                    raise RegistryError(f"스크립트 '{name}' 경로가 존재하지 않음: {path}")
                if not os.access(path, os.X_OK):
                    raise RegistryError(f"스크립트 '{name}' 실행 권한 없음: {path}")
            out[name] = ScriptEntry(
                name=name,
                path=path,
                description=str(cfg.get("description", "")),
                timeout_seconds=int(cfg.get("timeout_seconds", 600)),
            )
        return out
