"""기계가 읽는 계약(`/v1/meta` · OpenAPI) 회귀 테스트.

이 모듈의 존재 이유는 하나다 — **계약의 두 번째 사본이 조용히 어긋나는 것을
막는다.** `docs/integration.md` 의 엔드포인트 표와 `llm-gateway/README.md` 의 API
표는 둘 다 실제로 어긋났다(`/v1/meta`·`/v1/openapi.*` 가 빠진 채 방치됐다).
손으로 유지하는 목록은 반드시 어긋나므로, 여기서는 **실행 중인 라우터**와
스펙을 양방향으로 대조한다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

from app.main import build_app
from app.meta import ERROR_CODES, INLINE_ADMIN, PUBLIC_ROUTES, route_inventory
from app.scheduler import Scheduler
from tests.conftest import TOKEN_ALPHA, TOKEN_BETA, FakeOllama

H_ALPHA = {"Authorization": f"Bearer {TOKEN_ALPHA}"}   # allow_roles=*, admin
H_BETA = {"Authorization": f"Bearer {TOKEN_BETA}"}     # allow_roles=[fast], 비-admin

MAIN_PY = Path(__file__).resolve().parent.parent / "app" / "main.py"


@pytest.fixture
def app(store, roles, services):
    fake = FakeOllama()
    sched = Scheduler(store, roles, fake, poll_interval=0.01)
    return build_app(roles=roles, services=services, store=store,
                     client=fake, scheduler=sched)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


# --- 인증 ---
def test_meta_and_spec_require_a_token(client):
    for path in ("/v1/meta", "/v1/openapi.json", "/v1/openapi.yaml"):
        assert client.get(path).status_code == 401, path


# --- 라우트 ↔ 스펙 양방향 대조 (가장 중요한 테스트) ---
def test_every_consumer_route_is_in_the_spec(app, client):
    """앱에 있는 소비자 라우트가 스펙에 빠지면 실패한다."""
    spec = client.get("/v1/openapi.json", headers=H_ALPHA).json()
    in_spec = {(m.upper(), p)
               for p, ops in spec["paths"].items()
               for m in ops if m in ("get", "post", "put", "delete", "patch")}
    expected = {(e["method"], e["path"])
                for e in route_inventory(app.routes) if not e["admin"]}
    assert expected - in_spec == set(), "스펙에 빠진 소비자 라우트"


def test_spec_promises_nothing_the_app_lacks(app, client):
    """스펙에만 있고 앱에 없는 경로가 있으면 실패한다(거짓 약속)."""
    spec = client.get("/v1/openapi.json", headers=H_ALPHA).json()
    in_spec = {(m.upper(), p)
               for p, ops in spec["paths"].items()
               for m in ops if m in ("get", "post", "put", "delete", "patch")}
    real = {(e["method"], e["path"]) for e in route_inventory(app.routes)}
    assert in_spec - real == set(), "앱에 없는 경로를 스펙이 약속한다"


def test_admin_routes_are_listed_but_not_promised(app, client):
    """관리 경로는 admin 토큰에 목록으로만 나오고 paths 에는 없다."""
    spec = client.get("/v1/openapi.json", headers=H_ALPHA).json()
    listed = {(e["method"], e["path"]) for e in spec["x-admin-endpoints"]}
    admin = {(e["method"], e["path"])
             for e in route_inventory(app.routes) if e["admin"]}
    assert listed == admin
    assert not [p for p in spec["paths"] if p.startswith("/v1/admin/")]


def test_inline_admin_gate_is_not_mistaken_for_a_consumer_route(app):
    """POST /v1/models/requests 는 /v1/admin/* 이 아니지만 관리 전용이다.

    경로 접두사만 보면 이걸 소비자 API 로 오해한다. 실제 핸들러는 svc.admin 을
    요구하므로(main.decide_model_request) 재고가 admin 으로 분류해야 한다.
    """
    entry = next(e for e in route_inventory(app.routes)
                 if (e["method"], e["path"]) == ("POST", "/v1/models/requests"))
    assert entry["admin"] is True
    assert ("POST", "/v1/models/requests") in INLINE_ADMIN


def test_every_route_has_a_summary(app):
    """라우트를 추가하고 요약을 안 달면 실패한다 — 표가 조용히 비는 것을 막는다."""
    missing = [(e["method"], e["path"])
               for e in route_inventory(app.routes) if not e["summary"]]
    assert missing == []


def test_public_routes_are_marked_and_actually_public(app, client):
    inv = {(e["method"], e["path"]): e for e in route_inventory(app.routes)}
    for key in PUBLIC_ROUTES:
        assert inv[key]["auth"] is False
    assert client.get("/healthz").status_code == 200


# --- 오류 코드: 앱이 내는 것 == 표에 있는 것 ---
def test_every_error_code_emitted_by_main_is_documented():
    """`app/main.py` 를 grep 해 실제로 내는 코드를 전부 표와 대조한다.

    ERROR_CODES 는 손으로 관리하는 목록이라 코드가 추가되면 잊힌다. 실제로 9개가
    빠져 있었다. 이 테스트가 그 부류를 잡는다.
    """
    # {"error": "literal"} 형태만 뽑는다. str(exc)·f-string 은 코드가 아니다.
    emitted = set(re.findall(r'"error":\s*"([a-z_]+)"', MAIN_PY.read_text()))
    documented = {e["code"] for e in ERROR_CODES}
    assert emitted - documented == set(), "main.py 가 내는데 ERROR_CODES 에 없는 코드"


def test_documented_codes_are_marked_admin_correctly():
    """admin 전용으로 표시된 코드는 관리 핸들러에서만 나야 한다."""
    text = MAIN_PY.read_text()
    consumer_only = {e["code"] for e in ERROR_CODES if not e.get("admin")}
    # 소비자 코드는 전부 실제로 등장해야 한다(죽은 항목 금지).
    for code in consumer_only:
        assert f'"error": "{code}"' in text, f"쓰이지 않는 소비자 코드: {code}"


def test_error_schema_does_not_constrain_error_to_an_enum(client):
    """실제 응답의 error 에는 한국어 문장이 온다 — enum 이면 검증기가 거부한다."""
    spec = client.get("/v1/openapi.json", headers=H_ALPHA).json()
    err = spec["components"]["schemas"]["Error"]
    assert "enum" not in err["properties"]["error"]
    assert err["additionalProperties"] is True


def test_embed_error_shape_is_documented(client):
    """`/v1/embed` 실패는 Error 가 아니라 EmbedError 모양이다."""
    spec = client.get("/v1/openapi.json", headers=H_ALPHA).json()
    schemas = spec["components"]["schemas"]
    assert set(("status", "error", "retryable", "hint", "model_request")) <= set(
        schemas["EmbedError"]["properties"])
    responses = spec["paths"]["/v1/embed"]["post"]["responses"]
    for code in ("413", "502", "503"):
        refs = json.dumps(responses[code])
        assert "EmbedError" in refs, f"{code} 가 EmbedError 를 가리키지 않는다"


def test_real_embed_failure_body_validates_against_the_spec(store, roles, services):
    """백엔드 장애로 실제 503 을 받아 그 본문이 EmbedError 를 만족하는지 본다.

    이 본문의 error 에는 한국어 문장이 온다. 예전 스펙은 error 를 enum 으로
    묶어 두어 엄격한 검증기가 진짜 응답을 거부했다.
    """
    fake = FakeOllama()
    fake.embed_fail = True
    sched = Scheduler(store, roles, fake, poll_interval=0.01)
    app = build_app(roles=roles, services=services, store=store,
                    client=fake, scheduler=sched)
    with TestClient(app) as c:
        r = c.post("/v1/embed", json={"role": "vec", "input": "x"}, headers=H_ALPHA)
        spec = c.get("/v1/openapi.json", headers=H_ALPHA).json()

    assert r.status_code in (502, 503), r.text
    body = r.json()
    schema = spec["components"]["schemas"]["EmbedError"]
    assert body["status"] == "failed"
    # 코드가 아니라 문장이다 — 문서화된 코드 목록에 없어야 정상이다
    assert body["error"] not in {e["code"] for e in ERROR_CODES}
    assert set(schema["required"]) <= set(body)
    for key in body:
        assert key in schema["properties"] or schema["additionalProperties"] is True


# --- 토큰별로 결과가 달라진다 ---
def test_role_enum_is_scoped_to_the_token(client):
    alpha = client.get("/v1/openapi.json", headers=H_ALPHA).json()
    beta = client.get("/v1/openapi.json", headers=H_BETA).json()
    a_gen = alpha["components"]["schemas"]["GenerateRequest"]["properties"]["role"]["enum"]
    b_gen = beta["components"]["schemas"]["GenerateRequest"]["properties"]["role"]["enum"]
    assert "fast" in a_gen and "heavy" in a_gen
    assert b_gen == ["fast"]


def test_non_admin_sees_no_admin_surface(client):
    spec = client.get("/v1/openapi.json", headers=H_BETA).json()
    assert "x-admin-endpoints" not in spec
    assert not [p for p in spec["paths"] if p.startswith("/v1/admin/")]
    codes = {e["code"] for e in spec["x-error-codes"]}
    assert "in_use" not in codes and "backend_error" not in codes

    meta = client.get("/v1/meta", headers=H_BETA).json()
    assert meta["admin"] is False
    assert "admin_endpoints" not in meta
    assert {e["code"] for e in meta["error_codes"]} == codes
    assert [r["name"] for r in meta["roles"]] == ["fast"]


def test_admin_sees_admin_codes_and_endpoint_list(client):
    meta = client.get("/v1/meta", headers=H_ALPHA).json()
    assert meta["admin"] is True
    codes = {e["code"] for e in meta["error_codes"]}
    assert {"in_use", "backend_error", "in_progress"} <= codes
    paths = {e["path"] for e in meta["admin_endpoints"]}
    assert "/v1/admin/models" in paths


# --- 동적성: 런타임 변경이 즉시 반영된다 ---
def test_role_override_shows_up_in_meta_and_spec(client, roles, store):
    """운영자가 모델을 바꾸면 메타데이터·스펙이 즉시 따라간다."""
    before = client.get("/v1/meta", headers=H_ALPHA).json()
    old = next(r for r in before["roles"] if r["name"] == "fast")["model"]

    store.set_role_override("fast", origin="yaml", fields={"model": "swapped:1b"},
                            updated_by="test")
    roles.apply_overrides(store.list_role_overrides())

    after = client.get("/v1/meta", headers=H_ALPHA).json()
    assert next(r for r in after["roles"] if r["name"] == "fast")["model"] == "swapped:1b"
    assert old != "swapped:1b"


def test_new_db_role_appears_in_the_spec_enum(client, roles, store):
    store.set_role_override("brand_new", origin="db",
                            fields={"model": "small:1b", "kind": "generate"},
                            updated_by="test")
    roles.apply_overrides(store.list_role_overrides())
    spec = client.get("/v1/openapi.json", headers=H_ALPHA).json()
    enum = spec["components"]["schemas"]["GenerateRequest"]["properties"]["role"]["enum"]
    assert "brand_new" in enum


def test_limits_come_from_the_live_scheduler(store, roles, services):
    """env 를 다시 읽지 않고 주입된 스케줄러 값을 낸다."""
    fake = FakeOllama()
    sched = Scheduler(store, roles, fake, poll_interval=0.01,
                      max_retries=7, starvation_seconds=42, auto_install=False)
    app = build_app(roles=roles, services=services, store=store,
                    client=fake, scheduler=sched)
    with TestClient(app) as c:
        limits = c.get("/v1/meta", headers=H_ALPHA).json()["limits"]
    assert limits["max_retries"] == 7
    assert limits["starvation_seconds"] == 42
    assert limits["auto_install_models"] is False
    assert limits["lane_concurrency"] == 1
    assert limits["mem_budget_gb"] == roles.mem_budget_gb
    assert limits["recommended_poll_seconds"] == 2


# --- servers[] : /llm 이 빠지면 생성된 클라이언트가 404 를 맞는다 ---
def test_public_url_env_wins(client, monkeypatch):
    monkeypatch.setenv("LLMGW_PUBLIC_URL", "https://example.test/llm")
    spec = client.get("/v1/openapi.json", headers=H_ALPHA).json()
    assert spec["servers"][0]["url"] == "https://example.test/llm"


def test_forwarded_prefix_is_appended(client, monkeypatch):
    monkeypatch.delenv("LLMGW_PUBLIC_URL", raising=False)
    spec = client.get("/v1/openapi.json", headers={**H_ALPHA,
                                                   "X-Forwarded-Prefix": "/llm"}).json()
    assert spec["servers"][0]["url"].endswith("/llm")


def test_internal_server_is_offered_too(client, monkeypatch):
    monkeypatch.setenv("LLMGW_PUBLIC_URL", "https://example.test/llm")
    spec = client.get("/v1/openapi.json", headers=H_ALPHA).json()
    assert "http://127.0.0.1:8603" in [s["url"] for s in spec["servers"]]


# --- 형식 ---
def test_yaml_and_json_specs_are_the_same_document(client):
    as_json = client.get("/v1/openapi.json", headers=H_ALPHA).json()
    text = client.get("/v1/openapi.yaml", headers=H_ALPHA).text
    assert yaml.safe_load(text) == as_json


def test_every_ref_resolves(client):
    spec = client.get("/v1/openapi.json", headers=H_ALPHA).json()
    declared = set(spec["components"]["schemas"])
    used = set(re.findall(r'#/components/schemas/(\w+)', json.dumps(spec)))
    assert used - declared == set(), "정의되지 않은 $ref"


def test_required_openapi_keys_present(client):
    spec = client.get("/v1/openapi.json", headers=H_ALPHA).json()
    assert spec["openapi"].startswith("3.1")
    assert spec["info"]["version"] == spec["x-contract-version"]
    assert spec["paths"] and spec["servers"]


def test_content_disposition_only_on_download(client):
    plain = client.get("/v1/openapi.json", headers=H_ALPHA)
    assert "content-disposition" not in {k.lower() for k in plain.headers}
    dl = client.get("/v1/openapi.json?download=1", headers=H_ALPHA)
    assert "attachment" in dl.headers["content-disposition"]

    plain_y = client.get("/v1/openapi.yaml", headers=H_ALPHA)
    assert "content-disposition" not in {k.lower() for k in plain_y.headers}
    dl_y = client.get("/v1/openapi.yaml?download=1", headers=H_ALPHA)
    assert "attachment" in dl_y.headers["content-disposition"]
