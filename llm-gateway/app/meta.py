"""소비자용 기계가 읽는 계약 — 메타데이터와 OpenAPI 스펙.

`docs/integration.md` 는 사람이 읽는 문서다. 이 모듈은 같은 계약을 **코드
생성기가 먹을 수 있는 형태**로 낸다:

- `GET /v1/meta`        → 역할·한도·오류코드·엔드포인트 (JSON)
- `GET /v1/openapi.json` / `.yaml` → OpenAPI 3.1 스펙

**정적 파일로 두지 않고 매번 생성한다.** 역할은 런타임에 바뀌고(오버라이드),
한도는 스케줄러 인스턴스에서 읽으며 허용 역할은 토큰마다 다르다. 손으로 쓴
스펙은 반드시 실제와 어긋나고, 그 어긋남은 소비자가 코드를 생성한 뒤에야 드러난다.

**엔드포인트 목록도 손으로 적지 않는다.** `app.routes` 를 순회해 재고를 만들고
(`route_inventory`), 이 모듈의 사전은 사람이 읽는 *요약*만 채운다. 라우트를
추가하고 요약을 안 달면 테스트가 실패한다 — 문서 표가 조용히 거짓이 되는 경로를
구조적으로 막는다(`llm-gateway/README.md`·`integration.md` 의 표가 실제로 그렇게
어긋났다).

**토큰마다 결과가 다르다.** `role` enum 에 그 서비스가 쓸 수 있는 역할만 넣으므로,
생성된 클라이언트는 못 쓰는 역할을 애초에 노출하지 않는다. 관리 엔드포인트는
admin 토큰일 때만 `x-admin-endpoints` 로 알린다 — 공개 경로에서는 404 라
소비자 `paths` 에 있으면 거짓말이 된다.
"""

from __future__ import annotations

# 소비자 계약 버전. 응답 모양이나 의미가 **깨지는** 변경에만 올린다.
# 필드 추가는 전방 호환이므로 올리지 않는다(클라이언트가 미지 필드를 무시한다).
CONTRACT_VERSION = "1.0"

# 잡 상태 — /v1/generate·/v1/jobs 응답의 status 가 가질 수 있는 전부
JOB_STATUSES = ("ok", "pending", "failed", "cancelled")

# 오류 코드 → (HTTP, 뜻, 재시도해도 되는가).
#
# **이 표는 참고용이다. 스키마의 enum 으로 쓰지 않는다.** 응답의 error 필드에는
# 코드가 아니라 사람이 읽는 문장이 들어가는 경로가 실제로 있다(임베딩 백엔드
# 장애 3곳, 실패한 잡의 error). enum 으로 걸면 엄격한 검증기와 코드 생성기가
# 진짜 응답을 거부한다.
#
# admin: True 인 항목은 관리 엔드포인트에서만 난다 — 비-admin 토큰에는 걸러 낸다.
ERROR_CODES = [
    {"code": "unauthorized", "http": 401, "retryable": False,
     "detail": "토큰이 없거나 틀렸다. Authorization: Bearer <토큰>"},
    {"code": "forbidden", "http": 403, "retryable": False,
     "detail": "그 역할을 쓸 권한이 없다. allowed 필드에 쓸 수 있는 역할이 온다"},
    {"code": "rate_limited", "http": 429, "retryable": True,
     "detail": "분당 한도 초과. 잠시 뒤 재시도"},
    {"code": "unknown_role", "http": 404, "retryable": False,
     "detail": "없는 역할. known_roles 필드 확인"},
    {"code": "wrong_role_kind", "http": 400, "retryable": False,
     "detail": "생성 역할을 /v1/embed 에 (또는 반대로) 보냈다"},
    {"code": "invalid_request", "http": 400, "retryable": False,
     "detail": "role·prompt 누락 등 본문 오류"},
    {"code": "prompt_too_long", "http": 413, "retryable": False,
     "detail": "limit·got 필드가 온다. 잘라서 다시 보낼 것"},
    {"code": "batch_too_large", "http": 413, "retryable": False,
     "detail": "임베딩 입력 개수 초과"},
    {"code": "input_too_long", "http": 413, "retryable": False,
     "detail": "임베딩 입력이 모델 컨텍스트를 넘었다. 조용히 잘리지 않고 거부된다"},
    {"code": "not_cancellable", "http": 409, "retryable": False,
     "detail": "대기 중인 본인 잡만 취소할 수 있다"},
    {"code": "not_found", "http": 404, "retryable": False,
     "detail": "없는 잡이거나 다른 서비스의 잡"},
    # --- 아래는 관리 엔드포인트 전용 ---
    {"code": "invalid_model", "http": 400, "retryable": False, "admin": True,
     "detail": "모델 이름이 규칙에 맞지 않는다"},
    {"code": "invalid_role_name", "http": 400, "retryable": False, "admin": True,
     "detail": "새 역할 이름은 소문자·숫자·밑줄 2~32자"},
    {"code": "invalid_fields", "http": 400, "retryable": False, "admin": True,
     "detail": "덮어쓸 수 없는 필드이거나 값이 범위를 벗어났다"},
    {"code": "too_large", "http": 400, "retryable": False, "admin": True,
     "detail": "추정 크기가 메모리 예산을 넘는다. est_size_gb·mem_budget_gb 확인"},
    {"code": "conflict", "http": 409, "retryable": False, "admin": True,
     "detail": "이미 있는 역할 이름"},
    {"code": "in_use", "http": 409, "retryable": False, "admin": True,
     "detail": "쓰는 역할·잡이 있어 삭제할 수 없다. blockers 필드 확인"},
    {"code": "in_progress", "http": 409, "retryable": True, "admin": True,
     "detail": "같은 작업이 이미 진행 중이다"},
    {"code": "not_installed", "http": 409, "retryable": False, "admin": True,
     "detail": "먼저 설치해야 한다. models 필드 확인"},
    {"code": "backend_error", "http": 503, "retryable": True, "admin": True,
     "detail": "맥의 Ollama 가 거부·타임아웃했다. retryable 필드 확인"},
]

# 경로별 요약. **재고는 여기가 아니라 app.routes 가 만든다** — 이 사전은
# 사람이 읽는 한 줄만 채운다. 라우트가 있는데 여기 없으면 테스트가 실패한다.
ENDPOINT_SUMMARIES: dict[tuple[str, str], str] = {
    ("GET", "/healthz"): "헬스체크(인증 불필요)",
    ("POST", "/v1/generate"): "생성. wait 초까지 기다림(0이면 즉시 pending)",
    ("POST", "/v1/embed"): "임베딩 벡터. 유일하게 잡 큐를 타지 않는다",
    ("GET", "/v1/jobs"): "잡 목록(본인 서비스)",
    ("GET", "/v1/jobs/{job_id}"): "잡 조회(본인 서비스 것만)",
    ("DELETE", "/v1/jobs/{job_id}"): "취소(대기 중인 것만)",
    ("GET", "/v1/roles"): "쓸 수 있는 역할·모델",
    ("GET", "/v1/status"): "백엔드·레인 큐·사용량",
    ("GET", "/v1/models/requests"): "모델 설치 요청 목록",
    ("POST", "/v1/models/requests"): "[관리] 모델 설치 요청 승인·거부",
    ("GET", "/v1/integration"): "사람이 읽는 통합 가이드(마크다운)",
    ("GET", "/v1/meta"): "이 메타데이터",
    ("GET", "/v1/openapi.json"): "OpenAPI 3.1 스펙(JSON)",
    ("GET", "/v1/openapi.yaml"): "OpenAPI 3.1 스펙(YAML)",
    ("GET", "/v1/admin/roles"): "[관리] 역할 유효값·기본값 대비 차이",
    ("POST", "/v1/admin/roles"): "[관리] 역할 오버라이드 저장·신규 역할 생성",
    ("DELETE", "/v1/admin/roles"): "[관리] 오버라이드 해제(기본값 복귀)",
    ("GET", "/v1/admin/models"): "[관리] 설치된 모델·용량·삭제 차단 사유",
    ("DELETE", "/v1/admin/models"): "[관리] 맥에서 모델 삭제",
    ("POST", "/v1/admin/models/install"): "[관리] 설치 지시",
    ("GET", "/v1/admin/catalog"): "[관리] 내장 카탈로그 검색",
    ("POST", "/v1/admin/compare"): "[관리] 모델 A/B 비교 실행",
    ("GET", "/v1/admin/compare"): "[관리] 비교 이력",
    ("GET", "/v1/admin/compare/{run_id}"): "[관리] 비교 결과",
    ("GET", "/v1/admin/audit"): "[관리] 관리 작업 감사 로그",
}

# 경로 접두사로 판별할 수 없는 관리 게이트.
# POST /v1/models/requests 는 /v1/admin/* 이 아니지만 핸들러 안에서 svc.admin 을
# 요구한다(main.decide_model_request). 접두사만 보면 이걸 소비자 API 로 오해한다.
INLINE_ADMIN: set[tuple[str, str]] = {("POST", "/v1/models/requests")}

# 인증이 필요 없는 경로
PUBLIC_ROUTES: set[tuple[str, str]] = {("GET", "/healthz")}

# 소비자 `paths` 에 스키마를 쓰지 않는 경로(재고에는 있으나 OpenAPI 문서 대상이
# 아닌 것). 관리 계열은 x-admin-endpoints 로 가므로 여기 넣지 않는다.
_NO_SPEC: set[tuple[str, str]] = set()


def route_inventory(routes) -> list[dict]:
    """Starlette 라우트 표에서 `(method, path)` 재고를 만든다.

    엔드포인트 목록의 **단일 소스**다. 손으로 적은 목록은 반드시 어긋나므로
    (`integration.md` 의 표가 실제로 그랬다) 실행 중인 라우터에서 유도한다.
    `Mount`(정적 파일 등)는 메서드가 없어 건너뛴다.
    """
    out: list[dict] = []
    for route in routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods:
            continue
        for method in sorted(methods):
            # HEAD·OPTIONS 는 Starlette 가 자동으로 붙인다 — 계약이 아니다.
            if method in ("HEAD", "OPTIONS"):
                continue
            key = (method, path)
            out.append({
                "method": method,
                "path": path,
                "admin": path.startswith("/v1/admin/") or key in INLINE_ADMIN,
                "auth": key not in PUBLIC_ROUTES,
                "summary": ENDPOINT_SUMMARIES.get(key, ""),
            })
    return sorted(out, key=lambda e: (e["path"], e["method"]))


def _error_codes(svc) -> list[dict]:
    """비-admin 토큰에는 관리 전용 코드를 숨긴다(역할 필터링과 같은 방식)."""
    return [e for e in ERROR_CODES if svc.admin or not e.get("admin")]


def _role_entry(role) -> dict:
    d = role.to_dict()
    # /v1/roles·/v1/status 는 to_dict() 를 그대로 쓰므로 options 가 없다.
    # 여기만 붙인다 — 코드 생성 시 모델 옵션(temperature 등)을 알아야 한다.
    d["options"] = dict(role.options)
    return d


def build_meta(*, roles, svc, limits: dict, routes=()) -> dict:
    """`GET /v1/meta` 본문. 코드 생성·입력 검증에 필요한 사실만 담는다."""
    allowed = [r for r in roles.roles if svc.may_use(r.name)]
    inventory = route_inventory(routes)
    consumer = [e for e in inventory if not e["admin"]]
    return {
        "contract_version": CONTRACT_VERSION,
        "service": svc.name,
        "admin": bool(svc.admin),
        # 역할 이름이 계약이고 모델은 정책이다 — 모델은 런타임에 바뀔 수 있다.
        "roles": [_role_entry(r) for r in allowed],
        "generate_roles": [r.name for r in allowed if r.kind == "generate"],
        "embed_roles": [r.name for r in allowed if r.kind == "embed"],
        "limits": dict(limits, rate_limit_per_min=svc.rate_limit_per_min),
        "job_statuses": list(JOB_STATUSES),
        "error_codes": _error_codes(svc),
        # 실행 중인 라우터에서 유도한다 — 손 목록이 아니다.
        "endpoints": [{"method": e["method"], "path": e["path"],
                       "summary": e["summary"], "auth": e["auth"]}
                      for e in consumer],
        **({"admin_endpoints": [{"method": e["method"], "path": e["path"],
                                 "summary": e["summary"]}
                                for e in inventory if e["admin"]]}
           if svc.admin else {}),
        "links": {
            "integration_markdown": "/v1/integration",
            "openapi_json": "/v1/openapi.json",
            "openapi_yaml": "/v1/openapi.yaml",
            "docs": "/v1/docs",
        },
        "notes": [
            "역할의 모델은 운영자가 런타임에 바꿀 수 있다. 모델 이름을 "
            "하드코딩하지 말고 필요하면 GET /v1/roles 로 확인하라.",
            "이미 큐에 들어간 잡은 교체 후에도 옛 모델로 실행된다 "
            "(생성 시점의 모델·옵션·타임아웃을 스냅샷한다).",
            "응답에 모르는 필드가 추가될 수 있다 — 무시하고 넘어갈 수 있게 짜라. "
            "필드 추가로는 contract_version 이 올라가지 않는다.",
            "실패한 잡의 error 와 임베딩 백엔드 오류의 error 는 **사람이 읽는 "
            "문장**이다. error_codes 의 코드가 아니므로 문자열로 분기하지 말고 "
            "HTTP 상태와 retryable 을 보라.",
            "generate 의 model 필드는 관리 전용이다(소비자가 보내면 403).",
            "/v1/admin/* 은 공개 경로에서 404 다.",
        ],
    }


# --------------------------------------------------------------------------
# OpenAPI 3.1
# --------------------------------------------------------------------------
def _err(description: str, schema: str = "Error") -> dict:
    return {"description": description,
            "content": {"application/json": {
                "schema": {"$ref": f"#/components/schemas/{schema}"}}}}


def _embed_err(description: str) -> dict:
    """임베딩 실패는 Error 가 아니라 EmbedError 모양이다(§Error 주석 참고)."""
    return {"description": description,
            "content": {"application/json": {"schema": {"anyOf": [
                {"$ref": "#/components/schemas/Error"},
                {"$ref": "#/components/schemas/EmbedError"},
            ]}}}}


def _schemas(limits: dict, gen_roles: list[str], emb_roles: list[str]) -> dict:
    return {
        "Error": {
            "type": "object",
            "description": (
                "오류 본문. **error 는 enum 이 아니다** — 검증·권한 오류에서는 "
                "기계 코드(x-error-codes 참고)가 오지만, 백엔드 장애나 모델 "
                "미설치에서는 사람이 읽는 문장이 온다. 분기는 HTTP 상태와 "
                "retryable 로 하라."
            ),
            "properties": {
                "error": {"type": "string",
                          "description": "코드 또는 사람이 읽는 문장"},
                "detail": {"type": "string"},
                "hint": {"type": "string", "description": "운영자가 할 조치"},
                "retryable": {"type": "boolean"},
                "limit": {"type": "integer"}, "got": {"type": "integer"},
                "allowed": {"type": "array", "items": {"type": "string"}},
                "known_roles": {"type": "array", "items": {"type": "string"}},
                "embed_roles": {"type": "array", "items": {"type": "string"}},
                "models": {"type": "array", "items": {"type": "string"}},
                "role": {"type": "string"},
                "model_request": {"type": "string",
                                  "description": "설치 요청 상태(pending·pulling 등)"},
                "request": {"type": "object", "additionalProperties": True},
                "blockers": {"type": "array",
                             "items": {"type": "object", "additionalProperties": True},
                             "description": "kind 는 준정형, message 는 문장이다"},
                "est_size_gb": {"type": "number"},
                "mem_budget_gb": {"type": "number"},
                "run_id": {"type": "string"},
            },
            "required": ["error"],
            # 필드는 계속 늘어난다 — 닫아 두면 검증기가 진짜 응답을 거부한다.
            "additionalProperties": True,
        },
        "EmbedError": {
            "type": "object",
            "description": (
                "`/v1/embed` 의 실패 본문. Error 와 달리 status 를 들고 오고 "
                "error 에는 **항상 사람이 읽는 문장**이 온다(모델 미설치·컨텍스트 "
                "초과·백엔드 장애). 재시도 판단은 retryable 로 한다."
            ),
            "properties": {
                "status": {"type": "string", "enum": ["failed"]},
                "error": {"type": "string", "description": "사람이 읽는 문장"},
                "retryable": {"type": "boolean"},
                "hint": {"type": "string"},
                "model_request": {"type": "string"},
            },
            "required": ["status", "error"],
            "additionalProperties": True,
        },
        "Job": {
            "type": "object",
            "description": "모든 생성 경로가 공유하는 단일 응답 형태. "
                           "status 만 보면 되고 동기/비동기를 분기할 필요가 없다.",
            "properties": {
                "job_id": {"type": "string", "description": "항상 있다 — pending 이면 이걸로 폴링"},
                "status": {"type": "string", "enum": list(JOB_STATUSES)},
                "response": {"type": ["string", "null"], "description": "status=ok 일 때만"},
                "error": {"type": ["string", "null"],
                          "description": "status=failed 일 때만. **기계 코드가 아니라 "
                                         "사람이 읽는 문장이다** — 문자열로 분기하지 말 것"},
                "role": {"type": "string"},
                "model": {"type": ["string", "null"],
                          "description": "호출마다 다를 수 있다(운영자가 역할의 모델을 바꿀 수 있다)"},
                "lane": {"type": "string", "enum": list(limits.get("lanes") or
                                                       ["interactive", "batch"])},
                "attempts": {"type": "integer"},
                "metadata": {"type": "object", "additionalProperties": True},
                "queue_position": {"type": ["integer", "null"],
                                   "description": "pending 일 때 앞에 몇 개(0 = 다음 차례). "
                                                  "generate·단건 조회에만 있고 "
                                                  "GET /v1/jobs 목록 항목에는 없다"},
                "created_at": {"type": "string", "format": "date-time"},
                "started_at": {"type": ["string", "null"], "format": "date-time"},
                "finished_at": {"type": ["string", "null"], "format": "date-time"},
            },
            "required": ["job_id", "status"],
            "additionalProperties": True,
        },
        "GenerateRequest": {
            "type": "object",
            "properties": {
                "role": {"type": "string", "enum": gen_roles,
                         "description": "이 토큰이 쓸 수 있는 생성 역할"},
                "prompt": {"type": "string", "maxLength": limits["max_prompt_chars"]},
                "system": {"type": ["string", "null"],
                           "description": "있으면 역할 기본 프롬프트를 덮는다. "
                                          "빈 문자열은 '시스템 프롬프트 없음'(null 과 다르다)"},
                "wait": {"type": "number", "minimum": 0,
                         "maximum": limits["max_wait_seconds"],
                         "default": limits["default_wait_seconds"],
                         "description": "0 이면 즉시 pending + job_id"},
                "priority": {"type": "integer", "default": 0, "description": "클수록 먼저"},
                "metadata": {"type": "object", "additionalProperties": True,
                             "description": "그대로 되돌아온다"},
            },
            "required": ["role", "prompt"],
        },
        "EmbedRequest": {
            "type": "object",
            "properties": {
                "role": {"type": "string", "enum": emb_roles},
                "input": {
                    "description": "문자열 또는 문자열 배열. 길이 상한은 배열 "
                                   "전체의 **합**에 걸린다(항목별이 아니다)",
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"},
                         "maxItems": limits["max_embed_batch"]},
                    ],
                },
            },
            "required": ["role", "input"],
        },
        "EmbedResponse": {
            "type": "object",
            "description": "성공 응답. 실패는 EmbedError 를 본다.",
            "properties": {
                "status": {"type": "string", "enum": ["ok"]},
                "model": {"type": "string"},
                "embeddings": {"type": "array",
                               "items": {"type": "array", "items": {"type": "number"}}},
                "count": {"type": "integer"},
                "dimensions": {"type": "integer",
                               "description": "역할의 모델이 정한다 — 모델이 바뀌면 변한다"},
                "duration_ms": {"type": ["integer", "null"]},
            },
            "additionalProperties": True,
        },
        "Role": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "model": {"type": "string"},
                "kind": {"type": "string", "enum": ["generate", "embed"]},
                "lane": {"type": "string", "enum": list(limits.get("lanes") or
                                                       ["interactive", "batch"])},
                "timeout": {"type": "integer"},
                "has_default_system": {"type": "boolean",
                                       "description": "기본 시스템 프롬프트 유무만 알린다 "
                                                      "(내용은 내지 않는다)"},
                "max_prompt_chars": {"type": ["integer", "null"]},
                "options": {"type": "object", "additionalProperties": True,
                            "description": "GET /v1/meta 에만 있다 — "
                                           "/v1/roles·/v1/status 응답에는 없다"},
            },
            "additionalProperties": True,
        },
    }


def _consumer_paths(limits: dict, gen_roles: list[str], emb_roles: list[str]) -> dict:
    """소비자 계약. 관리 계열은 여기 넣지 않는다(x-admin-endpoints 로 간다)."""
    job_ok = {"description": "잡 상태(성공·대기·실패 모두 200)",
              "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Job"}}}}
    errs = {
        "401": _err("unauthorized"), "403": _err("forbidden"),
        "429": _err("rate_limited"),
    }
    spec_get = {
        "responses": {"200": {"description": "OpenAPI 3.1 스펙",
                              "content": {"application/json": {
                                  "schema": {"type": "object", "additionalProperties": True}}}},
                      **errs},
        "parameters": [{"name": "download", "in": "query", "required": False,
                        "schema": {"type": "string", "enum": ["1"]},
                        "description": "붙이면 Content-Disposition: attachment 로 내린다"}],
    }

    return {
        "/v1/generate": {"post": {
            "operationId": "generate", "summary": "생성 요청",
            "description": "**모든 요청은 잡이다.** wait 초까지 기다리고, 안 끝나면 "
                           "status=pending + job_id 를 준다. 서버리스라면 wait=0 + 폴링.",
            "requestBody": {"required": True, "content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/GenerateRequest"}}}},
            "responses": {"200": job_ok, "400": _err("invalid_request·wrong_role_kind"),
                          "404": _err("unknown_role"), "413": _err("prompt_too_long"),
                          **errs},
        }},
        "/v1/embed": {"post": {
            "operationId": "embed", "summary": "임베딩 벡터",
            "description": "유일하게 잡 큐를 타지 않고 동기 응답한다. 입력이 모델 "
                           "컨텍스트를 넘으면 조용히 잘리지 않고 413 으로 거부된다. "
                           "실패 본문은 EmbedError 모양이고 error 에 문장이 온다.",
            "requestBody": {"required": True, "content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/EmbedRequest"}}}},
            "responses": {
                "200": {"description": "벡터", "content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/EmbedResponse"}}}},
                "400": _err("invalid_request·wrong_role_kind"),
                "404": _err("unknown_role"),
                "413": _embed_err("batch_too_large·input_too_long 또는 모델 컨텍스트 초과"),
                "502": _embed_err("백엔드 응답 이상(재시도 무의미할 수 있다)"),
                "503": _embed_err("모델 미설치 또는 백엔드 일시 장애(retryable 확인)"),
                **errs},
        }},
        "/v1/jobs": {"get": {
            "operationId": "listJobs", "summary": "잡 목록(본인 서비스)",
            "description": "항목에는 queue_position 이 없다 — 단건 조회를 쓴다.",
            "parameters": [
                {"name": "status", "in": "query", "required": False,
                 "schema": {"type": "string",
                            "enum": ["queued", "running", "succeeded", "failed", "cancelled"]}},
                {"name": "limit", "in": "query", "required": False,
                 "schema": {"type": "integer", "default": 20, "maximum": 200}},
            ],
            "responses": {"200": {"description": "목록", "content": {"application/json": {
                "schema": {"type": "object", "properties": {"jobs": {
                    "type": "array", "items": {"$ref": "#/components/schemas/Job"}}}}}}},
                **errs},
        }},
        "/v1/jobs/{job_id}": {
            "get": {
                "operationId": "getJob", "summary": "잡 조회",
                "parameters": [{"name": "job_id", "in": "path", "required": True,
                                "schema": {"type": "string"}}],
                "responses": {"200": job_ok, "404": _err("not_found"), **errs},
            },
            "delete": {
                "operationId": "cancelJob", "summary": "잡 취소(대기 중인 것만)",
                "parameters": [{"name": "job_id", "in": "path", "required": True,
                                "schema": {"type": "string"}}],
                "responses": {
                    # Job 이 아니다 — job_id 도 없이 status 만 온다.
                    "200": {"description": "취소됨", "content": {"application/json": {
                        "schema": {"type": "object", "properties": {
                            "status": {"type": "string", "enum": ["cancelled"]}},
                            "required": ["status"]}}}},
                    "409": _err("not_cancellable"), **errs},
            },
        },
        "/v1/roles": {"get": {
            "operationId": "listRoles", "summary": "쓸 수 있는 역할·모델",
            "description": "역할의 모델은 런타임에 바뀔 수 있다 — 현재 값이 필요하면 "
                           "여기서 읽는다. 이 응답의 Role 에는 options 가 없다.",
            "responses": {"200": {"description": "역할 목록",
                "content": {"application/json": {"schema": {"type": "object", "properties": {
                    "service": {"type": "string"},
                    "roles": {"type": "array",
                              "items": {"$ref": "#/components/schemas/Role"}}}}}}},
                **errs},
        }},
        "/v1/status": {"get": {
            "operationId": "getStatus", "summary": "백엔드·레인 큐·사용량",
            "responses": {"200": {"description": "상태(필드가 늘어날 수 있다)",
                "content": {"application/json": {
                    "schema": {"type": "object", "additionalProperties": True}}}},
                **errs},
        }},
        "/v1/models/requests": {"get": {
            "operationId": "listModelRequests", "summary": "모델 설치 요청 목록",
            "description": "잡이 오래 pending 이면 여기서 이유를 확인한다(승인 대기 등). "
                           "같은 경로의 POST 는 관리 전용이다(x-admin-endpoints).",
            "responses": {"200": {"description": "요청 목록",
                "content": {"application/json": {
                    "schema": {"type": "object", "additionalProperties": True}}}},
                **errs},
        }},
        "/v1/meta": {"get": {
            "operationId": "getMeta", "summary": "기계가 읽는 계약 메타데이터",
            "responses": {"200": {"description": "메타데이터",
                "content": {"application/json": {
                    "schema": {"type": "object", "additionalProperties": True}}}},
                **errs},
        }},
        "/v1/openapi.json": {"get": dict(
            spec_get, operationId="getOpenapiJson", summary="이 스펙(JSON)")},
        "/v1/openapi.yaml": {"get": dict(
            spec_get, operationId="getOpenapiYaml", summary="이 스펙(YAML)",
            responses={"200": {"description": "OpenAPI 3.1 스펙",
                               "content": {"application/yaml": {"schema": {"type": "string"}}}},
                       **errs})},
        "/v1/integration": {"get": {
            "operationId": "getIntegrationDoc",
            "summary": "사람이 읽는 통합 가이드(마크다운)",
            "responses": {"200": {"description": "마크다운",
                                  "content": {"text/markdown": {"schema": {"type": "string"}}}},
                          **errs},
        }},
        "/healthz": {"get": {
            "operationId": "healthz", "summary": "헬스체크(인증 불필요)",
            "security": [],
            "responses": {"200": {"description": "ok", "content": {"application/json": {
                "schema": {"type": "object",
                           "properties": {"ok": {"type": "boolean"}}}}}}},
        }},
    }


def build_openapi(*, roles, svc, limits: dict, servers=None, routes=()) -> dict:
    """OpenAPI 3.1 스펙. 역할 enum 이 토큰 권한에 맞춰 좁혀진다."""
    allowed = [r for r in roles.roles if svc.may_use(r.name)]
    gen_roles = [r.name for r in allowed if r.kind == "generate"]
    emb_roles = [r.name for r in allowed if r.kind == "embed"]

    paths = _consumer_paths(limits, gen_roles, emb_roles)
    inventory = route_inventory(routes)

    spec = {
        "openapi": "3.1.0",
        "info": {
            "title": "hosub 공유 LLM 게이트웨이",
            "version": CONTRACT_VERSION,
            "summary": "역할 기반 LLM 게이트웨이. 모든 요청은 잡이다.",
            "description": (
                "역할 이름이 계약이고 모델은 정책이다 — 운영자가 역할의 모델을 "
                "런타임에 바꿀 수 있으므로 모델 이름을 하드코딩하지 말 것.\n\n"
                f"이 스펙은 **{svc.name}** 토큰 기준으로 **실행 중인 게이트웨이가 "
                "생성**했다. role enum 에는 이 토큰이 쓸 수 있는 역할만 들어 있고, "
                "모델 값은 지금 걸려 있는 것이다.\n\n"
                "사람이 읽는 가이드: `GET /v1/integration`"
            ),
        },
        "servers": list(servers or [{"url": "/"}]),
        "security": [{"bearerAuth": []}],
        "components": {
            "securitySchemes": {"bearerAuth": {
                "type": "http", "scheme": "bearer",
                "description": "서비스별 토큰. 프로덕션 코드에서 브라우저에 넣지 말 것 "
                               "(토큰이 노출된다) — 자기 서버 라우트에서만 호출한다.",
            }},
            "schemas": _schemas(limits, gen_roles, emb_roles),
        },
        "paths": paths,
        # 코드가 아니라 참고 표다 — Error.error 의 enum 으로 쓰지 않는다(§Error 주석).
        "x-error-codes": _error_codes(svc),
        "x-contract-version": CONTRACT_VERSION,
    }

    # 관리 엔드포인트는 스키마 없이 **목록만** 낸다. 공개 경로에서는 리버스
    # 프록시가 404 로 잘라내므로 소비자 paths 에 넣으면 없는 API 를 약속하는
    # 셈이고, 절반만 적으면 계약이 두 곳에서 어긋난다. 목록은 거짓말할 수 없다.
    if svc.admin:
        spec["x-admin-endpoints"] = [
            {"method": e["method"], "path": e["path"], "summary": e["summary"]}
            for e in inventory if e["admin"]
        ]
    return spec
