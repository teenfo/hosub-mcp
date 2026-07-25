"""수집 러너 — 어댑터(공시·뉴스)를 종목별로 돌려 raw_items 에 적재한다.

- dart: 장중(평일 09:00-16:00 KST) 10분 / 장외 60분 주기
- news(구글 RSS 기본 + 네이버 선택): 30분 주기
- 어댑터·종목 단위로 예외 격리 — 한 종목 실패가 사이클을 죽이지 않는다.
- 커서는 성공한 종목만 전진 (실패 시 다음 사이클에 같은 구간 재시도 — 멱등 적재)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from . import db, settings
from .collectors.base import Collector, RawDoc
from .collectors.dart import DartCollector
from .collectors.naver import NaverNewsCollector, daily_budget
from .collectors.rss import GoogleNewsCollector
from .pipeline.normalize import content_hash, normalize

log = logging.getLogger("tnm.collect")
KST = ZoneInfo("Asia/Seoul")


def is_market_hours(now: datetime) -> bool:
    return now.weekday() < 5 and "09:00" <= now.strftime("%H:%M") <= "16:00"


def doc_to_row(doc: RawDoc) -> dict:
    norm = normalize((doc.title or "") + " " + (doc.body or ""))
    return {
        "source_uid": doc.source_uid, "title": doc.title, "body": doc.body,
        "url": doc.url, "published_at": doc.published_at,
        "content_hash": content_hash(doc.title, doc.body), "norm_body": norm,
    }


class CollectRunner:
    def __init__(self) -> None:
        self.collectors: dict[str, list[Collector]] = {
            "dart": [DartCollector()],
            "news": [GoogleNewsCollector(), NaverNewsCollector()],
        }
        self.running: set[str] = set()
        self.last_run: dict[str, str] = {}
        self.last_result: dict[str, dict] = {}

    def status(self) -> dict:
        return {"running": sorted(self.running), "last_run": self.last_run,
                "last_result": self.last_result}

    async def run_once(self, kind: str) -> dict:
        if kind in self.running:
            return {"skipped": "이미 실행 중"}
        if not db.ready:
            return {"skipped": "DB 미준비"}
        self.running.add(kind)
        try:
            stocks = await db.active_watch_for_collect()
            counts = {"stocks": 0, "fetched": 0, "inserted": 0, "errors": 0}
            for col in self.collectors.get(kind, []):
                for stock in stocks:
                    if not col.enabled(stock):
                        continue
                    cursors = stock.get("last_collected_at") or {}
                    try:
                        docs, new_cursor = await col.fetch(stock, cursors.get(col.name))
                    except Exception as e:  # noqa: BLE001 — 종목 단위 격리
                        counts["errors"] += 1
                        log.warning("%s 수집 실패 %s(%s): %s",
                                    col.name, stock["name"], stock["ticker"], e)
                        continue
                    counts["stocks"] += 1
                    if docs:
                        rows = [doc_to_row(d) for d in docs]
                        n = await db.insert_raw_items(stock["id"], col.name, rows)
                        counts["fetched"] += len(docs)
                        counts["inserted"] += n
                    if new_cursor and new_cursor != cursors.get(col.name):
                        await db.set_cursor(stock["ticker"], col.name, new_cursor)
            self.last_run[kind] = datetime.now(KST).isoformat(timespec="seconds")
            self.last_result[kind] = counts
            if counts["fetched"] or counts["errors"]:
                log.info("%s 수집: %s", kind, counts)
            return counts
        finally:
            self.running.discard(kind)

    async def dart_loop(self) -> None:
        cfg = settings.COLLECT.get("dart", {})
        await asyncio.sleep(10)
        while True:
            try:
                await self.run_once("dart")
            except Exception:  # noqa: BLE001
                log.exception("dart 수집 루프 오류")
            interval = (cfg.get("market_interval_min", 10)
                        if is_market_hours(datetime.now(KST))
                        else cfg.get("off_interval_min", 60))
            await asyncio.sleep(int(interval) * 60)

    async def news_loop(self) -> None:
        cfg = settings.COLLECT.get("news", {})
        interval_min = int(cfg.get("interval_min", 30))
        # 네이버 API 일일 예산 사전 점검 로그 (비기능 9장)
        if settings.NAVER_CLIENT_ID and settings.NAVER_CLIENT_SECRET:
            try:
                n = len(await db.active_watch_for_collect()) if db.ready else 0
            except Exception:  # noqa: BLE001
                n = 0
            budget = daily_budget(n or 60, interval_min)
            log.info("네이버 API 일일 예산: %d콜 (한도 25,000)%s", budget,
                     " — 초과 위험!" if budget > 25_000 else "")
        await asyncio.sleep(15)
        while True:
            try:
                await self.run_once("news")
            except Exception:  # noqa: BLE001
                log.exception("news 수집 루프 오류")
            await asyncio.sleep(interval_min * 60)
