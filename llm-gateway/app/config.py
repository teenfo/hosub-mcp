"""역할(모델 정책)·서비스(인증) 설정 로드와 검증.

설계 원칙(docs/requests/llm-gateway-service.md 7절):
- 역할은 **모델 정책**만 정의한다(model/lane/timeout/options + 기본 system).
- 프롬프트는 **호출자 소유** — 요청의 system 이 있으면 그것이 우선한다.
  덕분에 roxlogy 같은 소비자가 자기 레포에서 프롬프트를 자유롭게 개선할 수 있다.

`roles.yaml` 은 **기본값**이고, 대시보드에서 건 런타임 오버라이드(SQLite)가 그 위에
얹힌다. 조회 메서드는 전부 `self._roles`(= 병합 결과) 하나만 거치므로 호출부는
오버라이드의 존재를 몰라도 된다.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

log = logging.getLogger("llmgw.config")

LANES = ("interactive", "batch")
DEFAULT_LANE = "batch"
# 역할의 종류. generate = 텍스트 생성(잡 큐를 탄다), embed = 임베딩(동기 처리)
KINDS = ("generate", "embed")
DEFAULT_KIND = "generate"
DEFAULT_TIMEOUT = 180
MAX_TIMEOUT = 3600
# 모델 크기를 모를 때 쓰는 보수적 추정(메모리 예산 가드가 과하게 낙관하지 않도록)
DEFAULT_MODEL_SIZE_GB = 20.0

# 런타임에 덮어쓸 수 있는 역할 필드.
# - kind 는 제외한다: generate→embed 로 바꾸면 그 역할이 잡 큐와 메모리 예산을
#   우회하는 동기 경로로 넘어간다(main.embed). 웹 버튼으로 21GB 모델을 그 경로에
#   올릴 수 있게 되는 셈이다. DB 생성 역할은 **생성 시에만** kind 를 정한다.
# - system 은 제외한다: 프롬프트는 호출자 소유라는 원칙(위 docstring)과 충돌한다.
OVERRIDABLE_FIELDS = ("model", "lane", "timeout", "options", "max_prompt_chars")

# 오버라이드의 출처. yaml = roles.yaml 역할을 덮어씀, db = YAML 에 없는 신규 역할
ORIGINS = ("yaml", "db")

# 역할 이름은 잡 행·Slack 텍스트·쿼리스트링에 그대로 들어간다.
ROLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
# 모델 이름 = 맥에 내려받을 대상. 스킴(://)·공백·경로 2단 이상을 막는다.
MODEL_NAME_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*"      # [namespace/]name
    r"(?:/[A-Za-z0-9][A-Za-z0-9._-]*)?"
    r"(?::[A-Za-z0-9][A-Za-z0-9._-]*)?$"  # :tag
)

_ENV_REF = re.compile(r"^\$\{([A-Z0-9_]+)\}$")


class ConfigError(ValueError):
    """설정 형식/검증 오류."""


def model_matches(wanted: str, available: str) -> bool:
    """태그 표기 차이를 흡수한다. "qwen2.5" 는 "qwen2.5:latest" 와 같다.

    (스케줄러·삭제 차단·역할 조회가 모두 같은 규칙을 써야 하므로 여기 둔다.
     scheduler 는 하위 호환을 위해 이 이름을 재수출한다.)
    """
    if wanted == available:
        return True
    return available.startswith(wanted + ":") or wanted.startswith(available + ":")


def validate_model_name(model) -> str:
    name = str(model or "").strip()
    if not name:
        raise ConfigError("모델 이름이 비어 있습니다")
    if len(name) > 128 or ".." in name or not MODEL_NAME_RE.match(name):
        raise ConfigError(f"모델 이름 형식이 잘못됨: {name!r}")
    return name


def validate_role_fields(fields, *, require_model: bool = False,
                         allow_kind: bool = False) -> dict:
    """오버라이드 필드를 검증·정규화한다. 저장 전과 로드 후 양쪽에서 쓴다.

    저장 시점 검증만으로는 부족하다 — DB 를 손으로 고칠 수 있고, 필드 목록이
    바뀌면 옛 행이 남는다. 로드할 때도 같은 함수를 거치게 해서 나쁜 행이
    조용히 유효값으로 섞이지 않게 한다.
    """
    if not isinstance(fields, dict):
        raise ConfigError("fields 는 매핑이어야 합니다")
    allowed = set(OVERRIDABLE_FIELDS) | ({"kind"} if allow_kind else set())
    unknown = sorted(set(fields) - allowed)
    if unknown:
        raise ConfigError(f"덮어쓸 수 없는 필드: {', '.join(unknown)}"
                          f" (가능: {', '.join(sorted(allowed))})")

    out: dict = {}
    if "model" in fields:
        out["model"] = validate_model_name(fields["model"])
    elif require_model:
        raise ConfigError("model 이 필요합니다")

    if "kind" in fields:
        kind = str(fields["kind"])
        if kind not in KINDS:
            raise ConfigError(f"kind 가 잘못됨: {kind} (가능: {KINDS})")
        out["kind"] = kind

    if "lane" in fields:
        lane = str(fields["lane"])
        if lane not in LANES:
            raise ConfigError(f"lane 이 잘못됨: {lane} (가능: {LANES})")
        out["lane"] = lane

    if "timeout" in fields:
        try:
            timeout = int(fields["timeout"])
        except (TypeError, ValueError) as exc:
            raise ConfigError("timeout 은 정수여야 합니다") from exc
        if not 1 <= timeout <= MAX_TIMEOUT:
            raise ConfigError(f"timeout 은 1~{MAX_TIMEOUT}초 사이여야 합니다")
        out["timeout"] = timeout

    if "options" in fields:
        opts = fields["options"]
        if not isinstance(opts, dict):
            raise ConfigError("options 는 매핑이어야 합니다")
        clean: dict = {}
        for k, v in opts.items():
            if not isinstance(k, str) or not k:
                raise ConfigError("options 의 키는 문자열이어야 합니다")
            if not isinstance(v, (str, int, float, bool)) and v is not None:
                raise ConfigError(f"options.{k} 는 스칼라여야 합니다")
            clean[k] = v
        out["options"] = clean

    if "max_prompt_chars" in fields:
        v = fields["max_prompt_chars"]
        if v in (None, "", 0):
            out["max_prompt_chars"] = None
        else:
            try:
                n = int(v)
            except (TypeError, ValueError) as exc:
                raise ConfigError("max_prompt_chars 는 정수여야 합니다") from exc
            if n <= 0:
                raise ConfigError("max_prompt_chars 는 0보다 커야 합니다")
            out["max_prompt_chars"] = n

    if not out:
        raise ConfigError("변경할 필드가 없습니다")
    return out


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
        # YAML 원본(불변) / 오버라이드 / 유효값. 조회는 전부 _roles 만 본다.
        self._base: dict[str, Role] = dict(roles)
        self._overrides: dict[str, dict] = {}
        self._roles: dict[str, Role] = dict(roles)
        self._invalid_overrides: list[dict] = []
        self._model_sizes = model_sizes          # roles.yaml (사람이 고른 값)
        self._catalog_sizes: dict[str, float] = {}   # config/catalog.yaml
        self._measured_sizes: dict[str, float] = {}  # /api/tags 디스크 실측
        self.mem_budget_gb = mem_budget_gb

    def role(self, name: str) -> Role | None:
        return self._roles.get(name)

    @property
    def role_names(self) -> list[str]:
        return sorted(self._roles)

    @property
    def roles(self) -> list[Role]:
        return [self._roles[n] for n in self.role_names]

    def roles_using(self, model: str, *, loose: bool = False) -> list[str]:
        """이 모델을 쓰는 역할 이름들(설치 요청 근거·삭제 차단 표시용).

        loose=True 면 태그 표기 차이를 흡수한다("qwen2.5" ≈ "qwen2.5:latest").
        삭제를 막을 때는 반드시 loose 여야 한다 — 정확히 일치하지 않는다고
        지워버리면 그 역할이 곧바로 깨진다.
        """
        if loose:
            return [n for n in self.role_names
                    if model_matches(self._roles[n].model, model)]
        return [n for n in self.role_names if self._roles[n].model == model]

    @property
    def embed_role_names(self) -> list[str]:
        return [n for n in self.role_names if self._roles[n].kind == "embed"]

    # ---------- 크기 추정 ----------
    def set_measured_sizes(self, sizes: dict[str, float]) -> None:
        """맥의 /api/tags 가 알려준 **디스크** 크기(GB). 스케줄러가 주기적으로 준다."""
        self._measured_sizes = {str(k): float(v) for k, v in (sizes or {}).items()}

    def set_catalog_sizes(self, sizes: dict[str, float]) -> None:
        self._catalog_sizes = {str(k): float(v) for k, v in (sizes or {}).items()}

    def _lookup_size(self, table: dict[str, float], model: str) -> float | None:
        if model in table:
            return float(table[model])
        for k, v in table.items():
            if model_matches(model, k):
                return float(v)
        return None

    def model_size_gb(self, model: str) -> float:
        """모델 메모리 추정치(GB). 미상이면 보수적으로 큰 값.

        순서가 의미를 갖는다:
        1. roles.yaml `model_sizes_gb` — 사람이 **런타임 메모리** 기준으로 고른 값
        2. catalog.yaml — 카탈로그가 제공하는 참고치
        3. 실측(디스크) — /api/tags 의 size. 디스크 < 런타임이므로 헤드룸을 얹는다
        4. 이름의 `Nb` 휴리스틱
        5. 20GB 고정값(마지막 보루)

        1이 3보다 앞인 것이 중요하다. qwen2.5:32b 는 디스크 ~19.9GB 인데 실제
        런타임은 그보다 크다(YAML 은 21로 잡아 뒀다). 실측을 우선하면 예산
        가드가 낙관적으로 틀어진다.
        """
        for table in (self._model_sizes, self._catalog_sizes):
            hit = self._lookup_size(table, model)
            if hit is not None:
                return hit
        measured = self._lookup_size(self._measured_sizes, model)
        if measured is not None:
            # 디스크 → 런타임 헤드룸(KV 캐시·컨텍스트 버퍼)
            return round(measured * 1.15 + 0.5, 1)
        # "qwen2.5:14b" 처럼 태그에서 파라미터 수를 추정 (b = billion)
        m = re.search(r"(\d+(?:\.\d+)?)\s*b\b", model.lower())
        if m:
            # 4bit 양자화 기준 대략 0.65GB/1B + 오버헤드
            return round(float(m.group(1)) * 0.65 + 1.5, 1)
        return DEFAULT_MODEL_SIZE_GB

    # ---------- 런타임 오버라이드 ----------
    def apply_overrides(self, rows) -> list[dict]:
        """저장된 오버라이드 전체를 다시 적용한다. 잘못된 행 목록을 반환.

        **부분 적용이 아니라 전량 재구성**이다 — 쓰기 경로가 "DB 에 저장 →
        list_role_overrides() → apply_overrides()" 하나뿐이라 상태가 갈라질 수 없다.

        잘못된 행 하나가 기동을 막으면 안 된다. 설정을 DB 로 옮겨 놓고 나쁜 행
        때문에 서비스가 안 뜨는 게 최악의 결과다 — 건너뛰고 로그·/v1/status 로 알린다.
        """
        overrides: dict[str, dict] = {}
        invalid: list[dict] = []
        for row in rows or []:
            name = str((row or {}).get("role") or "")
            origin = str((row or {}).get("origin") or "yaml")
            try:
                if origin not in ORIGINS:
                    raise ConfigError(f"origin 이 잘못됨: {origin}")
                if origin == "db":
                    if not ROLE_NAME_RE.match(name):
                        raise ConfigError(f"역할 이름 형식이 잘못됨: {name!r}")
                    fields = validate_role_fields(
                        row.get("fields"), require_model=True, allow_kind=True)
                else:
                    if name not in self._base:
                        raise ConfigError("roles.yaml 에 없는 역할입니다")
                    fields = validate_role_fields(row.get("fields"))
            except ConfigError as exc:
                log.warning("역할 오버라이드 무시 (%s): %s", name or "?", exc)
                invalid.append({"role": name, "origin": origin, "error": str(exc)})
                continue
            overrides[name] = {
                "origin": origin,
                "fields": fields,
                "note": row.get("note"),
                "updated_by": row.get("updated_by"),
                "updated_at": row.get("updated_at"),
            }
        self._overrides = overrides
        self._invalid_overrides = invalid
        self._rebuild()
        return invalid

    def _rebuild(self) -> None:
        """유효 역할 맵을 새로 만들어 **한 번에** 재바인딩한다.

        제자리 수정이 아니다 — 반쯤 지어진 맵을 다른 코루틴이 읽으면 안 된다.
        """
        merged = dict(self._base)
        for name, ov in self._overrides.items():
            fields = dict(ov["fields"])
            if "options" in fields:
                fields["options"] = dict(fields["options"])
            if ov["origin"] == "db":
                merged[name] = Role(
                    name=name,
                    model=fields["model"],
                    kind=fields.get("kind", DEFAULT_KIND),
                    lane=fields.get("lane", DEFAULT_LANE),
                    timeout=fields.get("timeout", DEFAULT_TIMEOUT),
                    system=None,          # 프롬프트는 호출자 소유
                    options=fields.get("options") or {},
                    max_prompt_chars=fields.get("max_prompt_chars"),
                )
            else:
                base = self._base.get(name)
                if base is None:
                    continue
                merged[name] = replace(base, **fields)
        self._roles = merged

    def has_override(self, name: str) -> bool:
        return name in self._overrides

    def is_db_role(self, name: str) -> bool:
        return self._overrides.get(name, {}).get("origin") == "db"

    def base_role(self, name: str) -> Role | None:
        return self._base.get(name)

    def role_view(self, name: str) -> dict | None:
        """UI 용 상세 — 유효값 + "기본값 대비 무엇이 바뀌었는지"."""
        role = self._roles.get(name)
        if role is None:
            return None
        ov = self._overrides.get(name)
        base = self._base.get(name)
        d = role.to_dict()
        d["options"] = dict(role.options)
        d["origin"] = ov["origin"] if ov else "yaml"
        d["overridden_fields"] = sorted(ov["fields"]) if ov else []
        d["base"] = base.to_dict() if base else None
        if base:
            d["base"]["options"] = dict(base.options)
        d["note"] = (ov or {}).get("note")
        d["updated_by"] = (ov or {}).get("updated_by")
        d["updated_at"] = (ov or {}).get("updated_at")
        return d

    def override_summary(self) -> dict:
        """/v1/status 용. 0 보다 크면 프로덕션이 roles.yaml 과 다르다는 뜻."""
        return {
            "count": len(self._overrides),
            "roles": sorted(self._overrides),
            "db_roles": sorted(n for n, o in self._overrides.items()
                               if o["origin"] == "db"),
            "invalid": list(self._invalid_overrides),
        }

    def yaml_snippet(self, name: str) -> str | None:
        """현재 유효값을 roles.yaml 에 붙여 넣을 수 있는 형태로.

        오버라이드는 DB 에만 있다 — 이 스니펫이 없으면 반년 뒤 프로덕션과 레포가
        왜 다른지 아무도 설명하지 못한다.
        """
        role = self._roles.get(name)
        if role is None:
            return None
        body: dict = {"model": role.model}
        if role.kind != DEFAULT_KIND:
            body["kind"] = role.kind
        body["lane"] = role.lane
        body["timeout"] = role.timeout
        if role.system:
            body["system"] = role.system
        if role.options:
            body["options"] = dict(role.options)
        if role.max_prompt_chars:
            body["max_prompt_chars"] = role.max_prompt_chars
        text = yaml.safe_dump({name: body}, allow_unicode=True, sort_keys=False)
        return "  " + text.replace("\n", "\n  ").rstrip() + "\n"

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
    # 값이 아니라 **어느 환경변수에서 왔는가**. 운영 화면이 "이 서비스가 죽은 건
    # .env 의 어느 줄이 비어서인가" 를 짚어 줄 수 있어야 한다.
    token_env: str = ""

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
                token_env=str(token_env),
            )
        if not services:
            raise ConfigError("services 가 비어 있습니다")
        return cls(services)
