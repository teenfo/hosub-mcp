"""역할 런타임 오버라이드 — 레이어링·검증·영속·잡 스냅샷.

핵심 회귀 3가지:
1. 오버라이드가 **같은 인스턴스의** /v1/roles·/v1/status 에 반영되는가
   (RoleConfig 는 라우트 클로저 ~10개와 스케줄러가 공유하는 하나의 객체다)
2. 오버라이드 **이전**에 만든 잡은 옛 모델 + 옛 옵션으로 도는가 (자기완결형 잡)
3. 잘못된 오버라이드 행이 있어도 기동되는가 (설정을 DB 로 옮긴 대가를 치르지 않기)
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.config import ConfigError, RoleConfig, validate_role_fields
from app.main import build_app
from app.scheduler import Scheduler
from app.store import Store
from tests.conftest import ROLES_RAW, TOKEN_ALPHA, TOKEN_BETA, FakeOllama

H = {"Authorization": f"Bearer {TOKEN_ALPHA}"}      # admin
H_BETA = {"Authorization": f"Bearer {TOKEN_BETA}"}  # 비 admin


@pytest.fixture
def client(store, roles, services):
    fake = FakeOllama()
    sched = Scheduler(store, roles, fake, poll_interval=0.01)
    app = build_app(roles=roles, services=services, store=store,
                    client=fake, scheduler=sched)
    app.state.fake = fake
    with TestClient(app) as c:
        yield c


# --- 필드 검증 ---
def test_validate_rejects_non_overridable_fields():
    with pytest.raises(ConfigError):
        validate_role_fields({"system": "새 프롬프트"})
    with pytest.raises(ConfigError):
        validate_role_fields({"kind": "embed"})       # allow_kind=False
    # 생성 시에는 kind 를 정할 수 있다
    assert validate_role_fields({"model": "m", "kind": "embed"},
                                allow_kind=True)["kind"] == "embed"


def test_validate_rejects_dangerous_model_names():
    for bad in ("http://evil/x", "../../etc/passwd", "a b", "a/b/c", "", "x..y"):
        with pytest.raises(ConfigError):
            validate_role_fields({"model": bad})
    assert validate_role_fields({"model": "library/qwen2.5:32b-instruct"})


def test_validate_bounds():
    with pytest.raises(ConfigError):
        validate_role_fields({"lane": "nope"})
    with pytest.raises(ConfigError):
        validate_role_fields({"timeout": 0})
    with pytest.raises(ConfigError):
        validate_role_fields({"timeout": 99999})
    with pytest.raises(ConfigError):
        validate_role_fields({"options": {"temperature": {"nested": 1}}})
    with pytest.raises(ConfigError):
        validate_role_fields({})                      # 변경할 필드 없음


# --- 레이어링 ---
def test_override_changes_effective_role_only(roles):
    assert roles.role("chat").model == "mid"
    roles.apply_overrides([
        {"role": "chat", "origin": "yaml", "fields": {"model": "big"}},
    ])
    assert roles.role("chat").model == "big"
    assert roles.base_role("chat").model == "mid"      # YAML 원본은 그대로
    assert roles.role("chat").timeout == 60            # 안 바꾼 필드는 유지
    assert roles.override_summary()["count"] == 1

    roles.apply_overrides([])                          # 되돌리기
    assert roles.role("chat").model == "mid"
    assert roles.override_summary()["count"] == 0


def test_db_role_created_and_removed(roles):
    roles.apply_overrides([
        {"role": "newbie", "origin": "db",
         "fields": {"model": "small", "lane": "interactive", "timeout": 15}},
    ])
    r = roles.role("newbie")
    assert r.model == "small" and r.lane == "interactive" and r.timeout == 15
    assert r.system is None                    # 프롬프트는 호출자 소유
    assert "newbie" in roles.role_names
    roles.apply_overrides([])
    assert roles.role("newbie") is None


def test_invalid_override_rows_are_skipped_not_fatal(roles):
    invalid = roles.apply_overrides([
        {"role": "chat", "origin": "yaml", "fields": {"model": "big"}},
        {"role": "chat2", "origin": "yaml", "fields": {"model": "x"}},   # YAML 에 없음
        {"role": "fast", "origin": "yaml", "fields": {"system": "탈취"}},  # 금지 필드
        {"role": "BAD NAME", "origin": "db", "fields": {"model": "small"}},
        {"role": "z", "origin": "무엇", "fields": {"model": "small"}},
    ])
    assert roles.role("chat").model == "big"   # 정상 행은 적용됨
    assert roles.role("fast").system == "기본 지시"
    assert {i["role"] for i in invalid} == {"chat2", "fast", "BAD NAME", "z"}
    assert len(roles.override_summary()["invalid"]) == 4


def test_roles_using_loose_matching(roles):
    roles.apply_overrides([
        {"role": "chat", "origin": "yaml", "fields": {"model": "qwen2.5:32b"}},
    ])
    assert roles.roles_using("qwen2.5:32b") == ["chat"]
    assert roles.roles_using("qwen2.5", loose=True) == ["chat"]
    assert roles.roles_using("qwen2.5") == []          # 엄격 매칭은 안 잡는다


def test_yaml_snippet_round_trips(roles):
    roles.apply_overrides([
        {"role": "fast", "origin": "yaml", "fields": {"model": "big", "timeout": 99}},
    ])
    snippet = roles.yaml_snippet("fast")
    assert "big" in snippet and "99" in snippet
    import yaml as _yaml
    parsed = _yaml.safe_load(snippet)
    raw = {**ROLES_RAW, "roles": {**ROLES_RAW["roles"], **parsed}}
    assert RoleConfig.from_dict(raw).role("fast").model == "big"


# --- 크기 추정 순서 ---
def test_size_order_yaml_beats_measured(roles):
    # YAML 은 런타임 메모리 기준(big=30), 실측은 디스크 크기다. YAML 이 이겨야 한다.
    roles.set_measured_sizes({"big": 19.9})
    assert roles.model_size_gb("big") == 30

    # YAML 에 없는 모델은 실측 + 헤드룸(×1.15+0.5)
    roles.set_measured_sizes({"newmodel": 10.0})
    assert roles.model_size_gb("newmodel") == 12.0

    # 실측도 없으면 Nb 휴리스틱 → 마지막이 20GB 고정값
    assert roles.model_size_gb("foo:7b") == 6.0      # 7 × 0.65 + 1.5
    assert roles.model_size_gb("mystery") == 20.0


def test_catalog_size_beats_measured(roles):
    roles.set_catalog_sizes({"mystery": 4.0})
    roles.set_measured_sizes({"mystery": 100.0})
    assert roles.model_size_gb("mystery") == 4.0


# --- HTTP ---
def test_admin_override_visible_in_same_instance(client):
    r = client.post("/v1/admin/roles", headers=H,
                    json={"role": "chat", "fields": {"model": "big"}})
    assert r.status_code == 200, r.text
    assert r.json()["role"]["model"] == "big"
    assert r.json()["role"]["overridden_fields"] == ["model"]

    # 라우트 클로저가 같은 RoleConfig 를 보는지 — 여기가 핵심
    roles_out = client.get("/v1/roles", headers=H).json()["roles"]
    assert next(x for x in roles_out if x["name"] == "chat")["model"] == "big"
    st = client.get("/v1/status", headers=H).json()
    assert st["overrides"]["count"] == 1
    assert st["overrides"]["roles"] == ["chat"]


def test_non_admin_forbidden_on_all_admin_routes(client):
    assert client.get("/v1/admin/roles", headers=H_BETA).status_code == 403
    assert client.post("/v1/admin/roles", headers=H_BETA,
                       json={"role": "chat", "fields": {"model": "big"}}
                       ).status_code == 403
    assert client.delete("/v1/admin/roles?role=chat",
                         headers=H_BETA).status_code == 403
    assert client.get("/v1/admin/audit", headers=H_BETA).status_code == 403
    # 토큰 없이는 401
    assert client.get("/v1/admin/roles").status_code == 401


def test_revert_restores_yaml_default(client):
    client.post("/v1/admin/roles", headers=H,
                json={"role": "chat", "fields": {"model": "big"}})
    r = client.delete("/v1/admin/roles?role=chat", headers=H)
    assert r.status_code == 200
    assert r.json()["removed"] is False
    assert r.json()["role"]["model"] == "mid"
    assert client.delete("/v1/admin/roles?role=chat", headers=H).status_code == 404


def test_partial_edits_merge_instead_of_replacing(client):
    """두 번째 편집이 첫 번째를 지우면 안 된다 — 모델 교체가 조용히 사라진다."""
    client.post("/v1/admin/roles", headers=H,
                json={"role": "chat", "fields": {"model": "big"}})
    client.post("/v1/admin/roles", headers=H,
                json={"role": "chat", "fields": {"timeout": 99}})
    view = client.get("/v1/admin/roles", headers=H).json()["roles"]
    chat = next(x for x in view if x["name"] == "chat")
    assert chat["model"] == "big" and chat["timeout"] == 99
    assert chat["overridden_fields"] == ["model", "timeout"]


def test_values_equal_to_default_are_not_overrides(client):
    """기본값과 같은 값을 보내면 오버라이드로 세지 않는다.

    UI 는 폼 전체(model·lane·timeout·options)를 보낸다. 그대로 저장하면
    아무것도 안 바꿨는데 드리프트 배지가 "roles.yaml 과 다름"이라고 거짓말한다.
    """
    base = {"model": "mid", "lane": "interactive", "timeout": 60, "options": {}}
    r = client.post("/v1/admin/roles", headers=H, json={"role": "chat", "fields": base})
    assert r.json()["role"]["overridden_fields"] == []
    assert r.json()["overrides"]["count"] == 0

    # 한 필드만 실제로 다르면 그것만 잡힌다
    r = client.post("/v1/admin/roles", headers=H,
                    json={"role": "chat", "fields": dict(base, model="big")})
    assert r.json()["role"]["overridden_fields"] == ["model"]

    # 다시 기본값으로 되돌리면 오버라이드가 사라진다(행까지 삭제)
    r = client.post("/v1/admin/roles", headers=H, json={"role": "chat", "fields": base})
    assert r.json()["overrides"]["count"] == 0
    assert client.get("/v1/status", headers=H).json()["overrides"]["count"] == 0


def test_override_to_missing_model_suggests_install(client):
    r = client.post("/v1/admin/roles", headers=H,
                    json={"role": "chat", "fields": {"model": "qwen2.5:14b"}})
    body = r.json()
    assert body["install_required"]["model"] == "qwen2.5:14b"


def test_admin_audit_records_changes(client):
    client.post("/v1/admin/roles", headers=H,
                json={"role": "chat", "fields": {"model": "big"}, "note": "실험"})
    client.delete("/v1/admin/roles?role=chat", headers=H)
    audit = client.get("/v1/admin/audit", headers=H).json()["audit"]
    assert [a["action"] for a in audit] == ["role_revert", "role_override"]
    assert audit[0]["actor"] == "alpha"


def test_bad_fields_rejected_over_http(client):
    r = client.post("/v1/admin/roles", headers=H,
                    json={"role": "chat", "fields": {"system": "탈취"}})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_fields"


def test_new_db_role_requires_valid_name(client):
    r = client.post("/v1/admin/roles", headers=H,
                    json={"role": "Bad Name", "fields": {"model": "small"}})
    assert r.status_code == 400
    r = client.post("/v1/admin/roles", headers=H,
                    json={"role": "newbie", "fields": {"model": "small"}})
    assert r.status_code == 200
    assert r.json()["role"]["origin"] == "db"
    # 부분 수정이 기존 필드를 지우지 않는다
    client.post("/v1/admin/roles", headers=H,
                json={"role": "newbie", "fields": {"timeout": 42}})
    view = client.get("/v1/admin/roles", headers=H).json()["roles"]
    newbie = next(x for x in view if x["name"] == "newbie")
    assert newbie["model"] == "small" and newbie["timeout"] == 42
    # 삭제하면 역할 자체가 사라진다
    assert client.delete("/v1/admin/roles?role=newbie", headers=H).json()["removed"]
    assert "newbie" not in [x["name"] for x in
                            client.get("/v1/roles", headers=H).json()["roles"]]


# --- 영속 + 잡 자기완결성 ---
def test_overrides_survive_restart(tmp_path, roles, services):
    db = tmp_path / "p.db"
    fake = FakeOllama()

    def make():
        cfg = RoleConfig.from_dict(ROLES_RAW)
        st = Store(db)
        sched = Scheduler(st, cfg, fake, poll_interval=0.01)
        return cfg, build_app(roles=cfg, services=services, store=st,
                              client=fake, scheduler=sched)

    _cfg, app = make()
    with TestClient(app) as c:
        c.post("/v1/admin/roles", headers=H,
               json={"role": "chat", "fields": {"model": "big"}})

    cfg2, app2 = make()          # 재기동 = 새 RoleConfig + 새 Store
    with TestClient(app2) as c:
        assert cfg2.role("chat").model == "big"
        assert c.get("/v1/status", headers=H).json()["overrides"]["count"] == 1


def test_queued_job_keeps_old_model_and_options(store, roles, services):
    """오버라이드 전에 만든 잡은 옛 모델 **+ 옛 옵션**으로 돈다.

    모델만 스냅샷하고 옵션을 실행 시점에 읽으면 "옛 모델 + 새 옵션" 이라는
    조용한 오염이 생긴다. 지금까지 잠재해 있던 결함이라 여기서 못박는다.
    """
    fake = FakeOllama(delay=0.3)
    sched = Scheduler(store, roles, fake, poll_interval=0.01)
    app = build_app(roles=roles, services=services, store=store,
                    client=fake, scheduler=sched)
    with TestClient(app) as c:
        first = c.post("/v1/generate", headers=H,
                       json={"role": "fast", "prompt": "a", "wait": 0}).json()
        c.post("/v1/admin/roles", headers=H,
               json={"role": "fast",
                     "fields": {"model": "mid", "options": {"temperature": 0.9}}})
        second = c.post("/v1/generate", headers=H,
                        json={"role": "fast", "prompt": "b", "wait": 0}).json()

    assert first["model"] == "small"
    assert second["model"] == "mid"
    old = store.get_job(first["job_id"])
    new = store.get_job(second["job_id"])
    assert old["options"] == {"temperature": 0.3}     # 오버라이드 이전 값
    assert new["options"] == {"temperature": 0.9}
    assert old["timeout_s"] == 30


def test_migrate_adds_columns_to_old_db(tmp_path):
    """구 스키마 DB 를 열어도 컬럼이 붙고 기존 행이 살아남는가."""
    import sqlite3
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE jobs (id TEXT PRIMARY KEY, service TEXT NOT NULL,
          role TEXT NOT NULL, model TEXT, lane TEXT NOT NULL, status TEXT NOT NULL,
          priority INTEGER NOT NULL DEFAULT 0, prompt TEXT NOT NULL, system TEXT,
          response TEXT, error TEXT, metadata_json TEXT,
          attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
          started_at TEXT, finished_at TEXT);
        INSERT INTO jobs (id, service, role, model, lane, status, prompt, created_at)
          VALUES ('old1','alpha','fast','small','interactive','queued','hi','2024-01-01');
    """)
    conn.commit(); conn.close()

    store = Store(db)
    job = store.get_job("old1")
    assert job["prompt"] == "hi"
    assert job["options"] is None and job["timeout_s"] is None   # 폴백 신호
    # 두 번 열어도 안전(멱등)
    assert Store(db).get_job("old1")["prompt"] == "hi"
