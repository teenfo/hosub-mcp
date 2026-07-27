"""모델 A/B 비교 — 공정성 프로토콜과 실사용 잡 보호.

측정 자체보다 중요한 것이 둘 있다:
1. **공정성** — 양쪽 동일 옵션·동일 system, 각 측 워밍업 후 측정.
   콜드 32b 대 웜 7b 를 비교하면 숫자가 아무 의미도 없다.
2. **실사용 잡을 밀지 않는다** — 우선순위 -1 + 동시 1건.
"""

from __future__ import annotations

import asyncio

import pytest
from starlette.testclient import TestClient

from app.main import build_app
from app.scheduler import Scheduler
from app.store import SUCCEEDED
from tests.conftest import TOKEN_ALPHA, TOKEN_BETA, FakeOllama

H = {"Authorization": f"Bearer {TOKEN_ALPHA}"}
H_BETA = {"Authorization": f"Bearer {TOKEN_BETA}"}


@pytest.fixture
def fake():
    f = FakeOllama()
    # small 이 mid 보다 2배 빠르고, mid 는 콜드 로드가 있다
    f.eval_ns = {"small": 250_000_000, "mid": 500_000_000}
    f.load_ns = {"mid": 3_000_000_000}
    return f


@pytest.fixture
def app(store, roles, services, fake):
    sched = Scheduler(store, roles, fake, poll_interval=0.01)
    return build_app(roles=roles, services=services, store=store,
                     client=fake, scheduler=sched)


async def wait_done(c, run_id, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        d = c.get(f"/v1/admin/compare/{run_id}", headers=H).json()
        if d["done"]:
            return d
        await asyncio.sleep(0.02)
    return c.get(f"/v1/admin/compare/{run_id}", headers=H).json()


# --- 권한 ---
def test_compare_is_admin_only(app):
    with TestClient(app) as c:
        assert c.post("/v1/admin/compare", headers=H_BETA,
                      json={"prompt": "x", "models": ["small", "mid"]}
                      ).status_code == 403
        assert c.get("/v1/admin/compare", headers=H_BETA).status_code == 403


def test_generate_model_field_is_admin_only(app):
    """소비자가 역할을 우회해 임의 모델을 고르면 역할=모델 정책 계약이 무너진다."""
    with TestClient(app) as c:
        r = c.post("/v1/generate", headers=H_BETA,
                   json={"role": "fast", "prompt": "x", "model": "big"})
        assert r.status_code == 403
        ok = c.post("/v1/generate", headers=H,
                    json={"role": "fast", "prompt": "x", "model": "mid", "wait": 5})
        assert ok.status_code == 200
        assert ok.json()["model"] == "mid"       # 역할의 small 이 아니라 지정한 모델


def test_generate_model_field_validates_name(app):
    with TestClient(app) as c:
        r = c.post("/v1/generate", headers=H,
                   json={"role": "fast", "prompt": "x", "model": "http://evil/x"})
        assert r.status_code == 400 and r.json()["error"] == "invalid_model"


# --- 입력 검증 ---
def test_compare_rejects_bad_input(app):
    with TestClient(app) as c:
        bad = [
            {"prompt": "", "models": ["small", "mid"]},
            {"prompt": "x", "models": ["small"]},
            {"prompt": "x"},
        ]
        for body in bad:
            assert c.post("/v1/admin/compare", headers=H,
                          json=body).status_code == 400
        same = c.post("/v1/admin/compare", headers=H,
                      json={"prompt": "x", "models": ["small", "small"]})
        assert same.status_code == 400


def test_compare_requires_installed_models(app):
    with TestClient(app) as c:
        r = c.post("/v1/admin/compare", headers=H,
                   json={"prompt": "x", "models": ["small", "nothere"]})
        assert r.status_code == 409
        assert r.json()["models"] == ["nothere"]


# --- 실행 ---
async def test_compare_runs_warmup_then_measure_for_each_side(app, store, fake):
    with TestClient(app) as c:
        r = c.post("/v1/admin/compare", headers=H,
                   json={"prompt": "같은 질문", "models": ["small", "mid"]})
        assert r.status_code == 202
        run_id = r.json()["run"]["id"]
        d = await wait_done(c, run_id)

    assert d["done"]
    # 각 측 2회 = 4회 호출, 순서는 a(small) 먼저
    assert fake.calls == ["small", "small", "mid", "mid"]
    # 워밍업은 1토큰만 뽑는다 — 로드 시간을 측정에서 빼기 위한 것
    assert fake.options_seen[0]["num_predict"] == 1
    assert "num_predict" not in fake.options_seen[1]
    # 21GB 두 개를 10분 붙잡아 두지 않는다
    assert set(fake.keep_alives) == {"30s"}


async def test_compare_reports_tokens_per_sec_and_cold_label(app):
    with TestClient(app) as c:
        r = c.post("/v1/admin/compare", headers=H,
                   json={"prompt": "q", "models": ["small", "mid"]})
        d = await wait_done(c, r.json()["run"]["id"])

    a, b = d["sides"]["a"]["metrics"], d["sides"]["b"]["metrics"]
    # eval_count 10 / eval_duration 0.25s = 40 tok/s vs 0.5s = 20 tok/s
    assert a["tokens_per_sec"] == 40.0
    assert b["tokens_per_sec"] == 20.0
    # 로드 시간은 tok/s 에 섞이지 않고 따로 라벨된다
    assert a["cold"] is False and b["cold"] is True
    assert b["load_duration_ms"] == 3000


async def test_compare_uses_identical_options_on_both_sides(app, fake):
    with TestClient(app) as c:
        r = c.post("/v1/admin/compare", headers=H,
                   json={"prompt": "q", "models": ["small", "mid"],
                         "options": {"top_p": 0.5}})
        await wait_done(c, r.json()["run"]["id"])
    measured = [fake.options_seen[1], fake.options_seen[3]]
    assert measured[0] == measured[1]
    # 결정적이어야 하고, 각 역할의 옵션(fast 는 temperature 0.3)을 상속하지 않는다
    assert measured[0] == {"temperature": 0, "seed": 0, "top_p": 0.5}


async def test_compare_jobs_run_behind_real_work(app, store, fake):
    """우선순위 -1 — TNM 분류 같은 실사용 잡이 A/B 뒤에서 기다리면 안 된다."""
    fake.delay = 0.05
    with TestClient(app) as c:
        c.post("/v1/admin/compare", headers=H,
               json={"prompt": "q", "models": ["small", "mid"]})
        # 같은 batch 레인에 나중에 들어왔는데도 앞에 아무도 없다
        real = c.post("/v1/generate", headers=H,
                      json={"role": "bulk", "prompt": "실사용", "wait": 0}).json()
        assert real["lane"] == "batch"
        assert real["queue_position"] == 0
        for _ in range(300):
            if store.get_job(real["job_id"])["status"] == SUCCEEDED:
                break
            await asyncio.sleep(0.01)
    assert store.get_job(real["job_id"])["status"] == SUCCEEDED


async def test_only_one_comparison_at_a_time(app, fake):
    fake.delay = 0.5
    with TestClient(app) as c:
        first = c.post("/v1/admin/compare", headers=H,
                       json={"prompt": "q", "models": ["small", "mid"]})
        assert first.status_code == 202
        second = c.post("/v1/admin/compare", headers=H,
                        json={"prompt": "q2", "models": ["small", "big"]})
    assert second.status_code == 409
    assert second.json()["run_id"] == first.json()["run"]["id"]


async def test_finished_run_frees_the_slot(app):
    with TestClient(app) as c:
        first = c.post("/v1/admin/compare", headers=H,
                       json={"prompt": "q", "models": ["small", "mid"]})
        await wait_done(c, first.json()["run"]["id"])
        again = c.post("/v1/admin/compare", headers=H,
                       json={"prompt": "q2", "models": ["small", "big"]})
    assert again.status_code == 202


async def test_compare_history_is_listed(app):
    with TestClient(app) as c:
        r = c.post("/v1/admin/compare", headers=H,
                   json={"prompt": "q", "models": ["small", "mid"]})
        await wait_done(c, r.json()["run"]["id"])
        runs = c.get("/v1/admin/compare", headers=H).json()["runs"]
    assert len(runs) == 1
    assert runs[0]["run"]["model_a"] == "small"
    assert runs[0]["sides"]["b"]["model"] == "mid"


def test_compare_unknown_run_404(app):
    with TestClient(app) as c:
        assert c.get("/v1/admin/compare/nope", headers=H).status_code == 404
