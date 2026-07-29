"""공용 적재 경로 — 수집한 문서를 분석 파이프라인에 태운다.

소스가 늘어도 이 뒤는 전부 같다는 것이 설계의 요지다. 그래서 이 테스트는
증권사 리포트가 아니라 **경로 자체**를 본다.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app import ingest


class _FakeDB:
    def __init__(self, watch=None, known=None, origin_count=0):
        self._watch = dict(watch or {})
        self._known = set(known or self._watch)
        self._origin_count = origin_count
        self.registered: list[dict] = []
        self.inserted: list[tuple[int, str, list[dict]]] = []
        self.register_tier: str | None = None
        self.register_origin: str | None = None
        self.fail_wids: set[int] = set()

    async def watch_ids_by_ticker(self):
        return dict(self._watch)

    async def known_tickers(self):
        return set(self._known)

    async def count_origin(self, origin):
        return self._origin_count

    async def add_discovered(self, rows, tier="other", origin="dart"):
        self.register_tier, self.register_origin = tier, origin
        for i, r in enumerate(rows):
            self._watch[r["ticker"]] = 900 + i
            self._known.add(r["ticker"])
        self.registered.extend(rows)
        return len(rows)

    async def insert_raw_items(self, wid, source, rows):
        if wid in self.fail_wids:
            raise RuntimeError("적재 실패")
        self.inserted.append((wid, source, rows))
        return len(rows)


def _item(ticker, name="종목", uid="u1"):
    return {"ticker": ticker, "name": name, "row": {
        "source_uid": uid, "title": "제목", "body": "본문",
        "url": "https://x/1", "published_at": datetime.now(timezone.utc),
        "content_hash": "h" * 64, "norm_body": "본문"}}


def _run(fake, items, monkeypatch, **kw):
    monkeypatch.setattr(ingest, "db", fake)
    return asyncio.run(ingest.attach_docs(items, "research",
                                          origin=kw.pop("origin", "research"), **kw))


def test_watched_stock_goes_straight_in(monkeypatch):
    fake = _FakeDB(watch={"005930": 7})
    out = _run(fake, [_item("005930")], monkeypatch)
    assert out["inserted"] == 1 and out["registered"] == 0
    wid, source, rows = fake.inserted[0]
    assert wid == 7 and source == "research"


def test_unwatched_stock_is_registered_then_ingested(monkeypatch):
    """관심종목 밖 종목도 분석에 태운다 — raw_items 는 종목이 필수라 먼저 등록한다."""
    fake = _FakeDB(watch={})
    out = _run(fake, [_item("000660", "SK하이닉스")], monkeypatch)
    assert out["registered"] == 1 and out["inserted"] == 1
    assert fake.registered[0] == {"ticker": "000660", "name": "SK하이닉스"}
    assert out["deferred"] == 0


def test_registration_uses_slow_tier_and_source_origin(monkeypatch):
    """tier 를 'trade' 로 넣으면 종목마다 30분 폴링이 붙어 호출이 폭증한다."""
    fake = _FakeDB(watch={})
    _run(fake, [_item("000660")], monkeypatch)
    assert fake.register_tier == "other"
    assert fake.register_origin == "research", "출처별 상한·성적을 따로 재려면 달라야 한다"


def test_excluded_stock_is_not_revived(monkeypatch):
    """사용자가 제외한 종목을 소스가 되살리면 그 결정을 덮는 것이다."""
    fake = _FakeDB(watch={}, known={"000660"})     # 등록된 적 있으나 지금 비활성
    out = _run(fake, [_item("000660")], monkeypatch)
    assert fake.registered == []
    assert out["inserted"] == 0
    assert out["deferred"] == 1, "버린 게 아니라 미룬 것으로 세야 한다"


def test_per_cycle_cap_defers_the_rest(monkeypatch):
    fake = _FakeDB(watch={})
    items = [dict(_item(f"00000{i}", uid=f"u{i}"), key=i) for i in range(5)]
    out = _run(fake, items, monkeypatch, cfg={"max_per_cycle": 2, "max_total": 100})
    assert out["registered"] == 2
    assert out["inserted"] == 2
    assert out["deferred"] == 3
    # 어느 문서가 밀렸는지 알려야 호출자가 재시도 횟수에서 뺄 수 있다.
    # 이걸 안 주면 상한에 밀린 문서가 시도 상한에 걸려 영영 버려진다.
    assert sorted(out["deferred_keys"]) == [2, 3, 4]


def test_total_cap_blocks_registration(monkeypatch):
    fake = _FakeDB(watch={}, origin_count=250)
    out = _run(fake, [_item("000660")], monkeypatch,
               cfg={"max_per_cycle": 15, "max_total": 250})
    assert out["registered"] == 0 and out["deferred"] == 1


def test_total_cap_overrun_does_not_go_negative(monkeypatch):
    """이미 상한을 넘긴 상태(설정을 낮췄을 때)에서 음수 슬라이스가 나면 안 된다."""
    fake = _FakeDB(watch={}, origin_count=999)
    out = _run(fake, [_item("000660")], monkeypatch, cfg={"max_total": 250})
    assert out["registered"] == 0 and fake.registered == []


def test_one_stock_failure_does_not_sink_the_batch(monkeypatch):
    fake = _FakeDB(watch={"005930": 7, "000660": 8})
    fake.fail_wids = {7}
    out = _run(fake, [_item("005930"), _item("000660", uid="u2")], monkeypatch)
    assert out["errors"] == 1 and out["inserted"] == 1


def test_disabled_by_config(monkeypatch):
    fake = _FakeDB(watch={"005930": 7})
    out = _run(fake, [_item("005930")], monkeypatch, cfg={"enabled": False})
    assert out.get("skipped") == "비활성"
    assert fake.inserted == []


def test_empty_input_is_noop(monkeypatch):
    fake = _FakeDB()
    out = _run(fake, [], monkeypatch)
    assert out["docs"] == 0 and "skipped" not in out


def test_doc_body_keeps_key_values_in_text():
    """칼럼에만 있는 값은 분류 LLM 이 못 읽는다 — 본문 문장으로 들어가야 한다."""
    body = ingest.doc_body("[SK증권 리포트] 목표가 상향",
                           [None, "목표주가 320,000원", "투자의견 매수", None])
    assert "320,000원" in body and "투자의견 매수" in body
    assert " /  / " not in body, "빈 항목이 구분자만 남기면 안 된다"
