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
    """Ollama 다운 → 행을 버리지 않고 배치 백오프, 복구 시 재처리 (비기능 9장)."""
    w = EmbedWorker()
    pending = [{"id": 1, "title": "t1", "norm_body": "b", "attempts": 2},
               {"id": 2, "title": "t2", "norm_body": "b", "attempts": 0}]
    retries, saved = [], []

    async def fake_pending(limit=8):
        return pending

    async def fake_retry(ids, delay):
        retries.append((ids, delay))

    async def fake_save(item_id, vec):
        saved.append(item_id)

    monkeypatch.setattr(db, "pending_embeds", fake_pending)
    monkeypatch.setattr(db, "mark_embed_retry", fake_retry)
    monkeypatch.setattr(db, "save_embedding", fake_save)

    async def down(text):
        raise ollama.OllamaUnavailable("연결 불가")

    monkeypatch.setattr(workers_mod.ollama, "embed", down)
    assert asyncio.run(w.run_batch()) == 0
    assert retries == [([1, 2], backoff_sec(2))]     # 최대 attempts 기준 백오프
    assert saved == [] and "연결 불가" in w.last_error

    # 복구 시나리오: embed 성공 → 저장·에러 해제
    async def up(text):
        return [0.1] * 1024

    monkeypatch.setattr(workers_mod.ollama, "embed", up)
    assert asyncio.run(w.run_batch()) == 2
    assert saved == [1, 2] and w.last_error == ""


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


def test_ollama_embed_fallback_and_unavailable(monkeypatch):
    """primary 실패 → fallback 사용, 둘 다 실패 → OllamaUnavailable."""
    monkeypatch.setattr(settings, "OLLAMA_URL", "http://primary:11434")
    monkeypatch.setattr(settings, "OLLAMA_FALLBACK_URL", "http://fallback:11434")

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"embedding": [0.5] * 1024}

    class FakeClient:
        async def post(self, url, **k):
            if "primary" in url:
                import httpx
                raise httpx.ConnectError("refused")
            return FakeResp()

    monkeypatch.setattr(ollama, "_client", FakeClient())
    vec = asyncio.run(ollama.embed("텍스트"))
    assert len(vec) == 1024

    class DeadClient:
        async def post(self, url, **k):
            import httpx
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(ollama, "_client", DeadClient())
    try:
        asyncio.run(ollama.embed("텍스트"))
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
