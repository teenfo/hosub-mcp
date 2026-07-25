"""네이버 뉴스 검색 API 수집기 — 선택 어댑터 (키 설정 시에만 활성).

커서 = 마지막 발행시각(ISO). 같은 기사가 구글 RSS 로도 들어오면
content_hash / 임베딩 dedup 단계가 흡수한다.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from .. import settings
from ..pipeline.normalize import clean_text
from .base import RawDoc

log = logging.getLogger("tnm.naver")

_URL = "https://openapi.naver.com/v1/search/news.json"
DAILY_LIMIT = 25_000


def daily_budget(n_stocks: int, interval_min: int) -> int:
    """일일 예상 호출수 — 기동 시 로그로 출력해 한도 초과를 사전 감지 (비기능 9장)."""
    if interval_min <= 0:
        return 0
    return n_stocks * (24 * 60 // interval_min)


def parse_items(payload: dict, cursor: str | None) -> tuple[list[RawDoc], str | None]:
    since = None
    if cursor:
        try:
            since = datetime.fromisoformat(cursor)
        except ValueError:
            since = None
    docs: list[RawDoc] = []
    latest = since
    for it in payload.get("items", []) or []:
        link = str(it.get("originallink") or it.get("link") or "").strip()
        if not link:
            continue
        try:
            published = parsedate_to_datetime(str(it.get("pubDate", "")))
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if latest is None or published > latest:
            latest = published
        if since is not None and published <= since:
            continue
        docs.append(RawDoc(
            source_uid=hashlib.sha256(link.encode()).hexdigest()[:32],
            title=clean_text(it.get("title", "")),
            body=clean_text(it.get("description", "")) or None,
            url=link,
            published_at=published,
        ))
    return docs, (latest.isoformat() if latest else cursor)


class NaverNewsCollector:
    name = "naver"

    def enabled(self, stock: dict) -> bool:
        return bool(settings.NAVER_CLIENT_ID and settings.NAVER_CLIENT_SECRET)

    async def fetch(self, stock: dict, cursor: str | None
                    ) -> tuple[list[RawDoc], str | None]:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(_URL, params={
                "query": stock["name"], "display": 50, "sort": "date",
            }, headers={
                "X-Naver-Client-Id": settings.NAVER_CLIENT_ID,
                "X-Naver-Client-Secret": settings.NAVER_CLIENT_SECRET,
            })
            r.raise_for_status()
            return parse_items(r.json(), cursor)
