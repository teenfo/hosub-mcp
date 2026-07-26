"""M5 완료판정 — LLM 분류: 스키마 검증·재시도·llm_failed 보존·폴백·보류·스코어 결정론."""
import asyncio

from app import db, ollama, settings
from app.ollama import SchemaError
from app.pipeline import workers as workers_mod
from app.pipeline.classify import (build_user_msg, check_hallucination,
                                   extract_json, validate)
from app.pipeline.score import compute
from app.pipeline.workers import ClassifyWorker

GOOD = {"category": "공급계약", "is_material": True, "impact_direction": "positive",
        "impact_horizon": "short", "confidence": 0.75,
        "reason": "북미 변압기 3,000억원 수주 공시", "summary": "요약 두 문장.",
        "contains_numbers": True}


# ---------- 스키마 검증 ----------

def test_validate_good_payload():
    out = validate(dict(GOOD))
    assert out["category"] == "공급계약" and out["confidence"] == 0.75


def test_validate_rejects_bad_enum_and_missing():
    for broken in [
        {**GOOD, "category": "매수추천"},          # enum 위반
        {**GOOD, "impact_direction": "buy"},
        {**GOOD, "confidence": 1.5},                # 범위 위반
        {**GOOD, "is_material": "yes"},             # 타입 위반
        {k: v for k, v in GOOD.items() if k != "reason"},  # 필수 키 누락
    ]:
        try:
            validate(broken)
            raised = False
        except SchemaError:
            raised = True
        assert raised, broken


def test_validate_coerces_confidence_string():
    assert validate({**GOOD, "confidence": "0.6"})["confidence"] == 0.6


def test_extract_json_lenient():
    import json
    fenced = "```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```"
    assert extract_json(fenced)["category"] == "공급계약"
    chatty = "다음은 결과입니다: " + json.dumps(GOOD, ensure_ascii=False) + " 감사합니다"
    assert extract_json(chatty)["is_material"] is True
    try:
        extract_json("JSON 없음")
        raised = False
    except SchemaError:
        raised = True
    assert raised


def test_build_user_msg_truncates_body():
    msg = build_user_msg("효성중공업", "dart", "제목", "2026-07-26", "가" * 10000, 6000)
    assert msg.count("가") == 6000 and "공시(DART)" in msg


# ---------- 환각 검사 ----------

def test_hallucination_flag():
    src = "회사가 3,000억원 규모 계약을 체결했다"
    assert check_hallucination("5000억원 수주", src) is True      # 원문에 없는 수치
    assert check_hallucination("3,000억원 수주", src) is False    # 원문 수치 인용
    assert check_hallucination("대규모 수주", src) is False       # 수치 없음


# ---------- 스코어 (요청서 6장 — 결정론) ----------

def test_score_deterministic_and_formula():
    for _ in range(100):
        s, d = compute("dart", GOOD, "new", settings.SCORE)
        assert s == 75                       # 100×1.0×1.0×1.0×0.75
    assert d["w_source"] == 1.0 and d["w_category"] == 1.0


def test_score_factors():
    s, _ = compute("rss", GOOD, "new", settings.SCORE)          # 0.7 소스
    assert s == round(100 * 0.7 * 1.0 * 1.0 * 0.75) == 52
    s, _ = compute("dart", GOOD, "follow_up", settings.SCORE)   # novelty 0.5
    assert s == 38                                              # round(37.5)
    s, _ = compute("dart", GOOD, "duplicate", settings.SCORE)
    assert s == 0
    s, d = compute("dart", {**GOOD, "is_material": False}, "new", settings.SCORE)
    assert s == 38 and d["non_material_factor"] == 0.5          # 감점
    s, _ = compute("dart", {**GOOD, "category": "시황해설"}, "new", settings.SCORE)
    assert s == 30                                              # W 0.4


# ---------- 분류 워커 ----------

def _item():
    return {"id": 9, "source": "dart", "title": "공급계약 체결", "body": None,
            "norm_body": "효성중공업 공시 — 3,000억원 계약", "published_at": "2026-07-26",
            "novelty": "new", "stock_name": "효성중공업"}


def _wire_db(monkeypatch, store):
    async def fake_pending(limit=2):
        return store["pending"]

    async def fake_log(*a):
        store["calls"].append(a)

    async def fake_insert(raw_id, a, novelty, score, detail, model, latency,
                          retries, input_hash, warn):
        store["analyses"].append({"id": raw_id, "score": score, "retries": retries,
                                  "model": model, "warn": warn, "novelty": novelty})

    async def fake_failed(raw_id, novelty, input_hash, retries, model):
        store["failed"].append({"id": raw_id, "retries": retries})

    monkeypatch.setattr(db, "pending_classify", fake_pending)
    monkeypatch.setattr(db, "log_llm_call", fake_log)
    monkeypatch.setattr(db, "insert_analysis", fake_insert)
    monkeypatch.setattr(db, "insert_llm_failed", fake_failed)


def test_classify_worker_success(monkeypatch):
    import json
    w = ClassifyWorker()
    store = {"pending": [_item()], "calls": [], "analyses": [], "failed": []}
    _wire_db(monkeypatch, store)

    async def good_chat(system, user):
        return json.dumps(GOOD, ensure_ascii=False), "qwen2.5:32b", 4200

    monkeypatch.setattr(workers_mod.ollama, "chat", good_chat)
    assert asyncio.run(w.run_batch()) == 1
    a = store["analyses"][0]
    assert a["score"] == 75 and a["retries"] == 0 and a["warn"] is False
    assert len(store["calls"]) == 1 and store["calls"][0][5] is True  # ok 로그


def test_classify_worker_retry_then_llm_failed(monkeypatch):
    """스키마 위반 3연속 → llm_failed 적재(원문 보존), 호출 로그 3건."""
    w = ClassifyWorker()
    store = {"pending": [_item()], "calls": [], "analyses": [], "failed": []}
    _wire_db(monkeypatch, store)

    async def bad_chat(system, user):
        return "분류 불가", "qwen2.5:32b", 1000        # JSON 아님 → SchemaError

    monkeypatch.setattr(workers_mod.ollama, "chat", bad_chat)
    assert asyncio.run(w.run_batch()) == 1
    assert store["analyses"] == []
    assert store["failed"] == [{"id": 9, "retries": 2}]
    assert len(store["calls"]) == 3 and all(c[5] is False for c in store["calls"])
    assert w.failed == 1


def test_classify_worker_holds_on_unavailable(monkeypatch):
    """Ollama 다운 → analyses/llm_failed 미생성(보류), unavailable 플래그."""
    w = ClassifyWorker()
    store = {"pending": [_item(), {**_item(), "id": 10}],
             "calls": [], "analyses": [], "failed": []}
    _wire_db(monkeypatch, store)

    async def down(system, user):
        raise ollama.OllamaUnavailable("연결 불가")

    monkeypatch.setattr(workers_mod.ollama, "chat", down)
    assert asyncio.run(w.run_batch()) == 0
    assert store["analyses"] == [] and store["failed"] == []
    assert w.unavailable is True


def test_chat_fallback_records_model(monkeypatch):
    """primary 연결 실패 → fallback(14b) 사용, model_name 에 기록 (요청서 5.5)."""
    monkeypatch.setattr(settings, "OLLAMA_URL", "http://primary:11434")
    monkeypatch.setattr(settings, "OLLAMA_FALLBACK_URL", "http://fb:11434")

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": "{}"}}

    class FakeClient:
        async def post(self, url, **k):
            if "primary" in url:
                import httpx
                raise httpx.ConnectError("refused")
            return FakeResp()

    monkeypatch.setattr(ollama, "_client", FakeClient())
    content, model, latency = asyncio.run(ollama.chat("s", "u"))
    assert model == settings.LLM.get("fallback_model", "qwen2.5:14b")