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
_MAX_PAGES = 5           # 한 사이클 안전 상한 (100건/페이지)


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


def parse_list_payload(payload: dict, cursor: str | None) -> tuple[list[RawDoc], str | None]:
    """list.json 응답 → cursor(rcept_no) 초과분만 RawDoc 으로. 새 커서 = 최대 rcept_no."""
    status = str(payload.get("status", ""))
    if status == "013":              # 조회 결과 없음 — 정상
        return [], cursor
    if status != "000":
        raise RuntimeError(f"DART 오류 status={status}: {payload.get('message', '')}")
    docs: list[RawDoc] = []
    max_rcept = cursor or ""
    for it in payload.get("list", []) or []:
        rcept_no = str(it.get("rcept_no", "")).strip()
        if not rcept_no:
            continue
        if cursor and rcept_no <= cursor:
            continue
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
        docs.append(RawDoc(
            source_uid=rcept_no,
            title=report_nm or f"{corp_name} 공시",
            body=body,
            url=_VIEWER_URL.format(rcept_no=rcept_no),
            published_at=published,
        ))
        if rcept_no > max_rcept:
            max_rcept = rcept_no
    return docs, (max_rcept or cursor)


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
        new_cursor = cursor
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
                docs, new_cursor = parse_list_payload(payload, new_cursor)
                all_docs.extend(docs)
                total_page = int(payload.get("total_page", 1) or 1)
                if page >= total_page:
                    break
        return all_docs, new_cursor
