"""DART 공시 수집기 (FR-02) — list.json 증분 수집.

커서 = 마지막으로 본 rcept_no (YYYYMMDD+일련번호라 문자열 비교로 증분 판정).
최초 수집은 최근 N일(config collect.dart.initial_days, 기본 30일)만 가져온다.
본문은 보고서명·제출인 기반 요약 텍스트로 구성하고(분류 입력에 충분),
공시원문(document.xml) 조회는 후속 마일스톤 확장 훅으로 남긴다.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from .. import settings
from .base import RawDoc

log = logging.getLogger("tnm.dart")
KST = ZoneInfo("Asia/Seoul")

_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
_VIEWER_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
_MAX_PAGES = 5            # 종목별 모드 한 사이클 안전 상한 (100건/페이지)
_MARKET_MAX_PAGES = 40    # 전종목 모드 상한 — 4,000건/사이클


class _RateLimiter:
    """호출 간 최소 간격 (kiwoom/client.py 패턴 축약)."""

    def __init__(self, min_interval: float = 0.15) -> None:
        self._min = min_interval
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            delta = time.monotonic() - self._last
            if delta < self._min:
                await asyncio.sleep(self._min - delta)
            self._last = time.monotonic()


_limiter = _RateLimiter()


def _to_doc(it: dict) -> RawDoc:
    report_nm = str(it.get("report_nm", "")).strip()
    corp_name = str(it.get("corp_name", "")).strip()
    flr_nm = str(it.get("flr_nm", "")).strip()
    rm = str(it.get("rm", "")).strip()
    rcept_dt = str(it.get("rcept_dt", "")).strip()   # YYYYMMDD
    try:
        published = datetime.strptime(rcept_dt, "%Y%m%d").replace(tzinfo=KST)
    except ValueError:
        published = datetime.now(KST)
    body = f"{corp_name} 공시 — {report_nm}. 제출인: {flr_nm}."
    if rm:
        body += f" 비고: {rm}"
    return RawDoc(
        source_uid=str(it.get("rcept_no", "")).strip(),
        title=report_nm or f"{corp_name} 공시",
        body=body,
        url=_VIEWER_URL.format(rcept_no=it.get("rcept_no")),
        published_at=published,
    )


def _new_items(payload: dict, cursor: str | None) -> list[dict]:
    """응답에서 커서 초과분만. status 013(결과 없음)은 정상으로 본다."""
    status = str(payload.get("status", ""))
    if status == "013":
        return []
    if status != "000":
        raise RuntimeError(f"DART 오류 status={status}: {payload.get('message', '')}")
    out = []
    for it in payload.get("list", []) or []:
        rcept_no = str(it.get("rcept_no", "")).strip()
        if not rcept_no or (cursor and rcept_no <= cursor):
            continue
        out.append(it)
    return out


def parse_list_payload(payload: dict, cursor: str | None) -> tuple[list[RawDoc], str | None]:
    """list.json 응답 → cursor(rcept_no) 초과분만 RawDoc 으로. 새 커서 = 최대 rcept_no."""
    items = _new_items(payload, cursor)
    docs = [_to_doc(it) for it in items]
    max_rcept = max([str(it["rcept_no"]).strip() for it in items] + [cursor or ""])
    return docs, (max_rcept or cursor)


def parse_market_payload(payload: dict, cursor: str | None
                         ) -> tuple[list[tuple[str, RawDoc]], str | None]:
    """전종목 응답 → [(corp_code, RawDoc)] + 새 커서.

    종목별 모드와 달리 어느 회사 공시인지 응답의 corp_code 로 판별한다.
    호출자가 corp_code → 감시종목 매핑으로 걸러 적재한다.
    """
    items = _new_items(payload, cursor)
    pairs = [(str(it.get("corp_code", "")).strip(), _to_doc(it))
             for it in items if it.get("corp_code")]
    max_rcept = max([str(it["rcept_no"]).strip() for it in items] + [cursor or ""])
    return pairs, (max_rcept or cursor)


async def fetch_market(cursor: str | None, initial_days: int = 3,
                       max_pages: int = _MARKET_MAX_PAGES
                       ) -> tuple[list[tuple[str, RawDoc]], str | None, bool]:
    """corp_code 없이 날짜 범위로 **전체 공시**를 받아온다.

    종목당 1콜(감시 65종목 = 65콜/사이클)에서 목록 몇 콜로 바뀐다. 같은 비용에
    감시목록 밖 종목까지 커버되고, 종목이 늘어도 호출 수가 늘지 않는다.

    오름차순으로 훑고 커서는 **실제로 받아온 최대 rcept_no** 까지만 전진시킨다.
    페이지 상한에 걸려 중간에 끊겨도 건너뛴 구간이 생기지 않는다(다음 사이클이
    이어받는다). 반환 3번째 값은 '아직 남았는가'.
    """
    if cursor and len(cursor) >= 8 and cursor[:8].isdigit():
        bgn_de = cursor[:8]
    else:
        bgn_de = (datetime.now(KST) - timedelta(days=initial_days)).strftime("%Y%m%d")
    pairs: list[tuple[str, RawDoc]] = []
    max_seen = cursor or ""
    more = False
    async with httpx.AsyncClient(timeout=30) as client:
        for page in range(1, max_pages + 1):
            await _limiter.wait()
            r = await client.get(_LIST_URL, params={
                "crtfc_key": settings.DART_API_KEY,
                "bgn_de": bgn_de,
                "end_de": datetime.now(KST).strftime("%Y%m%d"),
                "page_no": page, "page_count": 100,
                "sort": "date", "sort_mth": "asc",
            })
            r.raise_for_status()
            payload = r.json()
            # 필터 기준은 **진입 시점 커서로 고정**한다. 페이지마다 갱신하면
            # sort=date 가 날짜순이지 rcept_no 순이 아니라서, 앞 페이지의 큰
            # 일련번호가 뒤 페이지의 작은 번호를 통째로 걸러 버린다.
            # (실측 2026-07-27: 1,175건 중 291건만 통과했다)
            got, page_max = parse_market_payload(payload, cursor)
            pairs.extend(got)
            if page_max and page_max > max_seen:
                max_seen = page_max
            total_page = int(payload.get("total_page", 1) or 1)
            if page >= total_page:
                break
            if page >= max_pages:
                more = True
    return pairs, (max_seen or cursor), more


class DartCollector:
    name = "dart"

    def enabled(self, stock: dict) -> bool:
        return bool(settings.DART_API_KEY and stock.get("dart_corp_code"))

    async def fetch(self, stock: dict, cursor: str | None
                    ) -> tuple[list[RawDoc], str | None]:
        initial_days = int(settings.COLLECT.get("dart", {}).get("initial_days", 30))
        # 시작일: 커서가 있으면 커서의 날짜(당일 재조회로 경계 유실 방지), 없으면 N일 전
        if cursor and len(cursor) >= 8 and cursor[:8].isdigit():
            bgn_de = cursor[:8]
        else:
            bgn_de = (datetime.now(KST) - timedelta(days=initial_days)).strftime("%Y%m%d")
        all_docs: list[RawDoc] = []
        max_seen = cursor or ""
        async with httpx.AsyncClient(timeout=30) as client:
            for page in range(1, _MAX_PAGES + 1):
                await _limiter.wait()
                r = await client.get(_LIST_URL, params={
                    "crtfc_key": settings.DART_API_KEY,
                    "corp_code": stock["dart_corp_code"],
                    "bgn_de": bgn_de,
                    "end_de": datetime.now(KST).strftime("%Y%m%d"),
                    "page_no": page,
                    "page_count": 100,
                    "sort": "date", "sort_mth": "asc",
                })
                r.raise_for_status()
                payload = r.json()
                # 전종목 모드와 같은 이유로 필터 기준은 진입 커서로 고정한다
                docs, page_max = parse_list_payload(payload, cursor)
                all_docs.extend(docs)
                if page_max and page_max > max_seen:
                    max_seen = page_max
                total_page = int(payload.get("total_page", 1) or 1)
                if page >= total_page:
                    break
        return all_docs, (max_seen or cursor)
