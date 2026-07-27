"""DART 전종목 수집 + 뉴스 계층별 주기.

두 소스는 확장 방향이 정반대다. 여기서 지키는 것은 그 비대칭이다.
- DART: list.json 이 corp_code 없이 전체를 주므로 **종목이 늘어도 호출이 안 는다**.
  종목별 모드(65종목=65콜)로 되돌아가면 잡는다.
- 뉴스: 구글 RSS 는 종목명 검색이라 종목당 1콜이 강제된다. 전종목이 불가능하므로
  계층별로 주기를 차등한다. 계층이 무시되면 잡는다.
"""
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import db, settings
from app.collect import (DART_CURSOR_KEY, CollectRunner, is_due, tier_interval)
from app.collectors import dart

KST = ZoneInfo("Asia/Seoul")


def _item(rcept_no, corp_code="00126380", corp_name="삼성전자",
          report_nm="주요사항보고서", rcept_dt="20260727"):
    return {"rcept_no": rcept_no, "corp_code": corp_code, "corp_name": corp_name,
            "report_nm": report_nm, "flr_nm": corp_name, "rm": "",
            "rcept_dt": rcept_dt}


def _payload(items, total_page=1):
    return {"status": "000", "list": items, "total_page": total_page}


async def _noop(*a, **k):
    return None


# --- ① 전종목 응답 파싱 ---

def test_parse_market_payload_keeps_corp_code():
    """어느 회사 공시인지 corp_code 로 판별할 수 있어야 매칭이 가능하다."""
    pairs, cursor = dart.parse_market_payload(
        _payload([_item("20260727000001", corp_code="AAA"),
                  _item("20260727000002", corp_code="BBB")]), None)
    assert [c for c, _ in pairs] == ["AAA", "BBB"]
    assert cursor == "20260727000002"
    assert pairs[0][1].url.endswith("20260727000001")


def test_parse_market_payload_respects_cursor():
    pairs, cursor = dart.parse_market_payload(
        _payload([_item("20260727000001"), _item("20260727000005")]),
        "20260727000003")
    assert [p[1].source_uid for p in pairs] == ["20260727000005"]
    assert cursor == "20260727000005"


def test_parse_market_payload_skips_missing_corp_code():
    pairs, _ = dart.parse_market_payload(
        _payload([{"rcept_no": "20260727000001", "report_nm": "x"}]), None)
    assert pairs == []


def test_parse_market_payload_no_results_is_normal():
    pairs, cursor = dart.parse_market_payload({"status": "013"}, "CUR")
    assert pairs == [] and cursor == "CUR"


def test_parse_market_payload_raises_on_error():
    with pytest.raises(RuntimeError):
        dart.parse_market_payload({"status": "020", "message": "한도초과"}, None)


def test_per_stock_parser_still_works():
    """전종목 모드를 켜도 종목별 경로가 살아 있어야 한다(폴백)."""
    docs, cursor = dart.parse_list_payload(_payload([_item("20260727000009")]), None)
    assert len(docs) == 1 and cursor == "20260727000009"


# --- ② 호출 수가 종목 수와 무관하다 ---

def _fake_client(pages):
    """httpx.AsyncClient 대역 — 페이지별 응답을 순서대로 돌려준다."""
    calls = []

    class FakeResp:
        def __init__(self, payload):
            self._p = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._p

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            calls.append(params)
            idx = int((params or {}).get("page_no", 1)) - 1
            return FakeResp(pages[min(idx, len(pages) - 1)])

    return FakeClient, calls


def test_fetch_market_does_not_send_corp_code(monkeypatch):
    """corp_code 를 안 보내는 것이 전종목 모드의 전부다 — 보내면 종목별로 회귀."""
    FakeClient, calls = _fake_client([_payload([_item("20260727000001")])])
    monkeypatch.setattr(dart.httpx, "AsyncClient", lambda **k: FakeClient())
    monkeypatch.setattr(settings, "DART_API_KEY", "K")
    pairs, cursor, more = asyncio.run(dart.fetch_market(None))
    assert len(pairs) == 1 and cursor == "20260727000001" and more is False
    assert "corp_code" not in calls[0]
    assert len(calls) == 1                       # 종목 수와 무관하게 1콜


def test_fetch_market_pages_until_last(monkeypatch):
    pages = [_payload([_item(f"2026072700000{i}")], total_page=3) for i in (1, 2, 3)]
    FakeClient, calls = _fake_client(pages)
    monkeypatch.setattr(dart.httpx, "AsyncClient", lambda **k: FakeClient())
    monkeypatch.setattr(settings, "DART_API_KEY", "K")
    pairs, cursor, more = asyncio.run(dart.fetch_market(None))
    assert len(calls) == 3 and len(pairs) == 3 and more is False


def test_fetch_market_cursor_only_advances_over_fetched(monkeypatch):
    """페이지 상한에 걸려도 커서가 건너뛰지 않아야 한다 — 공시 유실 방지."""
    pages = [_payload([_item("20260727000001")], total_page=99)]
    FakeClient, _ = _fake_client(pages)
    monkeypatch.setattr(dart.httpx, "AsyncClient", lambda **k: FakeClient())
    monkeypatch.setattr(settings, "DART_API_KEY", "K")
    pairs, cursor, more = asyncio.run(dart.fetch_market(None, max_pages=2))
    assert cursor == "20260727000001"      # 실제로 받아온 만큼만
    assert more is True                    # 남았다고 알린다


def test_fetch_market_resumes_from_cursor_date(monkeypatch):
    """커서가 있으면 그 날짜부터 — 30일치를 매번 다시 훑지 않는다."""
    FakeClient, calls = _fake_client([_payload([])])
    monkeypatch.setattr(dart.httpx, "AsyncClient", lambda **k: FakeClient())
    monkeypatch.setattr(settings, "DART_API_KEY", "K")
    asyncio.run(dart.fetch_market("20260720000123"))
    assert calls[0]["bgn_de"] == "20260720"


# --- ③ 러너: 전종목 → 우리 종목 매칭 ---

def _runner_env(monkeypatch, pairs, by_corp):
    runner = CollectRunner()
    monkeypatch.setattr(db, "ready", True)
    monkeypatch.setattr(settings, "DART_API_KEY", "K")
    monkeypatch.setitem(settings.COLLECT, "dart", {"market_mode": True})
    state = {}
    inserted = []

    async def fake_fetch(cursor, initial_days=3, max_pages=40):
        return pairs, "CUR9", False

    async def fake_get_state(key):
        return state.get(key)

    async def fake_set_state(key, value):
        state[key] = value

    async def fake_by_corp():
        return by_corp

    async def fake_insert(wid, source, rows):
        inserted.append((wid, source, len(rows)))
        return len(rows)

    monkeypatch.setattr(dart, "fetch_market", fake_fetch)
    monkeypatch.setattr(db, "get_state", fake_get_state)
    monkeypatch.setattr(db, "set_state", fake_set_state)
    monkeypatch.setattr(db, "watch_by_corp_code", fake_by_corp)
    monkeypatch.setattr(db, "insert_raw_items", fake_insert)
    monkeypatch.setattr(db, "mark_attempt", _noop)
    return runner, state, inserted


def test_market_run_inserts_only_matched(monkeypatch):
    pairs = dart.parse_market_payload(
        _payload([_item("20260727000001", corp_code="AAA"),
                  _item("20260727000002", corp_code="ZZZ")]), None)[0]
    runner, state, inserted = _runner_env(
        monkeypatch, pairs, {"AAA": {"id": 7, "ticker": "005930", "name": "삼성전자"}})
    out = asyncio.run(runner.run_once("dart"))
    assert inserted == [(7, "dart", 1)]        # 감시 중인 종목만 적재
    assert out["unmatched"] == 1               # 나머지는 세기만 한다
    assert out["inserted"] == 1 and out["mode"] == "market"
    assert state[DART_CURSOR_KEY] == "CUR9"    # 전역 커서 전진


def test_market_run_groups_multiple_docs_per_stock(monkeypatch):
    pairs = dart.parse_market_payload(
        _payload([_item("20260727000001", corp_code="AAA"),
                  _item("20260727000002", corp_code="AAA")]), None)[0]
    runner, _, inserted = _runner_env(
        monkeypatch, pairs, {"AAA": {"id": 7, "ticker": "005930", "name": "A"}})
    asyncio.run(runner.run_once("dart"))
    assert inserted == [(7, "dart", 2)]        # 종목당 1회 적재로 묶는다


def test_market_run_survives_insert_failure(monkeypatch):
    pairs = dart.parse_market_payload(
        _payload([_item("20260727000001", corp_code="AAA"),
                  _item("20260727000002", corp_code="BBB")]), None)[0]
    runner, _, _ = _runner_env(monkeypatch, pairs, {
        "AAA": {"id": 7, "ticker": "005930", "name": "A"},
        "BBB": {"id": 8, "ticker": "000660", "name": "B"}})

    async def boom(wid, source, rows):
        if wid == 7:
            raise OSError("적재 실패")
        return len(rows)

    monkeypatch.setattr(db, "insert_raw_items", boom)
    out = asyncio.run(runner.run_once("dart"))
    assert out["errors"] == 1 and out["stocks"] == 1     # 나머지는 계속


def test_market_run_without_key_is_safe(monkeypatch):
    runner, _, inserted = _runner_env(monkeypatch, [], {})
    monkeypatch.setattr(settings, "DART_API_KEY", "")
    out = asyncio.run(runner.run_once("dart"))
    assert "skipped" in out and inserted == []


def test_market_mode_can_be_disabled(monkeypatch):
    """폴백 경로가 살아 있어야 한다 — 전종목 API 가 막히면 되돌릴 수 있다."""
    runner = CollectRunner()
    monkeypatch.setattr(db, "ready", True)
    monkeypatch.setitem(settings.COLLECT, "dart", {"market_mode": False})
    monkeypatch.setattr(db, "mark_attempt", _noop)

    async def fake_active():
        return []

    monkeypatch.setattr(db, "active_watch_for_collect", fake_active)
    out = asyncio.run(runner.run_once("dart"))
    assert out.get("mode") != "market"


# --- ④ 뉴스 계층별 주기 ---

def test_tier_interval_defaults_and_override():
    assert tier_interval("trade", {}) == 30
    assert tier_interval("collect", {}) == 120
    assert tier_interval("other", {}) == 720
    assert tier_interval("모르는tier", {}) == 720        # 알 수 없으면 가장 드물게
    assert tier_interval("trade", {"tier_interval_min": {"trade": 5}}) == 5


def test_is_due_first_time_always():
    now = datetime(2026, 7, 27, 10, 0, tzinfo=KST)
    assert is_due({"tier": "collect", "last_attempt": {}}, "news", {}, now)


def test_is_due_respects_tier():
    now = datetime(2026, 7, 27, 10, 0, tzinfo=KST)
    ago = lambda m: {"news": (now - timedelta(minutes=m)).isoformat()}  # noqa: E731
    # 매매 대상(30분): 40분 전 → 대상 / 10분 전 → 아직
    assert is_due({"tier": "trade", "last_attempt": ago(40)}, "news", {}, now)
    assert not is_due({"tier": "trade", "last_attempt": ago(10)}, "news", {}, now)
    # 수집전용(120분): 40분 전이면 아직 — 매매 대상보다 드물게 본다
    assert not is_due({"tier": "collect", "last_attempt": ago(40)}, "news", {}, now)
    assert is_due({"tier": "collect", "last_attempt": ago(130)}, "news", {}, now)


def test_is_due_survives_bad_timestamp():
    now = datetime(2026, 7, 27, 10, 0, tzinfo=KST)
    assert is_due({"tier": "trade", "last_attempt": {"news": "쓰레기"}}, "news", {}, now)


def test_news_run_skips_not_due(monkeypatch):
    """계층 주기를 무시하고 매 사이클 전 종목을 부르면 잡는다."""
    runner = CollectRunner()
    monkeypatch.setattr(db, "ready", True)
    now = datetime.now(KST)
    fetched = []

    class FakeCol:
        name = "rss"

        def enabled(self, stock):
            return True

        async def fetch(self, stock, cursor):
            fetched.append(stock["ticker"])
            return [], cursor

    runner.collectors = {"news": [FakeCol()]}

    async def fake_active():
        return [
            {"id": 1, "ticker": "000001", "name": "A", "dart_corp_code": None,
             "last_collected_at": {}, "tier": "trade",
             "last_attempt": {"news": (now - timedelta(minutes=45)).isoformat()}},
            {"id": 2, "ticker": "000002", "name": "B", "dart_corp_code": None,
             "last_collected_at": {}, "tier": "collect",
             "last_attempt": {"news": (now - timedelta(minutes=45)).isoformat()}},
            {"id": 3, "ticker": "000003", "name": "C", "dart_corp_code": None,
             "last_collected_at": {}, "tier": "other",
             "last_attempt": {"news": (now - timedelta(minutes=45)).isoformat()}},
        ]

    marked = []

    async def fake_mark(tickers, source):
        marked.extend(tickers)

    monkeypatch.setattr(db, "active_watch_for_collect", fake_active)
    monkeypatch.setattr(db, "mark_attempt", fake_mark)
    monkeypatch.setitem(settings.COLLECT, "news", {})
    out = asyncio.run(runner.run_once("news"))
    assert fetched == ["000001"]              # 매매 대상만 (45분 > 30분)
    assert out["skipped_not_due"] == 2        # 수집전용·기타는 아직
    assert marked == ["000001"]               # 시도한 종목만 시각 기록


def test_config_wires_tiers_and_market_mode():
    dart_cfg = settings.COLLECT["dart"]
    assert dart_cfg["market_mode"] is True
    tiers = settings.COLLECT["news"]["tier_interval_min"]
    # 매매 대상이 가장 자주여야 한다 — 순서가 뒤집히면 잡는다
    assert tiers["trade"] < tiers["collect"] < tiers["other"]
