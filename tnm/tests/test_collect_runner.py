"""M2·M3 완료판정 — 수집 러너: 적재·커서 전진·예외 격리·중복 가드 SQL."""
import asyncio
from datetime import datetime, timezone

from app import db, settings
from app.collect import CollectRunner, doc_to_row
from app.collectors.base import RawDoc


async def _noop(*a, **k):
    return None


def _doc(uid="u1", title="제목", body="본문  홍길동 기자"):
    return RawDoc(source_uid=uid, title=title, body=body,
                  url=f"https://x/{uid}",
                  published_at=datetime(2026, 7, 24, tzinfo=timezone.utc))


def test_doc_to_row_has_hash_and_norm():
    row = doc_to_row(_doc())
    assert len(row["content_hash"]) == 64
    assert "기자" not in row["norm_body"] and "본문" in row["norm_body"]


def test_run_once_inserts_and_advances_cursor(monkeypatch):
    runner = CollectRunner()
    monkeypatch.setattr(db, "ready", True)
    # 이 테스트는 종목별 모드를 검증한다 (전종목 모드는 별도 테스트)
    monkeypatch.setitem(settings.COLLECT, "dart", {"market_mode": False})

    class FakeCol:
        name = "dart"

        def enabled(self, stock):
            return stock["ticker"] != "000002"          # 한 종목은 비활성

        async def fetch(self, stock, cursor):
            if stock["ticker"] == "000003":
                raise OSError("원격 오류")               # 예외 격리 확인
            return [_doc()], "CUR2"

    runner.collectors = {"dart": [FakeCol()]}
    inserted, cursors = [], []

    async def fake_active():
        return [{"id": 1, "ticker": "000001", "name": "A", "dart_corp_code": "x",
                 "last_collected_at": {"dart": "CUR1"}},
                {"id": 2, "ticker": "000002", "name": "B", "dart_corp_code": "x",
                 "last_collected_at": {}},
                {"id": 3, "ticker": "000003", "name": "C", "dart_corp_code": "x",
                 "last_collected_at": {}}]

    async def fake_insert(wid, source, rows):
        inserted.append((wid, source, len(rows)))
        return len(rows)

    async def fake_cursor(ticker, source, cursor):
        cursors.append((ticker, source, cursor))

    monkeypatch.setattr(db, "active_watch_for_collect", fake_active)
    monkeypatch.setattr(db, "insert_raw_items", fake_insert)
    monkeypatch.setattr(db, "set_cursor", fake_cursor)
    monkeypatch.setattr(db, "mark_attempt", _noop)

    result = asyncio.run(runner.run_once("dart"))
    assert inserted == [(1, "dart", 1)]                  # 성공 종목만 적재
    assert cursors == [("000001", "dart", "CUR2")]       # 성공 종목만 커서 전진
    assert result["errors"] == 1 and result["stocks"] == 1


def test_run_once_guards(monkeypatch):
    runner = CollectRunner()
    monkeypatch.setattr(db, "ready", False)
    assert asyncio.run(runner.run_once("dart")) == {"skipped": "DB 미준비"}
    monkeypatch.setattr(db, "ready", True)
    runner.running.add("news")
    assert asyncio.run(runner.run_once("news")) == {"skipped": "이미 실행 중"}


def test_insert_raw_items_dedup_sql(monkeypatch):
    """적재 SQL 계약: (source,source_uid) 충돌 무시 + 동일 종목 content_hash 중복 스킵."""
    captured = []

    class FakeCursor:
        rowcount = 1

    class FakeConn:
        async def execute(self, sql, args=None):
            captured.append((" ".join(sql.split()).lower(), args))
            return FakeCursor()

    class FakePool:
        def connection(self):
            class Ctx:
                async def __aenter__(self):
                    return FakeConn()

                async def __aexit__(self, *a):
                    return False
            return Ctx()

    monkeypatch.setattr(db, "_pool", FakePool())
    row = doc_to_row(_doc())
    n = asyncio.run(db.insert_raw_items(7, "rss", [row]))
    assert n == 1
    sql, args = captured[0]
    assert "on conflict (source, source_uid) do nothing" in sql
    assert "where not exists" in sql and "content_hash" in sql
    assert args[0] == 7 and args[1] == "rss"
    assert args[-2] == 7 and args[-1] == row["content_hash"]   # not-exists 서브쿼리 인자
