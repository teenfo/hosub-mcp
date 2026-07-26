"""HTTP API 테스트 — 인증·권한·단일 응답 형태·잡 흐름."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.main import build_app
from app.scheduler import Scheduler
from tests.conftest import TOKEN_ALPHA, TOKEN_BETA, TOKEN_SLOW, FakeOllama

H_ALPHA = {"Authorization": f"Bearer {TOKEN_ALPHA}"}
H_BETA = {"Authorization": f"Bearer {TOKEN_BETA}"}


@pytest.fixture
def client(store, roles, services):
    fake = FakeOllama()
    sched = Scheduler(store, roles, fake, poll_interval=0.01)
    app = build_app(roles=roles, services=services, store=store,
                    client=fake, scheduler=sched)
    app.state.fake = fake
    with TestClient(app) as c:
        yield c


# --- 인증 ---
def test_healthz_is_public(client):
    assert client.get("/healthz").status_code == 200


def test_missing_token_401(client):
    r = client.post("/v1/generate", json={"role": "fast", "prompt": "x"})
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers


def test_wrong_token_401(client):
    r = client.get("/v1/roles", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_role_permission_enforced(client):
    # beta 는 fast 만 허용
    ok = client.post("/v1/generate", json={"role": "fast", "prompt": "x", "wait": 5},
                     headers=H_BETA)
    assert ok.status_code == 200
    denied = client.post("/v1/generate", json={"role": "heavy", "prompt": "x"},
                         headers=H_BETA)
    assert denied.status_code == 403
    assert denied.json()["allowed"] == ["fast"]


def test_rate_limit(store, roles, services):
    fake = FakeOllama()
    sched = Scheduler(store, roles, fake, poll_interval=0.01)
    app = build_app(roles=roles, services=services, store=store,
                    client=fake, scheduler=sched)
    with TestClient(app) as c:
        h = {"Authorization": f"Bearer {TOKEN_SLOW}"}   # 분당 2회
        assert c.get("/v1/roles", headers=h).status_code == 200
        assert c.get("/v1/roles", headers=h).status_code == 200
        assert c.get("/v1/roles", headers=h).status_code == 429


# --- 요청 검증 ---
def test_unknown_role_404(client):
    r = client.post("/v1/generate", json={"role": "nope", "prompt": "x"}, headers=H_ALPHA)
    assert r.status_code == 404
    assert "fast" in r.json()["known_roles"]


def test_missing_prompt_400(client):
    r = client.post("/v1/generate", json={"role": "fast"}, headers=H_ALPHA)
    assert r.status_code == 400


def test_prompt_too_long_413(client):
    r = client.post("/v1/generate",
                    json={"role": "tiny", "prompt": "x" * 100},   # max 50
                    headers=H_ALPHA)
    assert r.status_code == 413
    assert r.json()["limit"] == 50


# --- 단일 응답 형태 (설계 핵심) ---
def test_generate_returns_ok_with_job_id(client):
    r = client.post("/v1/generate",
                    json={"role": "fast", "prompt": "안녕", "wait": 5}, headers=H_ALPHA)
    assert r.status_code == 200
    b = r.json()
    assert b["status"] == "ok"
    assert b["job_id"]                       # 동기 응답에도 job_id 가 있다
    assert "[small]" in b["response"]
    assert b["role"] == "fast" and b["model"] == "small"


def test_wait_zero_returns_pending_same_shape(store, roles, services):
    """wait=0 → 즉시 pending. 형태는 성공 응답과 동일해야 한다."""
    fake = FakeOllama(delay=1.0)
    sched = Scheduler(store, roles, fake, poll_interval=0.01)
    app = build_app(roles=roles, services=services, store=store,
                    client=fake, scheduler=sched)
    with TestClient(app) as c:
        r = c.post("/v1/generate", json={"role": "fast", "prompt": "x", "wait": 0},
                   headers=H_ALPHA)
        b = r.json()
        assert r.status_code == 200            # 202 가 아니라 200 — 형태 통일
        assert b["status"] == "pending"
        assert b["job_id"] and b["response"] is None
        # 같은 job_id 로 폴링 가능
        got = c.get(f"/v1/jobs/{b['job_id']}", headers=H_ALPHA)
        assert got.status_code == 200
        assert got.json()["job_id"] == b["job_id"]


def test_caller_system_overrides_role_default(client):
    """프롬프트 소유권: 호출자 system 이 역할 기본값을 덮어쓴다."""
    client.post("/v1/generate",
                json={"role": "fast", "prompt": "x", "system": "호출자 지시", "wait": 5},
                headers=H_ALPHA)
    # 가짜 백엔드는 system 을 기록하지 않으므로 저장된 잡으로 확인
    jobs = client.get("/v1/jobs", headers=H_ALPHA).json()["jobs"]
    assert jobs[0]["job_id"]
    # 스토어 직접 확인
    store = client.app.state.store
    job = store.get_job(jobs[0]["job_id"])
    assert job["system"] == "호출자 지시"


def test_role_default_system_used_when_absent(client):
    client.post("/v1/generate", json={"role": "fast", "prompt": "x", "wait": 5},
                headers=H_ALPHA)
    jobs = client.get("/v1/jobs", headers=H_ALPHA).json()["jobs"]
    job = client.app.state.store.get_job(jobs[0]["job_id"])
    assert job["system"] == "기본 지시"


def test_metadata_roundtrip(client):
    r = client.post("/v1/generate",
                    json={"role": "fast", "prompt": "x", "wait": 5,
                          "metadata": {"session_id": 42}},
                    headers=H_ALPHA)
    assert r.json()["metadata"] == {"session_id": 42}


# --- 잡 관리 ---
def test_jobs_are_scoped_to_service(client):
    r = client.post("/v1/generate", json={"role": "fast", "prompt": "x", "wait": 5},
                    headers=H_ALPHA)
    job_id = r.json()["job_id"]
    # beta 는 alpha 의 잡을 볼 수 없다
    assert client.get(f"/v1/jobs/{job_id}", headers=H_BETA).status_code == 404
    assert client.get(f"/v1/jobs/{job_id}", headers=H_ALPHA).status_code == 200


def test_cancel_queued_job(store, roles, services):
    fake = FakeOllama(delay=2.0)
    sched = Scheduler(store, roles, fake, poll_interval=5.0)   # 사실상 안 집어감
    app = build_app(roles=roles, services=services, store=store,
                    client=fake, scheduler=sched)
    with TestClient(app) as c:
        r = c.post("/v1/generate", json={"role": "heavy", "prompt": "x", "wait": 0},
                   headers=H_ALPHA)
        job_id = r.json()["job_id"]
        assert c.delete(f"/v1/jobs/{job_id}", headers=H_ALPHA).status_code == 200
        assert c.get(f"/v1/jobs/{job_id}", headers=H_ALPHA).json()["status"] == "cancelled"
        # 두 번째 취소는 409
        assert c.delete(f"/v1/jobs/{job_id}", headers=H_ALPHA).status_code == 409


# --- 메타 ---
def test_roles_filtered_by_permission(client):
    all_roles = client.get("/v1/roles", headers=H_ALPHA).json()["roles"]
    assert len(all_roles) == 6          # 생성 5 + 임베딩 1
    beta_roles = client.get("/v1/roles", headers=H_BETA).json()["roles"]
    assert [r["name"] for r in beta_roles] == ["fast"]


def test_status_reports_backend_and_lanes(client):
    b = client.get("/v1/status", headers=H_ALPHA).json()
    assert b["backend"]["online"] is True
    assert set(b["lanes"]) == {"interactive", "batch"}
    assert b["mem_budget_gb"] == 40
    by_name = {r["name"]: r for r in b["roles"]}
    assert by_name["fast"]["model_available"] is True      # small 보유
    assert by_name["tiny"]["model_available"] is True


def test_status_graceful_when_backend_down(store, roles, services):
    fake = FakeOllama()
    fake.tags_fail = True
    sched = Scheduler(store, roles, fake, poll_interval=0.01)
    app = build_app(roles=roles, services=services, store=store,
                    client=fake, scheduler=sched)
    with TestClient(app) as c:
        b = c.get("/v1/status", headers=H_ALPHA).json()
        assert b["backend"]["online"] is False
        assert b["backend"]["error"]
        assert all(r["model_available"] is False for r in b["roles"])


# --- 임베딩 (/v1/embed — 유일하게 잡이 아닌 엔드포인트) ---
def test_embed_returns_vectors(client):
    fake = client.app.state.fake
    r = client.post("/v1/embed", headers=H_ALPHA,
                    json={"role": "vec", "input": ["첫 문장", "둘째 문장"]})
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "ok" and d["count"] == 2 and d["dimensions"] == 3
    assert d["model"] == "bge-m3"
    assert fake.embed_calls == [("bge-m3", ["첫 문장", "둘째 문장"])]


def test_embed_accepts_bare_string(client):
    fake = client.app.state.fake
    d = client.post("/v1/embed", headers=H_ALPHA,
                    json={"role": "vec", "input": "한 건"}).json()
    assert d["count"] == 1
    assert fake.embed_calls == [("bge-m3", ["한 건"])]


def test_embed_requires_auth(client):
    assert client.post("/v1/embed", json={"role": "vec", "input": "x"}).status_code == 401


def test_embed_rejects_generate_role(client):
    """역할 종류를 섞어 쓰면 조용히 이상한 결과가 나오는 대신 즉시 알린다."""
    r = client.post("/v1/embed", headers=H_ALPHA, json={"role": "fast", "input": "x"})
    assert r.status_code == 400
    assert r.json()["error"] == "wrong_role_kind"
    assert r.json()["embed_roles"] == ["vec"]


def test_generate_rejects_embed_role(client):
    r = client.post("/v1/generate", headers=H_ALPHA, json={"role": "vec", "prompt": "x"})
    assert r.status_code == 400
    assert r.json()["error"] == "wrong_role_kind"


def test_embed_respects_allow_roles(client):
    """beta 는 fast 만 허용 — 임베딩도 같은 권한 모델을 따른다."""
    assert client.post("/v1/embed", headers=H_BETA,
                       json={"role": "vec", "input": "x"}).status_code == 403


def test_embed_validates_input(client):
    for bad in [{}, {"role": "vec"}, {"role": "vec", "input": []},
                {"role": "vec", "input": [1, 2]}]:
        r = client.post("/v1/embed", headers=H_ALPHA, json=bad)
        assert r.status_code in (400, 404), bad


def test_embed_rejects_oversized_batch(client):
    r = client.post("/v1/embed", headers=H_ALPHA,
                    json={"role": "vec", "input": ["x"] * 300})
    assert r.status_code == 413 and r.json()["error"] == "batch_too_large"


def test_embed_backend_failure_is_retryable_503(client):
    client.app.state.fake.embed_fail = True
    r = client.post("/v1/embed", headers=H_ALPHA, json={"role": "vec", "input": "x"})
    assert r.status_code == 503                    # 맥 재부팅 등 — 나중에 다시
    assert r.json()["retryable"] is True


def test_embed_is_recorded_in_usage(client):
    client.post("/v1/embed", headers=H_ALPHA, json={"role": "vec", "input": "x"})
    usage = client.get("/v1/status", headers=H_ALPHA).json()["usage"]
    assert any(u["service"] == "alpha" and u["calls"] >= 1 for u in usage)
