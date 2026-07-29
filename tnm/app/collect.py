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

from . import brokers as broker_reg
from . import corp_codes, db, discover, ingest, settings
from .collectors import research
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
FOREIGN_CURSOR_KEY = "research_foreign_seen"   # 해외사별 마지막 기사 시각(ISO)
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
            if kind == "research":
                return await self._run_research()
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

    # ---------------- 증권사 리서치 리포트 ----------------

    def _report_row(self, r: research.Report) -> dict:
        return {"source": r.source, "source_uid": r.source_uid, "broker": r.broker,
                "category": r.category, "ticker": r.ticker,
                "stock_name": r.stock_name, "title": r.title, "summary": r.summary,
                "url": r.url, "pdf_url": r.pdf_url, "analyst": r.analyst,
                "target_price": r.target_price, "opinion": r.opinion,
                "published_at": r.published_at}

    def _report_to_raw(self, r: dict) -> dict:
        """리포트(DB 행) → raw_items 행. 목표가·투자의견을 본문에 넣는다.

        분류 LLM 이 보는 것은 본문 텍스트뿐이라, 별도 칼럼에만 있는 목표가는
        분류·점수에 반영되지 않는다. 리포트에서 가장 중요한 정보를 문장으로
        옮겨 넣어야 파이프라인이 그것을 읽는다.
        """
        title = r["title"]
        body = ingest.doc_body(f"[{r['broker']} 리포트] {title}", [
            r.get("summary"),
            f"목표주가 {r['target_price']:,}원" if r.get("target_price") else None,
            f"투자의견 {r['opinion']}" if r.get("opinion") else None,
            f"작성 {r['analyst']}" if r.get("analyst") else None,
        ])
        return {"source_uid": f"{r['source']}:{r['source_uid']}", "title": title,
                "body": body, "url": r["url"], "published_at": r["published_at"],
                "content_hash": content_hash(title, body),
                "norm_body": normalize(title + " " + body)}

    async def _ingest_reports(self, cfg: dict) -> dict:
        """적재 대기 리포트를 분석 파이프라인에 태운다 (DART·뉴스와 같은 경로).

        수집과 분리한 이유: 종목 등록 상한에 걸려 이번에 못 들어간 리포트가
        다음 사이클에 다시 시도돼야 한다. 그러려면 '대기' 가 수집 결과가
        아니라 **DB 상태**여야 한다(raw_item_id is null).
        """
        icfg = cfg.get("ingest") or {}
        rows = await db.pending_report_ingest(
            days=int(icfg.get("days", 7)), limit=int(icfg.get("batch", 200)),
            max_attempts=int(icfg.get("max_attempts", 3)))
        if not rows:
            return {}
        # 시도 횟수를 **먼저** 올린다. 적재가 영영 안 되는 건(중복·제외 종목)이
        # 매 사이클 같은 일을 반복하지 않도록 상한에서 빠져나가게 해야 한다.
        await db.bump_report_attempts([r["id"] for r in rows])
        out = await ingest.attach_docs(
            [{"ticker": r["ticker"], "name": r.get("stock_name") or r["ticker"],
              "row": self._report_to_raw(r)} for r in rows],
            source="research", origin="research", cfg=icfg)
        if out.get("inserted"):
            out["linked"] = await db.link_report_items()
        return {f"ingest_{k}": v for k, v in out.items()}

    async def _collect_foreign(self, brokers: list[dict], cfg: dict,
                               counts: dict) -> list[research.Report]:
        """해외 증권사 인용 기사. 증권사당 RSS 1콜, 커서는 전역 state 에 모아 둔다."""
        targets = [b for b in brokers if b.get("kind") == "foreign"]
        if not targets or not cfg.get("foreign_enabled", True):
            return []
        import json

        raw = await db.get_state(FOREIGN_CURSOR_KEY)
        try:
            cursors = json.loads(raw) if raw else {}
        except ValueError:
            cursors = {}
        # 국내 상장사명 — 종목 질의 결과가 국내 얘기인지 가르는 기준.
        # 없으면(캐시 미생성) 시장 용어만으로 좁게 판정한다.
        try:
            names = await corp_codes.ensure_names()
        except Exception as e:  # noqa: BLE001 — 캐시 실패가 수집을 막지 않는다
            log.warning("상장사명 조회 실패: %s", e)
            names = set()
        if not names:
            log.info("상장사명 캐시 없음 — 해외 인용은 시장 용어만으로 거른다")
        out: list[research.Report] = []
        for b in targets[:int(cfg.get("foreign_max_per_cycle", 12))]:
            prev = cursors.get(b["name"])
            since = None
            if prev:
                try:
                    since = datetime.fromisoformat(prev)
                except ValueError:
                    since = None
            try:
                rows = await research.fetch_foreign(
                    b, since, initial_days=int(cfg.get("initial_days", 3)),
                    names=names)
            except Exception as e:  # noqa: BLE001 — 증권사 단위 격리
                counts["errors"] += 1
                log.warning("해외 리서치 수집 실패 %s: %s", b["name"], e)
                continue
            if rows:
                cursors[b["name"]] = max(r.published_at for r in rows).isoformat()
                out.extend(rows)
        if cursors:
            await db.set_state(FOREIGN_CURSOR_KEY, json.dumps(cursors))
        return out

    async def _run_research(self) -> dict:
        """증권사 리포트 수집 — 국내는 원문 목록, 해외는 국내 언론 인용.

        수집 여부는 tnm_brokers.enabled 가 정한다. **끈 증권사의 리포트는 적재
        하지 않고**, 목록에 없는 증권사는 적재하되 '미등록'으로 세어 화면에
        보여준다 — 이름을 놓쳤다는 사실 자체가 목록을 고칠 근거다.
        """
        cfg = settings.COLLECT.get("research", {}) or {}
        counts = {"fetched": 0, "kept": 0, "inserted": 0,
                  "disabled_skip": 0, "unregistered": 0, "errors": 0}
        if not cfg.get("enabled", True):
            return counts | {"skipped": "비활성"}
        try:
            counts["seeded"] = await db.seed_brokers(broker_reg.seed_rows())
        except Exception as e:  # noqa: BLE001 — 시드 실패가 수집을 막지 않는다
            counts["errors"] += 1
            log.warning("증권사 시드 실패: %s", e)
        brokers = await db.list_brokers()
        index = broker_reg.build_index(brokers)

        found: list[research.Report] = []
        if cfg.get("naver_enabled", True):
            try:
                found += await research.fetch_naver(pages=int(cfg.get("pages", 1)))
            except Exception as e:  # noqa: BLE001 — 소스 단위 격리
                counts["errors"] += 1
                log.warning("네이버 리서치 수집 실패: %s", e)
        if cfg.get("consensus_enabled", True):
            try:
                found += await research.fetch_consensus(
                    days=int(cfg.get("days", 3)))
            except Exception as e:  # noqa: BLE001
                counts["errors"] += 1
                log.warning("한경컨센서스 수집 실패: %s", e)
        found += await self._collect_foreign(brokers, cfg, counts)
        counts["fetched"] = len(found)

        keep: list[research.Report] = []
        for r in found:
            b = broker_reg.match(r.broker, index)
            if b is None:
                counts["unregistered"] += 1
                keep.append(r)
                continue
            if not b.get("enabled"):
                counts["disabled_skip"] += 1
                continue
            # 표기를 등록된 정식명으로 통일한다 — 그래야 증권사별 집계가 갈리지
            # 않는다('유안타 리서치'와 '유안타증권'이 따로 세어지는 것을 막는다).
            r.broker = b["name"]
            keep.append(r)
        # 목록 페이지는 매 사이클 같은 30건을 다시 준다. 여기서 신규만 남기지
        # 않으면 상세 조회(건당 1콜)와 증권사 누적 건수가 매시간 헛돈다.
        try:
            known = await db.known_report_keys([(r.source, r.source_uid) for r in keep])
        except Exception as e:  # noqa: BLE001 — 조회 실패면 멱등 INSERT 에 맡긴다
            log.warning("리서치 기존 건 조회 실패: %s", e)
            known = set()
        keep = [r for r in keep if (r.source, r.source_uid) not in known]
        counts["kept"] = len(keep)
        if keep:
            try:
                await research.fill_naver_targets(
                    keep, limit=int(cfg.get("detail_max_per_cycle", 20)))
            except Exception as e:  # noqa: BLE001 — 보조 정보라 실패해도 계속
                log.warning("리서치 상세(목표가) 조회 실패: %s", e)
            counts["inserted"] = await db.insert_reports(
                [self._report_row(r) for r in keep])

        # 신규가 0건이어도 적재는 돌린다 — 지난 사이클에 상한으로 밀린 리포트가
        # 여기서 풀린다. 수집과 적재를 붙여 두면 그 대기열이 영영 안 빠진다.
        try:
            counts |= await self._ingest_reports(cfg)
        except Exception as e:  # noqa: BLE001 — 적재 실패가 수집을 되돌리지 않는다
            counts["errors"] += 1
            log.warning("리서치 분석 적재 실패: %s", e)

        if not keep:
            self.last_run["research"] = datetime.now(KST).isoformat(timespec="seconds")
            self.last_result["research"] = counts
            return counts

        tally: dict[str, int] = {}
        for r in keep:
            tally[r.broker] = tally.get(r.broker, 0) + 1
        try:
            await db.bump_brokers(tally, datetime.now(KST))
        except Exception as e:  # noqa: BLE001 — 통계라 실패해도 수집은 성립
            log.warning("증권사 통계 갱신 실패: %s", e)

        self.last_run["research"] = datetime.now(KST).isoformat(timespec="seconds")
        self.last_result["research"] = counts
        log.info("리서치 수집: %s", counts)
        return counts

    async def research_loop(self) -> None:
        cfg = settings.COLLECT.get("research", {}) or {}
        interval_min = int(cfg.get("interval_min", 60))
        await asyncio.sleep(20)
        while True:
            try:
                await self.run_once("research")
            except Exception:  # noqa: BLE001
                log.exception("research 수집 루프 오류")
            await asyncio.sleep(interval_min * 60)

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
