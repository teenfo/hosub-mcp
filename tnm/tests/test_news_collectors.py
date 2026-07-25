"""M3 완료판정 — 구글 RSS 파싱·커서, 본문 폴백, 네이버 활성 조건·예산."""
import asyncio
from datetime import datetime, timezone

from app import settings
from app.collectors import rss as rss_mod
from app.collectors.naver import NaverNewsCollector, daily_budget, parse_items
from app.collectors.rss import filter_new, parse_feed, uid_for

RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>q - Google 뉴스</title>
<item>
  <title>효성중공업, 북미 변압기 수주</title>
  <link>https://news.example.com/a1</link>
  <pubDate>Fri, 24 Jul 2026 09:00:00 GMT</pubDate>
  <description>&lt;a href="x"&gt;효성중공업&lt;/a&gt; 수주 소식</description>
  <source url="https://example.com">예시경제</source>
</item>
<item>
  <title>지난 기사</title>
  <link>https://news.example.com/old</link>
  <pubDate>Wed, 22 Jul 2026 01:00:00 GMT</pubDate>
  <description>옛날 것</description>
</item>
<item>
  <title>pubDate 없음 — 스킵</title>
  <link>https://news.example.com/bad</link>
</item>
</channel></rss>"""


def test_parse_feed_and_cursor_filter():
    items = parse_feed(RSS_XML)
    assert len(items) == 2                                  # pubDate 없는 항목 제외
    assert items[0]["description"] == "효성중공업 수주 소식"  # 태그 제거·공백 병합
    fresh, cursor = filter_new(items, "2026-07-23T00:00:00+00:00")
    assert [i["link"] for i in fresh] == ["https://news.example.com/a1"]
    assert cursor == datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc).isoformat()
    # 재실행: 새 커서 이후 발행분 없음 → 0건 (완전중복 0)
    again, cursor2 = filter_new(items, cursor)
    assert again == [] and cursor2 == cursor


def test_uid_stable():
    assert uid_for("https://a") == uid_for("https://a")
    assert uid_for("https://a") != uid_for("https://b")


def test_rss_body_fallback_to_description(monkeypatch):
    """기사 본문 추출 실패 → description 폴백 (M3 판정)."""
    col = rss_mod.GoogleNewsCollector()

    class FakeResp:
        text = RSS_XML

        def raise_for_status(self):
            pass

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return FakeResp()

    monkeypatch.setattr(rss_mod.httpx, "AsyncClient", lambda **k: FakeClient())

    async def broken_fetch(client, url):
        return None                       # trafilatura 실패 시뮬레이션

    monkeypatch.setattr(rss_mod, "fetch_article_body", broken_fetch)
    docs, cursor = asyncio.run(col.fetch({"name": "효성중공업"}, None))
    assert len(docs) == 2
    assert docs[0].body == "효성중공업 수주 소식"             # 폴백 본문
    assert docs[0].url == "https://news.example.com/a1"     # 원문 링크 보존 (P5)
    assert cursor is not None


def test_naver_enabled_and_parse(monkeypatch):
    col = NaverNewsCollector()
    monkeypatch.setattr(settings, "NAVER_CLIENT_ID", "")
    monkeypatch.setattr(settings, "NAVER_CLIENT_SECRET", "")
    assert not col.enabled({})                              # 키 없으면 비활성
    payload = {"items": [
        {"title": "<b>삼성전자</b> 실적", "originallink": "https://n.example/1",
         "link": "https://naver/1", "description": "요약<b>!</b>",
         "pubDate": "Fri, 24 Jul 2026 18:00:00 +0900"},
        {"title": "옛 기사", "originallink": "https://n.example/2",
         "link": "https://naver/2", "description": "",
         "pubDate": "Mon, 20 Jul 2026 09:00:00 +0900"},
    ]}
    docs, cursor = parse_items(payload, "2026-07-22T00:00:00+09:00")
    assert len(docs) == 1
    assert docs[0].title == "삼성전자 실적" and docs[0].url == "https://n.example/1"
    assert "2026-07-24" in cursor


def test_naver_daily_budget():
    assert daily_budget(60, 30) == 2880                     # 한도 25,000 내
    assert daily_budget(600, 1) > 25_000                    # 초과 감지 가능
