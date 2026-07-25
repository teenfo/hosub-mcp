"""M1 완료판정 — 관심종목 동기화: 응답 파싱·병합 규칙(신규/소멸/manual 불변/excluded 유지)."""
import asyncio

from app import db, settings, watch
from app.watch import WatchSync, merge_auto, parse_holdings, parse_trading_watchlist


def test_parse_trading_watchlist():
    payload = {"entries": [
        {"code": "005930", "name": "삼성전자", "collect_only": 1},
        {"code": "011200", "name": "HMM"},
        {"code": "", "name": "무시"},
    ]}
    assert parse_trading_watchlist(payload) == {"005930": "삼성전자", "011200": "HMM"}


def test_parse_holdings_variants():
    # 필드명이 달라도 파싱 (code/symbol/stk_cd, 'A' 접두 제거)
    assert parse_holdings({"stocks": [{"stk_cd": "A005930", "stk_nm": "삼성전자"}]}) \
        == {"005930": "삼성전자"}
    assert parse_holdings({"positions": [{"symbol": "011200"}]}) == {"011200": "011200"}
    assert parse_holdings({}) == {}


def test_merge_auto_holding_wins():
    auto = merge_auto({"005930": "삼성전자", "011200": "HMM"}, {"005930": "삼성전자"})
    assert auto["005930"] == ("삼성전자", "holding")   # 보유가 우선
    assert auto["011200"] == ("HMM", "trading")


def test_sync_skips_when_trading_down(monkeypatch):
    """trading 완전 다운 → 목록 변경 없이 skip (graceful)."""
    ws = WatchSync()
    monkeypatch.setattr(db, "ready", True)

    class Boom:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            raise OSError("connection refused")

    monkeypatch.setattr(watch.httpx, "AsyncClient", lambda **k: Boom())
    applied = []

    async def fake_apply(auto):
        applied.append(auto)
        return {}

    monkeypatch.setattr(db, "apply_sync", fake_apply)
    result = asyncio.run(ws.run_once())
    assert "skipped" in result and applied == []      # apply_sync 미호출


def test_sync_requires_db(monkeypatch):
    ws = WatchSync()
    monkeypatch.setattr(db, "ready", False)
    result = asyncio.run(ws.run_once())
    assert result == {"skipped": "DB 미준비"}


def test_apply_sync_merge_rules(monkeypatch):
    """병합 규칙: 신규 insert / 소멸 auto 비활성 / manual 불변 / excluded 유지."""
    state = {
        "005930": {"origin": "trading", "is_excluded": False, "is_active": True},
        "011200": {"origin": "manual", "is_excluded": False, "is_active": True},
        "003490": {"origin": "trading", "is_excluded": True, "is_active": False},
        "047040": {"origin": "trading", "is_excluded": False, "is_active": True},
    }
    ops = {"inserted": [], "updated": [], "deactivated": []}

    class FakeCursor:
        def __init__(self, rows=None, rowcount=1):
            self._rows = rows or []
            self.rowcount = rowcount

        async def fetchall(self):
            return self._rows

        async def fetchone(self):
            return self._rows[0] if self._rows else None

    class FakeConn:
        async def execute(self, sql, args=None):
            s = " ".join(sql.split()).lower()
            if s.startswith("select ticker, origin"):
                return FakeCursor([(t, r["origin"], r["is_excluded"], r["is_active"])
                                   for t, r in state.items()])
            if s.startswith("insert into tnm_watchlist"):
                ops["inserted"].append(args[0])
            elif s.startswith("update tnm_watchlist set last_seen_at"):
                ops["updated"].append(args[1])
            elif "is_active = false" in s:
                ops["deactivated"].extend(args[0])
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
    auto = {
        "005930": ("삼성전자", "holding"),   # 기존 auto → 갱신
        "011200": ("HMM", "trading"),        # manual → 불변 (update 안 함)
        "003490": ("대한항공", "trading"),   # excluded → 되살리지 않음
        "068270": ("셀트리온", "trading"),   # 신규 → insert
        # 047040 은 auto 에 없음 → 비활성화 대상
    }
    result = asyncio.run(db.apply_sync(auto))
    assert ops["inserted"] == ["068270"]
    assert ops["updated"] == ["005930"]                  # manual·excluded 는 미갱신
    assert ops["deactivated"] == ["047040"]
    assert result["inserted"] == 1 and result["deactivated"] == 1


def test_corp_code_xml_parse(tmp_path):
    from app.corp_codes import parse_corpcode_xml
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <result>
      <list><corp_code>00126380</corp_code><corp_name>삼성전자</corp_name>
            <stock_code>005930</stock_code></list>
      <list><corp_code>00999999</corp_code><corp_name>비상장사</corp_name>
            <stock_code> </stock_code></list>
    </result>""".encode("utf-8")
    assert parse_corpcode_xml(xml) == {"005930": "00126380"}
