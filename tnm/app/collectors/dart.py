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


def _published_at(rcept_dt: str, now: datetime | None = None) -> datetime:
    """접수일(YYYYMMDD) → 발행 시각 추정.

    list.json 은 **날짜만** 준다(rcept_no 의 일련번호도 시각이 아니다). 자정으로
    두면 15:00 에 나온 거래정지 공시가 15시간 전 것으로 보인다 — 신선도로 줄을
    세우는 곳(분류 큐 우선순위·promote 의 window_hours)에서 공시가 구조적으로
    뒤로 밀린다.

    **오늘 접수분은 수집 시각을 쓴다.** 전종목 모드가 장중 10분 주기로 도므로
    수집 시각은 실제 접수 시각의 상한이고 오차가 한 주기 안이다. 어제 이전은
    그날 자정 그대로 — 소급 백필이 전부 '방금'이 되어 버리면 안 된다.
    """
    now = now or datetime.now(KST)
    try:
        day = datetime.strptime(rcept_dt, "%Y%m%d").replace(tzinfo=KST)
    except ValueError:
        return now
    return now if day.date() == now.date() else day


def _to_doc(it: dict) -> RawDoc:
    report_nm = str(it.get("report_nm", "")).strip()
    corp_name = str(it.get("corp_name", "")).strip()
    flr_nm = str(it.get("flr_nm", "")).strip()
    rm = str(it.get("rm", "")).strip()
    rcept_dt = str(it.get("rcept_dt", "")).strip()   # YYYYMMDD — **시각이 없다**
    published = _published_at(rcept_dt)
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


def _meta(it: dict) -> dict:
    """RawDoc 이 못 담는 원본 식별 정보.

    **DART 응답에는 stock_code 가 이미 들어 있다.** 종전에는 이걸 읽지도 않고
    버렸는데, 감시목록 밖 공시에서 새 종목을 발굴하려면 바로 이 값이 필요하다
    (corp_code → ticker 역매핑 캐시를 만들 필요가 없다). 비상장사는 빈 문자열이다.
    """
    return {
        "stock_code": str(it.get("stock_code", "")).strip(),
        "corp_name": str(it.get("corp_name", "")).strip(),
        "report_nm": str(it.get("report_nm", "")).strip(),
        "rcept_no": str(it.get("rcept_no", "")).strip(),
    }


def parse_market_payload(payload: dict, cursor: str | None
                         ) -> tuple[list[tuple[str, RawDoc, dict]], str | None]:
    """전종목 응답 → [(corp_code, RawDoc, meta)] + 새 커서.

    종목별 모드와 달리 어느 회사 공시인지 응답의 corp_code 로 판별한다.
    호출자가 corp_code → 감시종목 매핑으로 걸러 적재하고, **매칭되지 않은 것은
    meta 로 신규 종목 발굴에 쓴다**(discover.py).
    """
    items = _new_items(payload, cursor)
    pairs = [(str(it.get("corp_code", "")).strip(), _to_doc(it), _meta(it))
             for it in items if it.get("corp_code")]
    max_rcept = max([str(it["rcept_no"]).strip() for it in items] + [cursor or ""])
    return pairs, (max_rcept or cursor)


async def fetch_market(cursor: str | None, initial_days: int = 3,
                       max_pages: int = _MARKET_MAX_PAGES
                       ) -> tuple[list[tuple[str, RawDoc, dict]], str | None, bool]:
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
    pairs: list[tuple[str, RawDoc, dict]] = []
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
