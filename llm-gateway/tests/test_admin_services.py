"""소비자 토큰 관측 — `/v1/admin/services` 와 전체 값 열람.

토큰 값을 그대로 내보내는 경로는 게이트웨이 전체에서 하나뿐이다. 여기서 지키는
것은 세 가지다 — **목록에는 전체 값이 절대 실리지 않는다**, **한 번에 하나만
준다**, **열람이 감사에 남는다**.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from starlette.testclient import TestClient

from app.main import build_app
from app.scheduler import Scheduler
from tests.conftest import TOKEN_ALPHA, TOKEN_BETA, FakeOllama

H_ADMIN = {"Authorization": f"Bearer {TOKEN_ALPHA}"}   # admin: true
H_PLAIN = {"Authorization": f"Bearer {TOKEN_BETA}"}    # admin: false


@pytest.fixture
def client(store, roles, services):
    fake = FakeOllama()
    sched = Scheduler(store, roles, fake, poll_interval=0.01)
    app = build_app(roles=roles, services=services, store=store,
                    client=fake, scheduler=sched)
    app.state.store = store
    with TestClient(app) as c:
        yield c


# --- 권한 ---
def test_both_routes_require_admin(client):
    for path in ("/v1/admin/services", "/v1/admin/services/alpha/token"):
        assert client.get(path).status_code == 401, path
        assert client.get(path, headers=H_PLAIN).status_code == 403, path


# --- 목록에는 전체 값이 없다 ---
def test_listing_never_carries_a_full_token(client):
    body = client.get("/v1/admin/services", headers=H_ADMIN).text
    assert TOKEN_ALPHA not in body
    assert TOKEN_BETA not in body


def test_listing_masks_and_fingerprints(client):
    d = client.get("/v1/admin/services", headers=H_ADMIN).json()
    alpha = next(s for s in d["services"] if s["name"] == "alpha")
    assert alpha["token_set"] is True
    assert alpha["token_len"] == len(TOKEN_ALPHA)
    assert alpha["token_masked"] == f"{TOKEN_ALPHA[:6]}…{TOKEN_ALPHA[-6:]}"
    assert alpha["token_sha256"] == hashlib.sha256(
        TOKEN_ALPHA.encode()).hexdigest()[:12]
    # 지문은 값을 복원할 수 없어야 한다(앞 12자만)
    assert len(alpha["token_sha256"]) == 12


def test_listing_shows_permissions_and_env_name(client):
    d = client.get("/v1/admin/services", headers=H_ADMIN).json()
    by = {s["name"]: s for s in d["services"]}
    assert by["alpha"]["admin"] is True
    assert by["beta"]["admin"] is False
    assert by["beta"]["allow_roles"] == ["fast"]
    # .env 의 어느 줄이 비었는지 짚어 주려면 env 이름이 필요하다
    assert by["alpha"]["token_env"] == "TOK_ALPHA"


def test_empty_token_is_flagged_not_hidden(store, roles, monkeypatch):
    """토큰이 비면 그 서비스는 조용히 죽는다 — 그 사실이 보여야 한다."""
    from app.config import ServiceConfig

    monkeypatch.setenv("TOK_ALPHA", TOKEN_ALPHA)
    monkeypatch.delenv("TOK_BETA", raising=False)
    services = ServiceConfig.from_dict({"services": {
        "alpha": {"token_env": "TOK_ALPHA", "allow_roles": ["*"], "admin": True},
        "beta": {"token_env": "TOK_BETA", "allow_roles": ["fast"]},
    }})
    fake = FakeOllama()
    sched = Scheduler(store, roles, fake, poll_interval=0.01)
    app = build_app(roles=roles, services=services, store=store,
                    client=fake, scheduler=sched)
    with TestClient(app) as c:
        d = c.get("/v1/admin/services", headers=H_ADMIN).json()
    beta = next(s for s in d["services"] if s["name"] == "beta")
    assert beta["token_set"] is False
    assert beta["token_masked"] == "" and beta["token_sha256"] is None
    assert beta["token_env"] == "TOK_BETA"


# --- 사용 이력 ---
def test_usage_is_joined_per_service(client, store):
    store.record_usage(service="alpha", role="fast", model="small",
                       eval_count=3, duration_ms=10, status="succeeded")
    store.record_usage(service="alpha", role="fast", model="small",
                       eval_count=3, duration_ms=10, status="succeeded")
    d = client.get("/v1/admin/services", headers=H_ADMIN).json()
    alpha = next(s for s in d["services"] if s["name"] == "alpha")
    assert alpha["calls_total"] == 2
    assert alpha["calls_window"] == 2
    assert alpha["last_used_at"]
    # 한 번도 안 쓴 서비스는 0 이고 last_used_at 이 None 이다 — 죽은 토큰 후보
    beta = next(s for s in d["services"] if s["name"] == "beta")
    assert beta["calls_total"] == 0 and beta["last_used_at"] is None


def test_orphan_usage_is_surfaced(client, store):
    """services.yaml 에서 사라졌는데 이력만 남은 이름은 눈에 띄어야 한다."""
    store.record_usage(service="gone_service", role="fast", model="small",
                       eval_count=1, duration_ms=1, status="succeeded")
    d = client.get("/v1/admin/services", headers=H_ADMIN).json()
    assert d["orphan_usage"] == ["gone_service"]
    assert "gone_service" not in [s["name"] for s in d["services"]]


# --- 전체 값 열람 ---
def test_reveal_returns_exactly_one_token(client):
    d = client.get("/v1/admin/services/beta/token", headers=H_ADMIN).json()
    assert d["service"] == "beta"
    assert d["token"] == TOKEN_BETA
    # 다른 서비스 토큰이 딸려 오지 않는다
    assert TOKEN_ALPHA not in json.dumps(d)


def test_reveal_is_audited_without_the_value(client, store):
    client.get("/v1/admin/services/beta/token", headers=H_ADMIN)
    rows = [r for r in store.list_audit(20) if r["action"] == "token_reveal"]
    assert len(rows) == 1
    assert rows[0]["target"] == "beta" and rows[0]["actor"] == "alpha"
    # 감사 로그가 비밀 저장소가 되면 안 된다
    assert TOKEN_BETA not in json.dumps(rows[0], default=str)


def test_reveal_of_unknown_service_is_404(client):
    r = client.get("/v1/admin/services/nope/token", headers=H_ADMIN)
    assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_reveal_of_empty_token_explains_which_env_is_missing(
        store, roles, monkeypatch):
    from app.config import ServiceConfig

    # alpha 는 살아 있어야 관리 요청이 통과한다. beta 만 비운다.
    monkeypatch.setenv("TOK_ALPHA", TOKEN_ALPHA)
    monkeypatch.delenv("TOK_BETA", raising=False)
    services = ServiceConfig.from_dict({"services": {
        "alpha": {"token_env": "TOK_ALPHA", "allow_roles": ["*"], "admin": True},
        "beta": {"token_env": "TOK_BETA", "allow_roles": ["fast"]},
    }})
    fake = FakeOllama()
    sched = Scheduler(store, roles, fake, poll_interval=0.01)
    app = build_app(roles=roles, services=services, store=store,
                    client=fake, scheduler=sched)
    with TestClient(app) as c:
        r = c.get("/v1/admin/services/beta/token", headers=H_ADMIN)
    assert r.status_code == 404
    assert "TOK_BETA" in r.json()["detail"]
    # 실패한 열람 시도도 남는다
    rows = [x for x in store.list_audit(20) if x["action"] == "token_reveal"]
    assert rows and rows[0]["outcome"] == "empty"


# --- 공개 스펙에 새지 않는다 ---
def test_routes_are_admin_only_in_the_spec(client):
    consumer = client.get("/v1/openapi.json", headers=H_PLAIN).json()
    assert not [p for p in consumer["paths"] if "services" in p]
    assert "x-admin-endpoints" not in consumer

    admin = client.get("/v1/openapi.json", headers=H_ADMIN).json()
    listed = {(e["method"], e["path"]) for e in admin["x-admin-endpoints"]}
    assert ("GET", "/v1/admin/services") in listed
    assert ("GET", "/v1/admin/services/{name}/token") in listed
    # 스키마 없는 목록이므로 paths 에는 없어야 한다
    assert not [p for p in admin["paths"] if "services" in p]
