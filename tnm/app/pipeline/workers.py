"""임베딩·신규성 워커 — DB 를 큐로 사용 (재시작·Mac 다운 내성).

- EmbedWorker: embedding NULL 스캔 → bge-m3. OllamaUnavailable 이면 배치 전체를
  지수백오프(최대 10분) 보류하고 다음 주기로 — 수집은 계속된다 (비기능 9장).
- DedupWorker: 임베딩 완료·novelty 미판정 → 동일 종목 7일 창 최대 유사도로 판정.
  duplicate 는 analyses(skipped_duplicate) 로 즉시 종결(LLM 생략),
  new/follow_up 은 novelty 만 기록해 분류 워커(M5)에 넘긴다.
"""
from __future__ import annotations

import asyncio
import logging

import hashlib

from .. import db, ollama, settings
from . import classify, score
from .dedup import judge

log = logging.getLogger("tnm.workers")

_MAX_BACKOFF_SEC = 600


def backoff_sec(attempts: int) -> int:
    """10s → 20s → 40s … 최대 600s."""
    return min(_MAX_BACKOFF_SEC, 10 * (2 ** max(0, attempts)))


class EmbedWorker:
    def __init__(self) -> None:
        self.processed = 0
        self.last_error = ""

    def status(self) -> dict:
        return {"processed": self.processed, "last_error": self.last_error}

    async def run_batch(self, limit: int = 16) -> int:
        """한 배치 처리 — 게이트웨이 /v1/embed 로 배치 전체를 1회 호출."""
        items = await db.pending_embeds(limit)
        if not items:
            return 0
        texts = [((it.get("title") or "") + "\n" + (it.get("norm_body") or ""))[:2000]
                 for it in items]
        try:
            vecs = await ollama.embed_batch(texts)
        except ollama.OllamaUnavailable as e:
            # 배치 전체 보류 — 같은 원인(게이트웨이/백엔드 불가)이므로 개별 재시도 무의미
            delay = backoff_sec(max(x.get("attempts", 0) for x in items))
            await db.mark_embed_retry([x["id"] for x in items], delay)
            self.last_error = str(e)
            log.warning("임베딩 보류(%ds 후 재시도): %s", delay, e)
            return 0
        for it, vec in zip(items, vecs):
            await db.save_embedding(it["id"], vec)
        self.processed += len(items)
        if self.last_error:
            self.last_error = ""
            log.info("게이트웨이 복구 — 임베딩 재개")
        return len(items)

    async def loop(self) -> None:
        await asyncio.sleep(20)
        while True:
            try:
                if db.ready:
                    n = await self.run_batch()
                    if n:            # 큐가 남아있으면 쉬지 않고 계속 소화
                        continue
            except Exception:  # noqa: BLE001
                log.exception("임베딩 워커 오류")
            await asyncio.sleep(10)


class ClassifyWorker:
    """LLM 분류 (요청서 5장·FR-06) + 스코어 산출 (6장).

    - OllamaUnavailable: analyses 행을 만들지 않고 보류 — 복구 시 자동 재처리.
      루프 수준 지수백오프로 재시도 폭주를 막는다.
    - SchemaError: 재시도 2회(총 3시도) 후 llm_failed 적재 (원문 보존).
    - 모든 호출은 tnm_llm_calls 에 기록 (입력해시·모델·지연·시도·결과).
    """

    def __init__(self) -> None:
        self.processed = 0
        self.failed = 0
        self.last_error = ""
        self.unavailable = False

    def status(self) -> dict:
        return {"processed": self.processed, "failed": self.failed,
                "last_error": self.last_error, "unavailable": self.unavailable}

    async def _classify_one(self, it: dict) -> bool:
        """한 항목 분류. 반환 False = Ollama 불가(배치 중단 신호)."""
        max_chars = int(settings.LLM.get("max_body_chars", 6000))
        body = it.get("norm_body") or it.get("body") or ""
        user = classify.build_user_msg(it["stock_name"], it["source"], it["title"],
                                       it["published_at"], body, max_chars)
        input_hash = hashlib.sha256(
            (classify.SYSTEM_PROMPT + "\n" + user).encode()).hexdigest()
        result = None
        model_name, latency = "", 0
        attempts = 0
        for attempt in range(1, 4):            # 최초 1 + 재시도 2 (FR-06)
            attempts = attempt
            try:
                content, model_name, latency = await ollama.chat(
                    classify.SYSTEM_PROMPT, user)
            except ollama.OllamaUnavailable as e:
                self.last_error = str(e)
                self.unavailable = True
                return False                    # 보류 — analyses 미생성
            try:
                result = classify.validate(classify.extract_json(content))
                await db.log_llm_call(it["id"], input_hash, model_name, latency,
                                      attempt, True, None)
                break
            except ollama.SchemaError as e:
                await db.log_llm_call(it["id"], input_hash, model_name, latency,
                                      attempt, False, str(e))
                log.warning("분류 스키마 위반(시도 %d/3) item=%s: %s",
                            attempt, it["id"], e)
        self.unavailable = False
        if result is None:                      # 3회 모두 위반 → 원문 보존 적재
            await db.insert_llm_failed(it["id"], it["novelty"], input_hash,
                                       attempts - 1, model_name)
            self.failed += 1
            return True
        score_val, detail = score.compute(it["source"], result, it["novelty"],
                                          settings.SCORE)
        warn = classify.check_hallucination(
            result["reason"], (it["title"] or "") + "\n" + body)
        await db.insert_analysis(it["id"], result, it["novelty"], score_val, detail,
                                 model_name, latency, attempts - 1, input_hash, warn)
        self.processed += 1
        return True

    async def run_batch(self, limit: int = 2) -> int:
        items = await db.pending_classify(limit)
        done = 0
        for it in items:
            if not await self._classify_one(it):
                break                           # Ollama 불가 — 배치 중단
            done += 1
        return done

    async def loop(self) -> None:
        await asyncio.sleep(30)
        backoff = 10
        while True:
            try:
                if db.ready:
                    n = await self.run_batch()
                    if self.unavailable:
                        backoff = min(_MAX_BACKOFF_SEC, backoff * 2)
                    else:
                        backoff = 10
                        if n:                   # 큐 소화 계속
                            continue
            except Exception:  # noqa: BLE001
                log.exception("분류 워커 오류")
            await asyncio.sleep(backoff)


class DedupWorker:
    def __init__(self) -> None:
        self.processed = 0
        self.duplicates = 0

    def status(self) -> dict:
        return {"processed": self.processed, "duplicates": self.duplicates}

    async def run_batch(self, limit: int = 50) -> int:
        ids = await db.pending_dedup(limit)
        window = int(settings.DEDUP.get("window_days", 7))
        for item_id in ids:
            sim = await db.max_similarity(item_id, window)
            novelty = judge(sim, settings.DEDUP)
            await db.set_novelty(item_id, novelty, sim)
            if novelty == "duplicate":
                await db.insert_skipped_duplicate(item_id, sim)
                self.duplicates += 1
        self.processed += len(ids)
        return len(ids)

    async def loop(self) -> None:
        await asyncio.sleep(25)
        while True:
            try:
                if db.ready:
                    n = await self.run_batch()
                    if n:
                        continue
            except Exception:  # noqa: BLE001
                log.exception("신규성 워커 오류")
            await asyncio.sleep(10)
