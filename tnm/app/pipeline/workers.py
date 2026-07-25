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

from .. import db, ollama, settings
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

    async def run_batch(self, limit: int = 8) -> int:
        """한 배치 처리. 반환: 임베딩 성공 수."""
        items = await db.pending_embeds(limit)
        if not items:
            return 0
        done = 0
        for it in items:
            text = ((it.get("title") or "") + "\n" + (it.get("norm_body") or ""))[:2000]
            try:
                vec = await ollama.embed(text)
            except ollama.OllamaUnavailable as e:
                # 배치 전체 보류 — 같은 원인(연결 불가)이므로 개별 재시도 무의미
                delay = backoff_sec(max(x.get("attempts", 0) for x in items))
                await db.mark_embed_retry([x["id"] for x in items], delay)
                self.last_error = str(e)
                log.warning("임베딩 보류(%ds 후 재시도): %s", delay, e)
                return done
            await db.save_embedding(it["id"], vec)
            done += 1
        self.processed += done
        if self.last_error:
            self.last_error = ""
            log.info("Ollama 복구 — 임베딩 재개")
        return done

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
