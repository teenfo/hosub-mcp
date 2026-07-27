"""고점수 뉴스·공시 종목을 트레이딩 감시목록으로 올린다 — **수집전용으로만**.

왜 수집전용인가. 뉴스 점수는 아직 검증되지 않았다. Shadow 라벨링 2주가 끝나기
전이고, 점수 × 익일 수익률 이벤트 스터디도 돌리지 않았다. 검증되지 않은 점수를
믿고 매매하는 것이 정확히 우리가 발굴 3규칙에서 겪은 일이다(하락장 데이터만
보고 유효하다고 판단했다가 국면 분리에서 베타로 판명).

그래서 지금 하는 일은 하나뿐이다: **데이터를 먼저 쌓는다.** 편입된 종목은
분봉이 수집되고, 2주 뒤 뉴스 점수와 실제 수익률의 관계를 측정할 수 있게 된다.
매매 tier 승격은 그 측정을 통과한 뒤 사람이 결정한다.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from . import db, settings

log = logging.getLogger("tnm.promote")
KST = ZoneInfo("Asia/Seoul")

DEFAULT_MIN_SCORE = 70
DEFAULT_HOURS = 24
DEFAULT_MAX_PER_RUN = 5
# 방향이 부정인 뉴스는 편입하지 않는다 — 악재 종목을 감시목록에 넣을 이유가 없다.
ALLOWED_DIRECTIONS = ("긍정", "positive", "up", "중립", "neutral")


def eligible(rows: list[dict], min_score: int, max_n: int) -> list[dict]:
    """편입 후보 선별 — 순수 함수. 점수 내림차순, 종목당 1건."""
    seen: set[str] = set()
    out: list[dict] = []
    for r in sorted(rows, key=lambda x: -int(x.get("score") or 0)):
        if int(r.get("score") or 0) < min_score:
            continue
        direction = str(r.get("impact_direction") or "").strip()
        if direction and direction not in ALLOWED_DIRECTIONS:
            continue
        ticker = str(r.get("ticker") or "").strip()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        out.append(r)
        if len(out) >= max_n:
            break
    return out


class Promoter:
    """주기적으로 고점수 종목을 트레이딩 감시목록(수집전용)으로 올린다."""

    def __init__(self) -> None:
        self.running = False
        self.last_run: str = ""
        self.last_error: str = ""
        self.last_result: dict = {}

    def status(self) -> dict:
        return {"running": self.running, "last_run": self.last_run,
                "last_error": self.last_error, "last_result": self.last_result,
                "enabled": bool(settings.PROMOTE.get("enabled", False))}

    async def run_once(self) -> dict:
        cfg = settings.PROMOTE
        if not cfg.get("enabled", False):
            return {"skipped": "비활성(promote.enabled=false)"}
        if self.running:
            return {"skipped": "이미 실행 중"}
        if not db.ready:
            return {"skipped": "DB 미준비"}
        if not settings.TRADING_TOKEN:
            return {"skipped": "TRADING_TOKEN 없음"}
        self.running = True
        try:
            rows = await db.high_score_recent(
                min_score=int(cfg.get("min_score", DEFAULT_MIN_SCORE)),
                hours=int(cfg.get("window_hours", DEFAULT_HOURS)))
            picks = eligible(rows, int(cfg.get("min_score", DEFAULT_MIN_SCORE)),
                             int(cfg.get("max_per_run", DEFAULT_MAX_PER_RUN)))
            result = {"candidates": len(rows), "picked": len(picks),
                      "added": 0, "already": 0, "errors": 0, "tickers": []}
            if picks:
                result |= await self._push(picks)
            self.last_run = datetime.now(KST).isoformat(timespec="seconds")
            self.last_error = ""
            self.last_result = result
            if result["added"]:
                log.info("뉴스 편입: %s", result)
            return result
        except Exception as e:  # noqa: BLE001 — 편입 실패가 파이프라인을 막지 않는다
            self.last_error = str(e)
            log.warning("뉴스 편입 실패: %s", e)
            return {"errors": 1, "error": str(e)}
        finally:
            self.running = False

    async def _push(self, picks: list[dict]) -> dict:
        """트레이딩 감시목록에 추가하고 즉시 수집전용으로 내린다.

        추가와 tier 지정이 두 호출로 나뉘므로, tier 지정이 실패하면 매매 대상으로
        남는다. 그건 허용할 수 없다 — 실패 시 되돌린다.
        """
        out = {"added": 0, "already": 0, "errors": 0, "tickers": []}
        headers = {"X-Internal-Token": settings.TRADING_TOKEN}
        base = settings.TRADING_URL
        async with httpx.AsyncClient(timeout=15) as client:
            try:
                r = await client.get(f"{base}/api/watchlist", headers=headers)
                r.raise_for_status()
                existing = {str(e.get("code")) for e in r.json().get("entries", [])}
            except Exception as e:  # noqa: BLE001 — 목록을 모르면 중복 편입 위험
                raise RuntimeError(f"감시목록 조회 실패: {e}") from e
            for p in picks:
                ticker = p["ticker"]
                if ticker in existing:
                    out["already"] += 1
                    continue
                try:
                    r = await client.post(f"{base}/api/watchlist", headers=headers,
                                          json={"code": ticker, "name": p.get("name") or ticker})
                    r.raise_for_status()
                    # 검증 전 신호이므로 매매에서 제외한다 — 데이터만 쌓는다
                    r = await client.post(f"{base}/api/watchlist/mode", headers=headers,
                                          json={"code": ticker, "collect_only": True})
                    r.raise_for_status()
                except Exception as e:  # noqa: BLE001 — 종목 단위 격리
                    out["errors"] += 1
                    log.warning("편입 실패 %s: %s", ticker, e)
                    await self._rollback(client, headers, base, ticker)
                    continue
                out["added"] += 1
                out["tickers"].append(ticker)
        return out

    @staticmethod
    async def _rollback(client, headers, base, ticker) -> None:
        """수집전용 지정에 실패한 종목은 감시목록에서 뺀다 — 매매 대상으로 남기지 않는다."""
        try:
            await client.post(f"{base}/api/watchlist/remove", headers=headers,
                              json={"code": ticker})
        except Exception:  # noqa: BLE001
            log.exception("편입 되돌리기 실패 %s — 수동 확인 필요", ticker)

    async def loop(self) -> None:
        import asyncio

        interval = int(settings.PROMOTE.get("interval_min", 60)) * 60
        await asyncio.sleep(60)
        while True:
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001
                log.exception("뉴스 편입 루프 오류")
            await asyncio.sleep(interval)


promoter = Promoter()
