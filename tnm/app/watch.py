"""관심종목 동기화 잡 — 트레이딩 서비스 API 를 소비한다 (키움 직접 호출 없음).

- GET {TRADING_URL}/api/watchlist  → 감시목록 전체 (매매 대상 + 수집 전용)
- GET {TRADING_URL}/api/account    → 보유 종목 (trading 이 kt00018 을 30초 캐시로 노출)
병합 규칙은 db.apply_sync 참조 (manual 불변·excluded 불활성). 30분 주기 + 수동 트리거.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from . import corp_codes, db, settings

log = logging.getLogger("tnm.watch")
KST = ZoneInfo("Asia/Seoul")


def parse_trading_watchlist(payload: dict) -> dict[str, tuple[str, str]]:
    """/api/watchlist 응답 → {ticker: (name, tier)}. 형식 방어적으로 파싱.

    tier 는 트레이딩의 collect_only 를 그대로 따른다 — 매매하지 않는 종목까지
    뉴스를 30분마다 볼 이유가 없다(구글 RSS 는 종목당 1콜).
    """
    out: dict[str, tuple[str, str]] = {}
    for e in payload.get("entries", []) or []:
        code = str(e.get("code", "")).strip()
        if code:
            tier = "collect" if e.get("collect_only") else "trade"
            out[code] = (str(e.get("name") or code), tier)
    return out


def parse_holdings(payload: dict) -> dict[str, str]:
    """/api/account 응답에서 보유 종목 {ticker: name} 추출.

    trading 의 계좌 응답(kt00018 파싱 결과)은 보유 목록 키가 변할 수 있어
    알려진 후보 키를 순서대로 시도한다. 종목코드 필드도 마찬가지.
    """
    out: dict[str, str] = {}
    holdings = None
    for key in ("stocks", "positions", "holdings", "items"):
        v = payload.get(key)
        if isinstance(v, list) and v:
            holdings = v
            break
    for h in holdings or []:
        if not isinstance(h, dict):
            continue
        code = ""
        for ck in ("code", "symbol", "stk_cd"):
            if h.get(ck):
                code = str(h[ck]).strip().lstrip("A")   # 키움은 'A005930' 형식 가능
                break
        if not code:
            continue
        name = ""
        for nk in ("name", "stk_nm"):
            if h.get(nk):
                name = str(h[nk])
                break
        out[code] = name or code
    return out


def merge_auto(trading: dict[str, tuple[str, str]], holdings: dict[str, str],
               ) -> dict[str, tuple[str, str, str]]:
    """자동 동기 대상 병합: ticker -> (name, origin, tier). 보유가 감시목록보다 우선."""
    auto: dict[str, tuple[str, str, str]] = {}
    for code, v in trading.items():
        name, tier = v if isinstance(v, tuple) else (v, "trade")
        auto[code] = (name, "trading", tier)
    for code, name in holdings.items():
        # 보유 종목은 origin='holding' 으로 우선 — 돈이 들어가 있으니 항상 매매 tier
        auto[code] = (name, "holding", "trade")
    return auto


class WatchSync:
    def __init__(self) -> None:
        self.running = False
        self.last_run: str = ""
        self.last_error: str = ""
        self.last_result: dict = {}

    def status(self) -> dict:
        return {"running": self.running, "last_run": self.last_run,
                "last_error": self.last_error, "last_result": self.last_result}

    async def run_once(self) -> dict:
        if self.running:
            return {"skipped": "이미 실행 중"}
        if not db.ready:
            return {"skipped": "DB 미준비"}
        self.running = True
        try:
            trading_wl: dict[str, str] = {}
            holdings: dict[str, str] = {}
            trading_ok = holdings_ok = False   # 소스별 성공 여부 — 실패한 소스는 소멸 판정 제외
            headers = {"X-Internal-Token": settings.TRADING_TOKEN}
            async with httpx.AsyncClient(timeout=10) as client:
                try:
                    r = await client.get(f"{settings.TRADING_URL}/api/watchlist",
                                         headers=headers)
                    r.raise_for_status()
                    trading_wl = parse_trading_watchlist(r.json())
                    trading_ok = True
                except Exception as e:  # noqa: BLE001 — trading 다운 시 기존 목록 유지
                    log.warning("트레이딩 감시목록 조회 실패(기존 유지): %s", e)
                try:
                    r = await client.get(f"{settings.TRADING_URL}/api/account",
                                         headers=headers)
                    r.raise_for_status()
                    payload = r.json()
                    # trading 은 키움 장애 시 HTTP 200 + {"ok": false} 를 반환한다
                    if payload.get("ok") is False:
                        raise RuntimeError(payload.get("error") or "계좌 조회 실패")
                    holdings = parse_holdings(payload)
                    holdings_ok = True
                except Exception as e:  # noqa: BLE001
                    log.warning("보유계좌 조회 실패(기존 유지): %s", e)
            if not trading_ok and not holdings_ok:
                self.last_error = "trading 응답 없음 — 목록 변경 없이 유지"
                return {"skipped": self.last_error}
            ok_origins = (({"trading"} if trading_ok else set())
                          | ({"holding"} if holdings_ok else set()))
            result = await db.apply_sync(merge_auto(trading_wl, holdings),
                                         ok_origins=ok_origins)
            # corp_code 미보유 행 채우기 (DART 키 있을 때만)
            try:
                mapping = await corp_codes.mapping_for_missing()
                if mapping:
                    result["corp_codes_filled"] = await db.fill_corp_codes(mapping)
            except Exception as e:  # noqa: BLE001 — corp_code 는 부가 작업
                log.warning("corp_code 채우기 실패: %s", e)
            self.last_run = datetime.now(KST).isoformat(timespec="seconds")
            self.last_error = ""
            self.last_result = result
            log.info("관심종목 동기화: %s", result)
            return result
        finally:
            self.running = False

    async def loop(self) -> None:
        interval = int(settings.WATCH.get("sync_interval_min", 30)) * 60
        await asyncio.sleep(5)   # 기동 직후 DB init 여유
        while True:
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 — 루프는 죽지 않는다
                log.exception("관심종목 동기화 오류")
            await asyncio.sleep(interval)
