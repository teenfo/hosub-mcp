"""구글 뉴스 RSS 검색 수집기 — 기본 뉴스 소스 (키·호출한도 없음).

news.google.com/rss/search 를 종목명으로 조회한다. 커서 = 마지막 발행시각(ISO).
본문은 링크를 따라가 trafilatura 로 추출하고, 실패하면 RSS description 으로
폴백한다 (요청서 P5: 원문 링크는 항상 보존).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx

from .. import settings
from ..pipeline.normalize import clean_text
from .base import RawDoc

log = logging.getLogger("tnm.rss")

_SEARCH_URL = "https://news.google.com/rss/search"
_MAX_BODY_FETCH = 10       # 한 사이클·종목당 본문 추출 상한 (예의상 제한)


def parse_feed(xml_text: str) -> list[dict]:
    """RSS xml → [{title, link, description, published(datetime), source}]."""
    out: list[dict] = []
    root = ET.fromstring(xml_text)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_raw = (item.findtext("pubDate") or "").strip()
        if not (title and link and pub_raw):
            continue
        try:
            published = parsedate_to_datetime(pub_raw)
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        out.append({
            "title": title,
            "link": link,
            "description": clean_text(item.findtext("description") or ""),
            "published": published,
            "source": (item.findtext("source") or "").strip(),
        })
    return out


def filter_new(items: list[dict], cursor: str | None,
               initial_days: int | None = None) -> tuple[list[dict], str | None]:
    """커서(ISO 시각) 이후 발행분만. 새 커서 = 최대 발행시각.

    첫 실행(cursor 없음)은 initial_days 이내 발행분으로 제한 — 과거 백로그가
    임베딩·분류 큐를 불필요하게 채우는 것을 막는다.
    """
    since = None
    if cursor:
        try:
            since = datetime.fromisoformat(cursor)
        except ValueError:
            since = None
    if since is None and initial_days:
        since = datetime.now(timezone.utc) - timedelta(days=int(initial_days))
    fresh = [it for it in items if since is None or it["published"] > since]
    latest = max((it["published"] for it in items), default=None)
    return fresh, (latest.isoformat() if latest else cursor)


def uid_for(link: str) -> str:
    return hashlib.sha256(link.encode("utf-8")).hexdigest()[:32]


async def fetch_article_body(client: httpx.AsyncClient, url: str) -> str | None:
    """기사 링크 fetch + trafilatura 본문 추출. 어떤 실패든 None (폴백은 호출자)."""
    try:
        import trafilatura
        r = await client.get(url, follow_redirects=True, timeout=10,
                             headers={"User-Agent": "Mozilla/5.0 (tnm-collector)"})
        r.raise_for_status()
        text = trafilatura.extract(r.text, include_comments=False)
        return text.strip() if text else None
    except Exception:  # noqa: BLE001 — 본문은 부가 정보, 실패 시 description 폴백
        return None


class GoogleNewsCollector:
    name = "rss"

    def enabled(self, stock: dict) -> bool:
        return True                        # 키 불필요 — 항상 사용

    async def fetch(self, stock: dict, cursor: str | None
                    ) -> tuple[list[RawDoc], str | None]:
        params = {"q": f'"{stock["name"]}"', "hl": "ko", "gl": "KR", "ceid": "KR:ko"}
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(_SEARCH_URL, params=params, follow_redirects=True)
            r.raise_for_status()
            initial_days = int(settings.COLLECT.get("news", {}).get("initial_days", 7))
            fresh, new_cursor = filter_new(parse_feed(r.text), cursor,
                                           initial_days=initial_days)
            docs: list[RawDoc] = []
            fetched = 0
            fetch_body = bool(settings.COLLECT.get("news", {}).get("fetch_body", True))
            for it in fresh:
                body = None
                if fetch_body and fetched < _MAX_BODY_FETCH:
                    body = await fetch_article_body(client, it["link"])
                    fetched += 1
                    await asyncio.sleep(0.2)     # 발행사 서버 예의상 간격
                docs.append(RawDoc(
                    source_uid=uid_for(it["link"]),
                    title=it["title"],
                    body=body or it["description"] or None,
                    url=it["link"],
                    published_at=it["published"],
                ))
        return docs, new_cursor
