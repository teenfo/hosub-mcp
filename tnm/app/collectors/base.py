"""수집 어댑터 공통 규약.

각 어댑터는 name(소스명 — raw_items.source 값), enabled(), fetch() 를 구현한다.
fetch 는 (신규 문서 목록, 갱신된 커서) 를 반환하고, 커서 형식은 소스별로 자유
(DART: 마지막 rcept_no, 뉴스: 마지막 발행시각 ISO). 커서 저장은 호출자(runner)가
tnm_watchlist.last_collected_at(jsonb) 에 소스명 키로 한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass
class RawDoc:
    source_uid: str          # 소스 내 고유 ID (rcept_no, URL 해시 등)
    title: str
    body: str | None
    url: str                 # 원문 링크 — 알림에 반드시 포함 (요청서 P5)
    published_at: datetime   # tz-aware


class Collector(Protocol):
    name: str

    def enabled(self, stock: dict) -> bool:
        """이 종목에 대해 수집 가능한가 (키 유무·corp_code 유무 등)."""
        ...

    async def fetch(self, stock: dict, cursor: str | None
                    ) -> tuple[list[RawDoc], str | None]:
        """cursor 이후 신규 문서와 새 커서를 반환. 신규 없으면 ([], cursor)."""
        ...
