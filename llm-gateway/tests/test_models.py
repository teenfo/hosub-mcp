"""미설치 모델 자동 요청 → 승인 → 설치 → 잡 재개 흐름.

핵심 회귀 포인트:
  1) 미설치 모델의 잡이 **레인을 막지 않는다** (뒤의 정상 잡이 먼저 끝난다)
  2) 승인하면 게이트웨이가 스스로 pull 하고, 대기하던 잡이 이어서 성공한다
  3) 거부/설치실패면 무한 대기가 아니라 명확한 오류로 잡을 끝낸다
  4) 백엔드가 죽어 목록을 모를 때는 아무것도 "없다"고 단정하지 않는다
"""

from __future__ import annotations

import asyncio
import time

import pytest
from starlette.testclient import TestClient

from app.config import RoleConfig, ServiceConfig
from app.main import build_app
from app.scheduler import Scheduler, model_matches
from app.store import (
    FAILED,
    MR_APPROVED,
    MR_FAILED,
    MR_PENDING,
    MR_READY,
    MR_REJECTED,
    QUEUED,
    SUCCEEDED,
)
from tests.conftest import TOKEN_ALPHA, TOKEN_BETA, FakeOllama

# pytest.ini 의 asyncio_mode=auto 가 async 테스트를 알아서 처리한다.

# "novel" 역할만 맥에 없는 모델(newbie)을 쓴다
ROLES_RAW = {
    "backend": {"base_url": "http://mac.test:11434"},
    "mem_budget_gb": 40,
    "model_sizes_gb": {"small": 5, "newbie": 9},
    "roles": {
        "fast":  {"model": "small", "lane": "interactive", "timeout": 30},
        "novel": {"model": "newbie", "lane": "interactive", "timeout": 30},
        "novel2": {"model": "newbie", "lane": "batch", "timeout": 30},
    },
}

SERVICES_RAW = {
    "services": {
        # alpha = 운영 주체(승인 권한), beta = 일반 소비자
        "alpha": {"token_env": "TOK_ALPHA", "allow_roles": ["*"],
                  "rate_limit_per_min": 200, "admin": True},
        "beta":  {"token_env": "TOK_BETA", "allow_roles": ["*"],
                  "rate_limit_per_min": 200},
    }
}


@pytest.fixture
def mroles():
    return RoleConfig.from_dict(ROLES_RAW)


@pytest.fixture
def mservices(monkeypatch):
    monkeypatch.setenv("TOK_ALPHA", TOKEN_ALPHA)
    monkeypatch.setenv("TOK_BETA", TOKEN_BETA)
    return ServiceConfig.from_dict(SERVICES_RAW)


def mk_job(store, role, model, lane, service="beta"):
    return store.create_job(service=service, role=role, model=model, lane=lane,
                            prompt="p", system=None)


async def wait_until(fn, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        await asyncio.sleep(interval)
    return False


def fake_backend(**kw):
    return FakeOllama(models=["small"], **kw)


def mk_sched(store, mroles, fake, **kw):
    kw.setdefault("poll_interval", 0.01)
    kw.setdefault("models_refresh", 0.05)
    return Scheduler(store, mroles, fake, **kw)


# --------------------------------------------------------------------------
# 스케줄러
# --------------------------------------------------------------------------
async def test_missing_model_creates_request_and_does_not_block_lane(
    store, mroles
):
    fake = fake_backend()
    sched = mk_sched(store, mroles, fake)
    await sched.start()
    try:
        blocked = mk_job(store, "novel", "newbie", "interactive")
        later = mk_job(store, "fast", "small", "interactive")

        # 뒤에 들어온 정상 잡이 먼저 끝난다 (레인이 막히지 않았다)
        assert await wait_until(
            lambda: store.get_job(later["id"])["status"] == SUCCEEDED
        )
        assert store.get_job(blocked["id"])["status"] == QUEUED

        # 설치 요청이 자동으로 만들어졌다
        assert await wait_until(lambda: store.get_model_request("newbie") is not None)
        req = store.get_model_request("newbie")
        assert req["status"] == MR_PENDING
        assert req["requested_by"] == "beta"
        assert req["roles"] == ["novel", "novel2"]
        assert req["est_size_gb"] == 9
        assert fake.pulled == []          # 승인 전에는 절대 설치하지 않는다
    finally:
        await sched.stop()


async def test_approval_pulls_model_and_resumes_waiting_job(store, mroles):
    fake = fake_backend()
    sched = mk_sched(store, mroles, fake)
    await sched.start()
    try:
        job = mk_job(store, "novel", "newbie", "interactive")
        assert await wait_until(lambda: store.get_model_request("newbie") is not None)

        store.set_model_request_status("newbie", MR_APPROVED)

        assert await wait_until(
            lambda: store.get_job(job["id"])["status"] == SUCCEEDED, timeout=8
        )
        assert fake.pulled == ["newbie"]
        req = store.get_model_request("newbie")
        assert req["status"] == MR_READY
        assert req["progress"] == 100
    finally:
        await sched.stop()


async def test_rejection_fails_waiting_jobs_with_clear_error(store, mroles):
    fake = fake_backend()
    sched = mk_sched(store, mroles, fake)
    await sched.start()
    try:
        job = mk_job(store, "novel", "newbie", "interactive")
        assert await wait_until(lambda: store.get_model_request("newbie") is not None)

        store.set_model_request_status("newbie", MR_REJECTED)

        assert await wait_until(
            lambda: store.get_job(job["id"])["status"] == FAILED
        )
        err = store.get_job(job["id"])["error"]
        assert "newbie" in err and "거부" in err
        assert fake.pulled == []
    finally:
        await sched.stop()


async def test_pull_failure_marks_request_failed_and_fails_jobs(store, mroles):
    fake = fake_backend()
    fake.pull_fail = {"newbie"}
    sched = mk_sched(store, mroles, fake)
    await sched.start()
    try:
        job = mk_job(store, "novel", "newbie", "interactive")
        assert await wait_until(lambda: store.get_model_request("newbie") is not None)
        store.set_model_request_status("newbie", MR_APPROVED)

        assert await wait_until(
            lambda: store.get_model_request("newbie")["status"] == MR_FAILED
        )
        assert await wait_until(
            lambda: store.get_job(job["id"])["status"] == FAILED
        )
        assert "설치 실패" in store.get_job(job["id"])["error"]
    finally:
        await sched.stop()


async def test_backend_down_does_not_declare_models_missing(store, mroles):
    """목록을 모를 때 잡을 설치 대기로 돌려버리면 안 된다."""
    fake = fake_backend()
    fake.tags_fail = True
    sched = mk_sched(store, mroles, fake)
    await sched.start()
    try:
        job = mk_job(store, "novel", "newbie", "interactive")
        await asyncio.sleep(0.4)
        assert store.get_model_request("newbie") is None
        assert sched.available_models is None
        # 설치 요청 대신 평범하게 실행을 시도했다(가짜 백엔드라 성공)
        assert await wait_until(lambda: store.get_job(job["id"])["status"] == SUCCEEDED)
    finally:
        await sched.stop()


async def test_auto_install_can_be_disabled(store, mroles):
    fake = fake_backend()
    sched = mk_sched(store, mroles, fake, auto_install=False)
    await sched.start()
    try:
        mk_job(store, "novel", "newbie", "interactive")
        await asyncio.sleep(0.3)
        assert store.get_model_request("newbie") is None
        assert sched.available_models is None
    finally:
        await sched.stop()


async def test_model_larger_than_budget_fails_instead_of_waiting_forever(
    store, mroles
):
    """예산을 다 비워도 안 들어가는 모델은 조용히 큐에 쌓이면 안 된다."""
    fake = FakeOllama(models=["small", "huge"])
    sched = mk_sched(store, mroles, fake)
    await sched.start()
    try:
        # mem_budget_gb=40, huge 는 태그 추정으로 80*0.65+1.5 = 53.5GB
        job = mk_job(store, "novel", "huge:80b", "interactive")
        assert await wait_until(lambda: store.get_job(job["id"])["status"] == FAILED)
        err = store.get_job(job["id"])["error"]
        assert "메모리 예산" in err and "40" in err
        # 예산 초과는 설치로 해결되지 않으므로 설치 요청도 만들지 않는다
        assert store.get_model_request("huge:80b") is None
    finally:
        await sched.stop()


def test_model_matches_tag_forms():
    assert model_matches("qwen2.5:7b", "qwen2.5:7b")
    assert model_matches("qwen2.5", "qwen2.5:latest")
    assert model_matches("qwen2.5:7b", "qwen2.5")
    assert not model_matches("qwen2.5", "qwen3:7b")
    assert not model_matches("qwen2.5:7b", "qwen2.5:14b")


# --------------------------------------------------------------------------
# 저장소
# --------------------------------------------------------------------------
def test_ensure_model_request_is_idempotent(store):
    a = store.ensure_model_request("m", requested_by="beta", roles=["x"], est_size_gb=3)
    b = store.ensure_model_request("m", requested_by="gamma", roles=["x", "y"],
                                   est_size_gb=3)
    assert a["created_at"] == b["created_at"]
    assert b["requested_by"] == "beta"      # 최초 요청자를 유지
    assert b["roles"] == ["x", "y"]         # 역할 목록은 갱신
    assert len(store.list_model_requests()) == 1


def test_rejected_request_is_not_reopened_automatically(store):
    store.ensure_model_request("m", requested_by="beta", roles=[], est_size_gb=3)
    store.set_model_request_status("m", MR_REJECTED)
    again = store.ensure_model_request("m", requested_by="beta", roles=[], est_size_gb=3)
    assert again["status"] == MR_REJECTED


def test_ready_model_that_vanished_is_reopened(store):
    store.ensure_model_request("m", requested_by="beta", roles=[], est_size_gb=3)
    store.set_model_request_status("m", MR_READY, progress=100)
    again = store.ensure_model_request("m", requested_by="beta", roles=[], est_size_gb=3)
    assert again["status"] == MR_PENDING
    assert again["finished_at"] is None


def test_claim_approved_model_is_atomic(store):
    store.ensure_model_request("m", requested_by="beta", roles=[], est_size_gb=3)
    assert store.claim_approved_model() is None      # pending 은 안 집는다
    store.set_model_request_status("m", MR_APPROVED)
    first = store.claim_approved_model()
    assert first["model"] == "m" and first["status"] == "pulling"
    assert store.claim_approved_model() is None      # 두 번 집히지 않는다


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
def _client(store, mroles, mservices, fake):
    # 스케줄러 루프 없이 순수 API 만 검증 (auto_install 로 인한 경합 제거)
    sched = Scheduler(store, mroles, fake, poll_interval=0.01, auto_install=False)
    return TestClient(build_app(roles=mroles, services=mservices, store=store,
                                client=fake, scheduler=sched))


def test_api_lists_requests_and_flags_permission(store, mroles, mservices):
    store.ensure_model_request("newbie", requested_by="beta", roles=["novel"],
                               est_size_gb=9)
    with _client(store, mroles, mservices, fake_backend()) as c:
        r = c.get("/v1/models/requests",
                  headers={"Authorization": f"Bearer {TOKEN_ALPHA}"})
        assert r.status_code == 200
        body = r.json()
        assert body["can_decide"] is True
        assert [q["model"] for q in body["requests"]] == ["newbie"]

        r = c.get("/v1/models/requests",
                  headers={"Authorization": f"Bearer {TOKEN_BETA}"})
        assert r.json()["can_decide"] is False


def test_api_only_admin_may_decide(store, mroles, mservices):
    store.ensure_model_request("newbie", requested_by="beta", roles=[], est_size_gb=9)
    with _client(store, mroles, mservices, fake_backend()) as c:
        r = c.post("/v1/models/requests",
                   headers={"Authorization": f"Bearer {TOKEN_BETA}"},
                   json={"model": "newbie", "action": "approve"})
        assert r.status_code == 403
        assert store.get_model_request("newbie")["status"] == MR_PENDING

        r = c.post("/v1/models/requests",
                   headers={"Authorization": f"Bearer {TOKEN_ALPHA}"},
                   json={"model": "newbie", "action": "approve"})
        assert r.status_code == 200
        assert r.json()["request"]["status"] == MR_APPROVED


def test_api_reject_and_reapprove_after_failure(store, mroles, mservices):
    store.ensure_model_request("newbie", requested_by="beta", roles=[], est_size_gb=9)
    with _client(store, mroles, mservices, fake_backend()) as c:
        h = {"Authorization": f"Bearer {TOKEN_ALPHA}"}
        assert c.post("/v1/models/requests", headers=h,
                      json={"model": "newbie", "action": "reject"}).status_code == 200
        assert store.get_model_request("newbie")["status"] == MR_REJECTED

        # 실패했던 요청을 다시 승인하면 이전 오류 흔적이 지워진다
        store.set_model_request_status("newbie", MR_FAILED, error="디스크 부족")
        r = c.post("/v1/models/requests", headers=h,
                   json={"model": "newbie", "action": "approve"})
        assert r.json()["request"]["status"] == MR_APPROVED
        assert not r.json()["request"]["error"]


def test_api_rejects_bad_input(store, mroles, mservices):
    with _client(store, mroles, mservices, fake_backend()) as c:
        h = {"Authorization": f"Bearer {TOKEN_ALPHA}"}
        assert c.post("/v1/models/requests", headers=h,
                      json={"model": "x", "action": "nope"}).status_code == 400
        assert c.post("/v1/models/requests", headers=h,
                      json={"model": "ghost", "action": "approve"}).status_code == 404

    store.ensure_model_request("busy", requested_by="beta", roles=[], est_size_gb=1)
    store.set_model_request_status("busy", MR_APPROVED)
    with _client(store, mroles, mservices, fake_backend()) as c:
        r = c.post("/v1/models/requests",
                   headers={"Authorization": f"Bearer {TOKEN_ALPHA}"},
                   json={"model": "busy", "action": "reject"})
        assert r.status_code == 409       # 설치 진행 중은 되돌리지 않는다


def test_api_requires_auth(store, mroles, mservices):
    with _client(store, mroles, mservices, fake_backend()) as c:
        assert c.get("/v1/models/requests").status_code == 401
        assert c.post("/v1/models/requests",
                      json={"model": "x", "action": "approve"}).status_code == 401


def test_status_reports_pending_model_requests(store, mroles, mservices):
    store.ensure_model_request("newbie", requested_by="beta", roles=[], est_size_gb=9)
    with _client(store, mroles, mservices, fake_backend()) as c:
        body = c.get("/v1/status",
                     headers={"Authorization": f"Bearer {TOKEN_ALPHA}"}).json()
        assert body["model_requests"]["pending"] == 1


# --------------------------------------------------------------------------
# 통합 가이드 서빙 (/v1/integration)
# --------------------------------------------------------------------------
def test_integration_doc_is_served_as_markdown(store, mroles, mservices):
    with _client(store, mroles, mservices, fake_backend()) as c:
        r = c.get("/v1/integration",
                  headers={"Authorization": f"Bearer {TOKEN_ALPHA}"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/markdown")
        body = r.text
        assert "소비 프로젝트 통합 가이드" in body
        assert "/v1/generate" in body          # 실제 계약이 담겨 있다


def test_integration_doc_requires_auth(store, mroles, mservices):
    """공개 경로(/llm/v1/*)에 놓이므로 스캐너에게 내부 구조를 주지 않는다."""
    with _client(store, mroles, mservices, fake_backend()) as c:
        assert c.get("/v1/integration").status_code == 401


def test_integration_doc_available_to_non_admin_consumers(store, mroles, mservices):
    """소비자(beta)도 읽을 수 있어야 한다 — 이걸 보라고 만든 것이다."""
    with _client(store, mroles, mservices, fake_backend()) as c:
        assert c.get("/v1/integration",
                     headers={"Authorization": f"Bearer {TOKEN_BETA}"}).status_code == 200


def test_missing_doc_returns_helpful_404(store, mroles, mservices, monkeypatch, tmp_path):
    from app import main as main_mod

    monkeypatch.setattr(main_mod, "DOCS_DIR", tmp_path / "nope")
    with _client(store, mroles, mservices, fake_backend()) as c:
        r = c.get("/v1/integration", headers={"Authorization": f"Bearer {TOKEN_ALPHA}"})
        assert r.status_code == 404
        assert "이미지를 다시 빌드" in r.json()["detail"]


# --------------------------------------------------------------------------
# 임베딩이 잡 큐를 타지 않는다는 회귀 테스트
# --------------------------------------------------------------------------
async def test_embed_does_not_wait_behind_a_long_generation(store, mservices):
    """이게 깨지면 임베딩을 큐 밖에 둔 이유가 사라진다.

    batch 레인에서 3초짜리 생성이 도는 동안 임베딩이 즉시 끝나야 한다.
    """
    import asyncio
    import time

    from starlette.testclient import TestClient

    from app.config import RoleConfig
    from app.main import build_app

    roles_raw = {
        "backend": {"base_url": "http://mac.test:11434"},
        "mem_budget_gb": 40,
        "model_sizes_gb": {"slow": 30, "bge-m3": 1.2},
        "roles": {
            "heavy": {"model": "slow", "lane": "batch", "timeout": 60},
            "vec": {"model": "bge-m3", "kind": "embed", "timeout": 60},
        },
    }
    mroles = RoleConfig.from_dict(roles_raw)
    fake = FakeOllama(models=["slow", "bge-m3"], delays={"slow": 3.0})
    sched = Scheduler(store, mroles, fake, poll_interval=0.01, auto_install=False)

    with TestClient(build_app(roles=mroles, services=mservices, store=store,
                              client=fake, scheduler=sched)) as c:
        h = {"Authorization": f"Bearer {TOKEN_ALPHA}"}
        # 긴 생성을 배치 레인에 넣고(기다리지 않는다) 실행에 들어가게 한다
        c.post("/v1/generate", headers=h,
               json={"role": "heavy", "prompt": "x", "wait": 0})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not sched.running_snapshot():
            await asyncio.sleep(0.02)
        assert sched.running_snapshot(), "긴 잡이 실행 중이어야 테스트가 의미 있다"

        t0 = time.monotonic()
        r = c.post("/v1/embed", headers=h, json={"role": "vec", "input": "빠르게"})
        elapsed = time.monotonic() - t0

    assert r.status_code == 200 and r.json()["count"] == 1
    assert elapsed < 1.0, f"임베딩이 생성 뒤에서 기다렸다 ({elapsed:.1f}s)"
