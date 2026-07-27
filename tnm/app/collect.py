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

from . import db, discover, settings
from .collectors.base import Collector, RawDoc
from .collectors.dart import DartCollector
from .collectors.naver import NaverNewsCollector, daily_budget
from .collectors.rss import GoogleNewsCollector
from .pipeline.normalize import content_hash, normalize

log = logging.getLogger("tnm.collect")
KST = ZoneInfo("Asia/Seoul")


def is_market_hours(now: datetime) -> bool:
    return now.weekday() < 5 and "09:00" <= now.strftime("%H:%M") <= "16:00"


DART_CURSOR_KEY = "dart_market_rcept_no"
# 계층별 뉴스 수집 주기(분). 구글 RSS 는 종목명 검색이라 종목당 1콜이 강제돼
# 전종목이 불가능하다. 대신 매매 대상은 자주, 그 외는 드물게 본다.
DEFAULT_TIER_INTERVAL = {"trade": 30, "collect": 120, "other": 720}


def tier_interval(tier: str, cfg: dict) -> int:
    table = {**DEFAULT_TIER_INTERVAL, **(cfg.get("tier_interval_min") or {})}
    return int(table.get(tier) or table.get("other", 720))


def is_due(stock: dict, source: str, cfg: dict, now: datetime) -> bool:
    """이 종목을 지금 수집할 차례인가. 시도 이력이 없으면 항상 대상."""
    raw = (stock.get("last_attempt") or {}).get(source)
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(str(raw))
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=KST)
    gap_min = (now - last).total_seconds() / 60
    # 경계에서 한 주기를 통째로 미루지 않도록 여유를 둔다(루프 지터 흡수)
    return gap_min >= tier_interval(stock.get("tier", "trade"), cfg) - 0.5


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
            if kind == "dart" and settings.COLLECT.get("dart", {}).get("market_mode", True):
                return await self._run_dart_market()
            stocks = await db.active_watch_for_collect()
            cfg = settings.COLLECT.get(kind, {})
            now = datetime.now(KST)
            counts = {"stocks": 0, "fetched": 0, "inserted": 0, "errors": 0,
                      "skipped_not_due": 0}
            attempted: list[str] = []
            for col in self.collectors.get(kind, []):
                for stock in stocks:
                    if not col.enabled(stock):
                        continue
                    # 계층별 주기 — 매매 대상은 자주, 수집전용·기타는 드물게
                    if not is_due(stock, kind, cfg, now):
                        counts["skipped_not_due"] += 1
                        continue
                    attempted.append(stock["ticker"])
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
            await db.mark_attempt(sorted(set(attempted)), kind)
            self.last_run[kind] = datetime.now(KST).isoformat(timespec="seconds")
            self.last_result[kind] = counts
            if counts["fetched"] or counts["errors"]:
                log.info("%s 수집: %s", kind, counts)
            return counts
        finally:
            self.running.discard(kind)

    async def _run_dart_market(self) -> dict:
        """DART 전종목 모드 — 날짜 범위로 전체 공시를 받아 우리 종목만 적재한다.

        종목별 모드는 감시 65종목 = 65콜/사이클이고 종목이 늘면 그대로 늘어난다.
        전종목 모드는 목록 몇 콜로 끝나고 종목 수와 무관하다. 감시목록 밖 공시는
        종전에 세기만 하고 버렸는데(사이클당 ~1,120건), 이제 보고서명 allowlist 로
        걸러 **신규 종목을 발굴한다**(discover.py).
        """
        from .collectors import dart as dart_mod

        cfg = settings.COLLECT.get("dart", {})
        counts = {"mode": "market", "stocks": 0, "fetched": 0, "inserted": 0,
                  "errors": 0, "unmatched": 0, "more": False,
                  "candidates": 0, "discovered": 0}
        if not settings.DART_API_KEY:
            return counts | {"skipped": "DART 키 없음"}
        cursor = await db.get_state(DART_CURSOR_KEY)
        try:
            pairs, new_cursor, more = await dart_mod.fetch_market(
                cursor, initial_days=int(cfg.get("market_initial_days", 3)))
        except Exception as e:  # noqa: BLE001 — 사이클 격리
            log.warning("dart 전종목 수집 실패: %s", e)
            return counts | {"errors": 1}
        by_corp = await db.watch_by_corp_code()
        grouped: dict[int, list[dict]] = {}
        tickers: list[str] = []
        unmatched: list[tuple] = []
        for corp_code, doc, meta in pairs:
            stock = by_corp.get(corp_code)
            if not stock:
                counts["unmatched"] += 1
                unmatched.append((corp_code, doc, meta))
                continue
            grouped.setdefault(stock["id"], []).append(doc_to_row(doc))
            tickers.append(stock["ticker"])
        try:
            counts |= await self._discover(unmatched, set(by_corp))
        except Exception as e:  # noqa: BLE001 — 발굴 실패가 수집을 막지 않는다
            log.warning("dart 신규 종목 발굴 실패: %s", e)
        for wid, rows in grouped.items():
            counts["fetched"] += len(rows)
            try:
                counts["inserted"] += await db.insert_raw_items(wid, "dart", rows)
                counts["stocks"] += 1
            except Exception as e:  # noqa: BLE001 — 종목 단위 격리
                counts["errors"] += 1
                log.warning("dart 적재 실패 watchlist_id=%s: %s", wid, e)
        await db.mark_attempt(sorted(set(tickers)), "dart")
        # 커서는 적재 성공 여부와 무관하게 '받아온 만큼' 전진한다. 적재는 멱등이라
        # 다음 사이클이 같은 구간을 다시 봐도 중복이 생기지 않는다.
        if new_cursor and new_cursor != cursor:
            await db.set_state(DART_CURSOR_KEY, new_cursor)
        counts["more"] = more
        self.last_run["dart"] = datetime.now(KST).isoformat(timespec="seconds")
        self.last_result["dart"] = counts
        log.info("dart 전종목 수집: %s", counts)
        return counts

    async def _discover(self, unmatched: list[tuple], known: set[str]) -> dict:
        """매칭 안 된 공시에서 신규 종목을 발굴해 관심종목에 등록한다.

        상한 둘을 둔다. 사이클당 후보가 수십~수백 건 나올 수 있는데, 등록하는
        족족 뉴스 수집 대상이 늘어 RSS 호출이 선형으로 증가하기 때문이다.
          max_per_cycle  한 사이클에 새로 넣을 수 있는 종목 수
          max_total      dart 출처 관심종목 총량 — 넘으면 아무것도 안 넣는다
        """
        cfg = (settings.COLLECT.get("dart", {}) or {}).get("discover", {}) or {}
        if not cfg.get("enabled", True) or not unmatched:
            return {}
        cands = discover.candidates(unmatched, known, await db.known_tickers())
        # discovered 를 항상 실어 보낸다 — 어느 경로로 끝나든 호출자가 같은 키를
        # 읽을 수 있어야 "0건인가 키가 없는가" 를 헷갈리지 않는다.
        out = {"candidates": len(cands), "discovered": 0}
        if not cands:
            return out
        room = int(cfg.get("max_total", 300)) - await db.count_origin("dart")
        if room <= 0:
            log.info("dart 발굴 상한 도달 — 후보 %d건 보류", len(cands))
            return out | {"discover_capped": True}
        take = cands[:max(0, min(int(cfg.get("max_per_cycle", 10)), room))]
        added = await db.add_discovered(take, tier=str(cfg.get("tier", "other")))
        if added:
            log.info("dart 신규 종목 발굴: %d건 등록 (%s)", added,
                     ", ".join(f"{c['ticker']} {c['reason']}" for c in take[:5]))
        return out | {"discovered": added}

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
