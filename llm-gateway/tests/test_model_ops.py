"""모델 운영 — 삭제 차단·삭제 후처리·카탈로그·직접 설치.

가장 중요한 회귀는 **삭제 후 Slack 스팸**이다. model_requests 행을 ready 로 남기면
_triage_missing 이 ready→pending 으로 되살리고 알림을 쏜다. 지운 직후 루프를
여러 틱 돌려 조용한지 확인한다.
"""

from __future__ import annotations

import asyncio

import pytest
from starlette.testclient import TestClient

from app.catalog import Catalog
from app.config import RoleConfig
from app.main import build_app
from app.scheduler import Scheduler
from app.store import MR_APPROVED, MR_PULLING, MR_READY, QUEUED
from tests.conftest import TOKEN_ALPHA, TOKEN_BETA, FakeOllama

H = {"Authorization": f"Bearer {TOKEN_ALPHA}"}
H_BETA = {"Authorization": f"Bearer {TOKEN_BETA}"}

# fast/tiny=small, chat/bulk=mid, heavy=big, vec=bge-m3 (conftest)
# 'spare' 는 아무 역할도 안 쓰는 모델 → 삭제 가능
ROLES_RAW = {
    "backend": {"base_url": "http://mac.test:11434"},
    "mem_budget_gb": 40,
    "model_sizes_gb": {"small": 5, "mid": 10, "huge": 99},
    "roles": {
        "fast": {"model": "small", "lane": "interactive", "timeout": 30},
        "vec":  {"model": "bge-m3", "kind": "embed", "timeout": 60},
    },
}


@pytest.fixture
def oroles():
    return RoleConfig.from_dict(ROLES_RAW)


class RecordingNotifier:
    """SlackNotifier 대역 — 무엇이 언제 나갔는지 센다."""

    def __init__(self):
        self.sent: list[tuple] = []
        self._last: dict[str, str] = {}

    def _changed(self, key, state):
        if self._last.get(key) == state:
            return False
        self._last[key] = state
        return True

    def forget(self, key):
        self._last.pop(key, None)

    async def model_approval_needed(self, req):
        if self._changed(f"model:{req['model']}", "pending"):
            self.sent.append(("approval", req["model"]))

    async def model_install_finished(self, model, *, ok, error=None):
        if self._changed(f"model:{model}", "ready" if ok else "failed"):
            self.sent.append(("install", model, ok))

    async def model_deleted(self, model, *, freed_gb=None, actor=None):
        self.sent.append(("deleted", model))

    async def backend_offline(self, base_url, error):
        pass

    async def backend_online(self, base_url):
        pass


@pytest.fixture
def ctx(store, oroles, services):
    fake = FakeOllama(models=["small", "mid", "spare", "bge-m3"])
    fake.model_disk_gb = {"spare": 3.0, "small": 4.0}
    notifier = RecordingNotifier()
    sched = Scheduler(store, oroles, fake, poll_interval=0.01,
                      models_refresh=0.02, notifier=notifier)
    app = build_app(roles=oroles, services=services, store=store,
                    client=fake, scheduler=sched)
    return {"app": app, "fake": fake, "sched": sched, "store": store,
            "roles": oroles, "notifier": notifier}


@pytest.fixture
def client(ctx):
    with TestClient(ctx["app"]) as c:
        yield c


# --- 삭제 차단 ---
def test_delete_blocked_by_role_using_model(client):
    r = client.request("DELETE", "/v1/admin/models?model=small", headers=H)
    assert r.status_code == 409
    kinds = [b["kind"] for b in r.json()["blockers"]]
    assert "roles" in kinds


def test_delete_blocked_by_loose_tag_match(ctx):
    """역할이 'small', 맥에는 'small:latest' — 느슨하게 안 잡으면 역할이 깨진다."""
    ctx["fake"]._models = ["small:latest", "spare"]
    with TestClient(ctx["app"]) as c:
        r = c.request("DELETE", "/v1/admin/models?model=small:latest", headers=H)
    assert r.status_code == 409
    assert r.json()["blockers"][0]["detail"] == ["fast"]


def test_embed_role_flagged_separately(ctx):
    """임베딩 모델은 지우면 소비자가 pending 이 아니라 즉시 503 을 받는다."""
    with TestClient(ctx["app"]) as c:
        r = c.request("DELETE", "/v1/admin/models?model=bge-m3", headers=H)
    assert r.status_code == 409
    assert r.json()["blockers"][0]["embed_roles"] == ["vec"]


def test_delete_blocked_by_queued_and_running_jobs(ctx):
    # 레인 동시성이 1이라 첫 잡은 running, 둘째는 queued 로 남는다
    ctx["fake"].delay = 5.0
    for _ in range(2):
        ctx["store"].create_job(service="alpha", role="fast", model="spare",
                                lane="batch", prompt="p", system=None)
    with TestClient(ctx["app"]) as c:
        r = c.request("DELETE", "/v1/admin/models?model=spare", headers=H)
    assert r.status_code == 409
    # 실행 시작 타이밍에 따라 running 은 있을 수도 없을 수도 있다 —
    # 대기 잡이 잡히는 것이 이 테스트의 요지다
    kinds = {b["kind"] for b in r.json()["blockers"]}
    assert "queued_jobs" in kinds and kinds <= {"queued_jobs", "running_jobs"}


def test_delete_blocked_while_installing(ctx):
    ctx["store"].ensure_model_request("spare", requested_by="alpha", roles=[],
                                      est_size_gb=3)
    ctx["store"].set_model_request_status("spare", MR_PULLING)
    with TestClient(ctx["app"]) as c:
        r = c.request("DELETE", "/v1/admin/models?model=spare", headers=H)
    assert r.status_code == 409
    assert [b["kind"] for b in r.json()["blockers"]] == ["installing"]


def test_non_admin_cannot_delete_or_install(client):
    assert client.request("DELETE", "/v1/admin/models?model=spare",
                          headers=H_BETA).status_code == 403
    assert client.post("/v1/admin/models/install", headers=H_BETA,
                       json={"model": "x"}).status_code == 403
    assert client.get("/v1/admin/catalog", headers=H_BETA).status_code == 403


# --- 삭제 성공 + 후처리 ---
def test_delete_removes_request_row_and_updates_availability(ctx):
    store, sched = ctx["store"], ctx["sched"]
    store.ensure_model_request("spare", requested_by="alpha", roles=[], est_size_gb=3)
    store.set_model_request_status("spare", MR_READY)
    with TestClient(ctx["app"]) as c:
        r = c.request("DELETE", "/v1/admin/models?model=spare", headers=H)
        assert r.status_code == 200, r.text
        assert r.json()["existed"] is True
        assert "spare" in ctx["fake"].deleted
        # 행을 ready 로 남기거나 rejected 로 바꾸지 않는다 — 아예 없앤다
        assert store.get_model_request("spare") is None
        # /v1/status 가 최대 30초 거짓말하지 않도록 즉시 반영
        assert "spare" not in (sched.available_models or [])
    assert ("deleted", "spare") in ctx["notifier"].sent


async def test_no_slack_spam_after_delete(ctx):
    """지운 뒤 모델 루프를 여러 틱 돌려도 재요청·알림이 없어야 한다."""
    store, sched, notifier = ctx["store"], ctx["sched"], ctx["notifier"]
    with TestClient(ctx["app"]) as c:
        c.request("DELETE", "/v1/admin/models?model=spare", headers=H)
        notifier.sent.clear()
        await asyncio.sleep(0.3)          # models_refresh=0.02 → 여러 틱
    assert notifier.sent == []
    assert store.get_model_request("spare") is None


async def test_delete_then_reinstall_notifies_again(ctx):
    """notifier.forget 을 안 하면 재설치 때 승인 알림이 억제된다."""
    store, notifier = ctx["store"], ctx["notifier"]
    store.ensure_model_request("spare", requested_by="alpha", roles=[], est_size_gb=3)
    store.set_model_request_status("spare", MR_READY)
    with TestClient(ctx["app"]) as c:
        # 한 번 pending 알림을 보낸 상태를 만든다
        await notifier.model_approval_needed({"model": "spare"})
        assert len(notifier.sent) == 1
        c.request("DELETE", "/v1/admin/models?model=spare", headers=H)
        notifier.sent.clear()
        await notifier.model_approval_needed({"model": "spare"})
    assert notifier.sent == [("approval", "spare")]


def test_backend_delete_failure_is_reported(ctx):
    ctx["fake"].delete_fail = True
    with TestClient(ctx["app"]) as c:
        r = c.request("DELETE", "/v1/admin/models?model=spare", headers=H)
    assert r.status_code == 502
    assert ctx["store"].list_audit()[0]["outcome"] == "failed"


# --- 설치 ---
def test_install_goes_through_existing_approval_pipeline(ctx):
    """새 pull 코드를 만들지 않는다 — approved 로 넣으면 기존 puller 가 집어간다."""
    with TestClient(ctx["app"]) as c:
        r = c.post("/v1/admin/models/install", headers=H, json={"model": "mid2:7b"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "approved"
    req = ctx["store"].get_model_request("mid2:7b")
    assert req["status"] in (MR_APPROVED, MR_PULLING, MR_READY)


async def test_installed_model_actually_gets_pulled(ctx):
    with TestClient(ctx["app"]) as c:
        c.post("/v1/admin/models/install", headers=H, json={"model": "mid2:7b"})
        for _ in range(200):
            if "mid2:7b" in ctx["fake"].pulled:
                break
            await asyncio.sleep(0.01)
    assert "mid2:7b" in ctx["fake"].pulled


def test_install_rejects_oversized_model_upfront(client):
    """_pick 이 사후에 잡을 몰살하는 대신 설치 전에 명확히 거부한다."""
    r = client.post("/v1/admin/models/install", headers=H, json={"model": "huge"})
    assert r.status_code == 400
    assert r.json()["error"] == "too_large"
    # 크기를 모르는 모델도 20GB 로 가정되지만 예산(40) 안이라 통과해야 한다
    assert client.post("/v1/admin/models/install", headers=H,
                       json={"model": "mystery"}).status_code == 200


def test_install_rejects_dangerous_names(client):
    for bad in ("http://evil/x", "../../etc/passwd", "a b", ""):
        r = client.post("/v1/admin/models/install", headers=H, json={"model": bad})
        assert r.status_code == 400, bad
        assert r.json()["error"] == "invalid_model"


def test_install_already_installed_is_noop(client):
    r = client.post("/v1/admin/models/install", headers=H, json={"model": "spare"})
    assert r.json()["status"] == "already_installed"


def test_install_conflicts_while_pulling(ctx):
    ctx["store"].ensure_model_request("newone", requested_by="alpha", roles=[],
                                      est_size_gb=3)
    ctx["store"].set_model_request_status("newone", MR_PULLING)
    with TestClient(ctx["app"]) as c:
        assert c.post("/v1/admin/models/install", headers=H,
                      json={"model": "newone"}).status_code == 409


# --- 목록 · 카탈로그 ---
def test_list_models_reports_size_roles_and_blockers(client):
    d = client.get("/v1/admin/models", headers=H).json()
    names = [m["name"] for m in d["models"]]
    assert set(names) >= {"small", "spare", "bge-m3"}
    small = next(m for m in d["models"] if m["name"] == "small")
    assert small["roles"] == ["fast"] and small["blockers"]
    spare = next(m for m in d["models"] if m["name"] == "spare")
    assert spare["blockers"] == []
    assert d["models"] == sorted(d["models"], key=lambda m: m["size_gb"] or 0,
                                 reverse=True)


def test_usage_by_model(store):
    store.record_usage(service="a", role="fast", model="small",
                       eval_count=10, duration_ms=5, status="succeeded")
    store.record_usage(service="b", role="fast", model="small",
                       eval_count=2, duration_ms=1, status="failed")
    rows = store.usage_by_model()
    assert rows[0]["model"] == "small"
    assert rows[0]["calls"] == 2 and rows[0]["ok"] == 1


def test_catalog_search_and_sizes(tmp_path):
    p = tmp_path / "catalog.yaml"
    p.write_text("""
models:
  - name: qwen2.5
    family: qwen
    kinds: [generate]
    description: 범용
    tags:
      - { tag: "qwen2.5:7b", size_gb: 5, context: 32768 }
  - name: bge-m3
    kinds: [embed]
    tags:
      - { tag: "bge-m3", size_gb: 1.2 }
""", encoding="utf-8")
    cat = Catalog(p)
    assert len(cat.models()) == 2
    assert cat.sizes() == {"qwen2.5:7b": 5.0, "bge-m3": 1.2}
    assert [m["name"] for m in cat.search("qwen")] == ["qwen2.5"]
    assert [m["name"] for m in cat.search(kind="embed")] == ["bge-m3"]


def test_broken_catalog_keeps_last_good_value(tmp_path):
    """카탈로그가 깨져도 /v1/status·설치 화면이 죽으면 안 된다."""
    p = tmp_path / "catalog.yaml"
    p.write_text("models:\n  - name: a\n    tags: [{tag: 'a:1', size_gb: 1}]\n",
                 encoding="utf-8")
    cat = Catalog(p)
    assert len(cat.models()) == 1
    import os
    import time
    p.write_text("models: [ this is: [broken\n", encoding="utf-8")
    os.utime(p, (time.time() + 1, time.time() + 1))
    assert len(cat.models()) == 1          # 직전 정상본 유지
    assert cat.error


def test_missing_catalog_is_not_an_error(tmp_path):
    cat = Catalog(tmp_path / "nope.yaml")
    assert cat.models() == [] and cat.sizes() == {} and cat.error is None


def test_catalog_endpoint_lists_installed(client):
    d = client.get("/v1/admin/catalog?q=qwen", headers=H).json()
    assert all("qwen" in m["name"] for m in d["models"])
    assert "spare" in d["installed"]
    assert d["mem_budget_gb"] == 40


def test_catalog_size_used_for_budget_gate(ctx):
    """카탈로그 크기가 예산 가드에 실제로 반영되는가(70b=42GB > 40)."""
    with TestClient(ctx["app"]) as c:
        r = c.post("/v1/admin/models/install", headers=H,
                   json={"model": "llama3.1:70b"})
    assert r.status_code == 400 and r.json()["error"] == "too_large"


def test_queued_job_models_ignores_finished(store):
    j = store.create_job(service="a", role="fast", model="small", lane="batch",
                         prompt="p", system=None)
    assert store.queued_job_models() == {"small": 1}
    store.finish(j["id"], status="succeeded", response="x")
    assert store.queued_job_models() == {}
