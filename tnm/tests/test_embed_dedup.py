"""M4 완료판정 — 임베딩 워커(Ollama 다운 보류·백오프·복구)·신규성 판정·첫 수집 창."""
import asyncio
from datetime import datetime, timedelta, timezone

from app import db, ollama, settings
from app.collectors.rss import filter_new
from app.pipeline import workers as workers_mod
from app.pipeline.dedup import judge
from app.pipeline.workers import DedupWorker, EmbedWorker, backoff_sec

CFG = {"duplicate_threshold": 0.92, "follow_up_threshold": 0.85, "window_days": 7}


def test_judge_thresholds():
    assert judge(0.93, CFG) == "duplicate"
    assert judge(0.92, CFG) == "duplicate"     # 경계 포함 (요청서: 0.92 이상)
    assert judge(0.88, CFG) == "follow_up"
    assert judge(0.85, CFG) == "follow_up"
    assert judge(0.5, CFG) == "new"
    assert judge(None, CFG) == "new"           # 비교 대상 없음 = 신규


def test_judge_config_injection():
    assert judge(0.5, {"duplicate_threshold": 0.4}) == "duplicate"


def test_backoff_caps_at_10min():
    assert backoff_sec(0) == 10
    assert backoff_sec(3) == 80
    assert backoff_sec(99) == 600


def test_embed_worker_holds_batch_on_unavailable(monkeypatch):
    """게이트웨이 다운 → 행을 버리지 않고 배치 백오프, 복구 시 재처리 (비기능 9장)."""
    w = EmbedWorker()
    pending = [{"id": 1, "title": "t1", "norm_body": "b", "attempts": 2},
               {"id": 2, "title": "t2", "norm_body": "b", "attempts": 0}]
    retries, saved = [], []

    async def fake_pending(limit=16):
        return pending

    async def fake_retry(ids, delay):
        retries.append((ids, delay))

    async def fake_save(item_id, vec):
        saved.append(item_id)

    monkeypatch.setattr(db, "pending_embeds", fake_pending)
    monkeypatch.setattr(db, "mark_embed_retry", fake_retry)
    monkeypatch.setattr(db, "save_embedding", fake_save)

    async def down(texts):
        raise ollama.OllamaUnavailable("연결 불가")

    monkeypatch.setattr(workers_mod.ollama, "embed_batch", down)
    assert asyncio.run(w.run_batch()) == 0
    assert retries == [([1, 2], backoff_sec(2))]     # 최대 attempts 기준 백오프
    assert saved == [] and "연결 불가" in w.last_error

    # 복구 시나리오: 배치 1회 호출로 전체 저장·에러 해제
    calls = []

    async def up(texts):
        calls.append(len(texts))
        return [[0.1] * 1024 for _ in texts]

    monkeypatch.setattr(workers_mod.ollama, "embed_batch", up)
    assert asyncio.run(w.run_batch()) == 2
    assert saved == [1, 2] and w.last_error == ""
    assert calls == [2]                              # 배치 전체 1회 호출


def test_dedup_worker_routes_duplicates(monkeypatch):
    """재탕 기사 세트: 0.95→duplicate 즉시 종결, 0.88→follow_up, 0.3→new."""
    w = DedupWorker()
    sims = {11: 0.95, 12: 0.88, 13: 0.3}
    novelties, dups = [], []

    async def fake_pending(limit=50):
        return [11, 12, 13]

    async def fake_sim(item_id, window):
        assert window == 7
        return sims[item_id]

    async def fake_set(item_id, novelty, sim):
        novelties.append((item_id, novelty))

    async def fake_dup(item_id, sim):
        dups.append(item_id)

    monkeypatch.setattr(db, "pending_dedup", fake_pending)
    monkeypatch.setattr(db, "max_similarity", fake_sim)
    monkeypatch.setattr(db, "set_novelty", fake_set)
    monkeypatch.setattr(db, "insert_skipped_duplicate", fake_dup)
    monkeypatch.setattr(settings, "DEDUP", CFG)
    assert asyncio.run(w.run_batch()) == 3
    assert novelties == [(11, "duplicate"), (12, "follow_up"), (13, "new")]
    assert dups == [11] and w.duplicates == 1        # duplicate 만 LLM 생략 종결


def test_embed_batch_via_gateway(monkeypatch):
    """임베딩은 게이트웨이 /v1/embed 배치 — 실패 시 OllamaUnavailable."""
    from app.llmgw import GatewayError

    class FakeGW:
        async def embed(self, texts, *, role="embed"):
            assert role == "embed"
            return [[0.5] * 1024 for _ in texts]

    monkeypatch.setattr(ollama, "_gw", FakeGW())
    vecs = asyncio.run(ollama.embed_batch(["a", "b"]))
    assert len(vecs) == 2 and len(vecs[0]) == 1024
    assert len(asyncio.run(ollama.embed("한 건"))) == 1024

    class DownGW:
        async def embed(self, texts, *, role="embed"):
            raise GatewayError("연결 실패")

    monkeypatch.setattr(ollama, "_gw", DownGW())
    try:
        asyncio.run(ollama.embed_batch(["a"]))
        raised = False
    except ollama.OllamaUnavailable:
        raised = True
    assert raised

    class BadGW:
        async def embed(self, texts, *, role="embed"):
            return [[0.1]]                            # 개수 불일치

    monkeypatch.setattr(ollama, "_gw", BadGW())
    try:
        asyncio.run(ollama.embed_batch(["a", "b"]))
        raised = False
    except ollama.OllamaUnavailable:
        raised = True
    assert raised


def test_rss_initial_window_limits_first_run():
    """첫 실행(커서 없음)은 최근 N일만 — 과거 백로그 방지."""
    now = datetime.now(timezone.utc)
    items = [
        {"link": "a", "published": now - timedelta(days=1)},
        {"link": "b", "published": now - timedelta(days=30)},   # 오래된 기사
    ]
    fresh, cursor = filter_new(items, None, initial_days=7)
    assert [i["link"] for i in fresh] == ["a"]
    # 커서는 창과 무관하게 피드 최신 발행시각 — 다음 주기 중복 방지
    assert cursor == (now - timedelta(days=1)).isoformat()
    # 커서가 있으면 창 제한 미적용 (기존 동작 유지)
    fresh2, _ = filter_new(items, (now - timedelta(days=40)).isoformat(),
                           initial_days=7)
    assert len(fresh2) == 2
