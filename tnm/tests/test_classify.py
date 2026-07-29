"""M5 완료판정 — LLM 분류: 스키마 검증·재시도·llm_failed 보존·폴백·보류·스코어 결정론."""
import asyncio

import pytest

from app import db, ollama, settings
from app.ollama import SchemaError
from app.pipeline import classify
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


class _FakeJob:
    def __init__(self, status="ok", response="{}", model="qwen2.5:7b",
                 job_id="j1", error=None):
        self.status = status
        self.response = response
        self.model = model
        self.job_id = job_id
        self.error = error

    @property
    def ok(self):
        return self.status == "ok"

    @property
    def pending(self):
        return self.status == "pending"


def test_chat_via_gateway_ok(monkeypatch):
    """분류는 게이트웨이 role(classify_news) 경유 — 응답·모델명 반환."""
    calls = {}

    class FakeGW:
        async def generate(self, role, prompt, *, system=None, wait=30,
                           priority=0, metadata=None):
            calls.update(role=role, system=system, wait=wait)
            return _FakeJob(response='{"a":1}', model="qwen2.5:7b")

    monkeypatch.setattr(ollama, "_gw", FakeGW())
    content, model, latency = asyncio.run(ollama.chat("SYS", "USER"))
    assert content == '{"a":1}' and model == "qwen2.5:7b"
    assert calls["role"] == "classify_news" and calls["system"] == "SYS"


def test_chat_gateway_pending_then_polled(monkeypatch):
    class FakeGW:
        async def generate(self, *a, **k):
            return _FakeJob(status="pending", response=None, job_id="j9")

        async def wait_for(self, job_id, *, timeout, poll):
            assert job_id == "j9"
            return _FakeJob(response='{"done":true}')

    monkeypatch.setattr(ollama, "_gw", FakeGW())
    content, _, _ = asyncio.run(ollama.chat("s", "u"))
    assert content == '{"done":true}'


def test_chat_gateway_failure_holds(monkeypatch):
    """게이트웨이 연결 실패·잡 실패 → OllamaUnavailable (보류 계약 유지)."""
    from app.llmgw import GatewayError

    class DownGW:
        async def generate(self, *a, **k):
            raise GatewayError("연결 실패")

    monkeypatch.setattr(ollama, "_gw", DownGW())
    try:
        asyncio.run(ollama.chat("s", "u"))
        raised = False
    except ollama.OllamaUnavailable:
        raised = True
    assert raised

    class FailedGW:
        async def generate(self, *a, **k):
            return _FakeJob(status="failed", response=None, error="backend 죽음")

    monkeypatch.setattr(ollama, "_gw", FailedGW())
    try:
        asyncio.run(ollama.chat("s", "u"))
        raised = False
    except ollama.OllamaUnavailable as e:
        raised = "backend 죽음" in str(e)
    assert raised


def test_chat_requires_token(monkeypatch):
    monkeypatch.setattr(ollama, "_gw", None)
    monkeypatch.setattr(settings, "LLMGW_TOKEN", "")
    try:
        asyncio.run(ollama.chat("s", "u"))
        raised = False
    except ollama.OllamaUnavailable:
        raised = True
    assert raised

# --- 스키마 흡수 (2026-07-29 실측 실패 원인) ---
#
# 7일간 분류 실패 원인 상위: impact_horizon 'unclear' 247건, category 위반
# 175건(기술개발·규제·신제품 등), impact_direction 'mixed' 29건.
# 프롬프트에 "그대로 쓰라"고 적어도 모델이 계속 벗어나므로 결정론적으로 흡수한다.

def _payload(**over):
    base = {"category": "실적", "is_material": True, "impact_direction": "positive",
            "impact_horizon": "short", "confidence": 0.8,
            "reason": "근거", "summary": "요약", "contains_numbers": False}
    return base | over


def test_horizon_unclear_is_accepted():
    """direction 에는 '모르겠다'가 있는데 horizon 에는 없어 항목이 버려졌다.

    게다가 impact_horizon 은 1년치 소급 측정에서 예측력이 확인되지 않은 필드다 —
    예측력 없는 칸의 enum 이 좁아서 분석 자체를 버리는 것은 손해만 있다.
    """
    assert classify.validate(_payload(impact_horizon="unclear"))["impact_horizon"] == "unclear"


@pytest.mark.parametrize("raw,expect", [
    ("규제", "소송규제"),
    ("매매거래정지", "거래정지"),
    ("기재정정", "정정공시"),
    ("수주", "공급계약"),
    ("유상증자", "자금조달"),
])
def test_category_synonyms_map_to_canonical(raw, expect):
    assert classify.validate(_payload(category=raw))["category"] == expect


@pytest.mark.parametrize("raw", ["기술개발", "연구개발", "신제품출시", "투자", "ESG"])
def test_vague_categories_fall_to_기타_not_증설투자(raw):
    """'기술개발'을 증설투자(1.0)로 올리면 일반 기사가 공급계약과 같은 무게를 갖는다.

    과대평가는 누락보다 비싸다 — 프롬프트도 '애매하면 기타'라고 지시한다.
    """
    assert classify.validate(_payload(category=raw))["category"] == "기타"


def test_multi_value_takes_the_first():
    """모델이 '실적 | 공급계약' 처럼 둘을 내놓는다(실측 11건). 앞이 주 판단이다."""
    assert classify.validate(_payload(category="실적 | 공급계약"))["category"] == "실적"


def test_direction_mixed_becomes_unclear():
    assert classify.validate(_payload(impact_direction="mixed"))["impact_direction"] == "unclear"


def test_unknown_category_still_fails():
    """별칭에도 없는 값은 실패시킨다 — 모델 드리프트가 보이지 않으면 못 고친다."""
    with pytest.raises(SchemaError):
        classify.validate(_payload(category="완전히새로운분류"))


def test_urgent_categories_survive_coercion():
    """거래정지·정정공시는 긴급 판정의 입력이다 — 흡수 과정에서 뭉개지면 안 된다."""
    for cat in ("거래정지", "정정공시"):
        assert classify.validate(_payload(category=cat))["category"] == cat
