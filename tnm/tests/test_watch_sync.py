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
    assert parse_trading_watchlist(payload) == {
        "005930": ("삼성전자", "collect"), "011200": ("HMM", "trade")}


def test_parse_holdings_variants():
    # 필드명이 달라도 파싱 (code/symbol/stk_cd, 'A' 접두 제거)
    assert parse_holdings({"stocks": [{"stk_cd": "A005930", "stk_nm": "삼성전자"}]}) \
        == {"005930": "삼성전자"}
    assert parse_holdings({"positions": [{"symbol": "011200"}]}) == {"011200": "011200"}
    assert parse_holdings({}) == {}


def test_merge_auto_holding_wins():
    auto = merge_auto({"005930": ("삼성전자", "trade"), "011200": ("HMM", "collect")},
                      {"005930": "삼성전자"})
    assert auto["005930"] == ("삼성전자", "holding", "trade")   # 보유가 우선
    assert auto["011200"] == ("HMM", "trading", "collect")


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


class _FakeCursor:
    def __init__(self, rows=None, rowcount=1):
        self._rows = rows or []
        self.rowcount = rowcount

    async def fetchall(self):
        return self._rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None


def _fake_pool(handler):
    class FakeConn:
        async def execute(self, sql, args=None):
            return handler(" ".join(sql.split()), args)

    class FakePool:
        def connection(self):
            class Ctx:
                async def __aenter__(self):
                    return FakeConn()

                async def __aexit__(self, *a):
                    return False
            return Ctx()
    return FakePool()


def _sync_handler(state, ops):
    def handler(s, args):
        low = s.lower()
        if low.startswith("select ticker, origin"):
            return _FakeCursor([(t, r["origin"], r["is_excluded"], r["is_active"])
                                for t, r in state.items()])
        if low.startswith("insert into tnm_watchlist"):
            ops["inserted"].append(args[0])
        elif low.startswith("update tnm_watchlist set last_seen_at"):
            # (name, origin, tier, ticker) — 경합 방어 WHERE 포함 UPDATE
            assert "not is_excluded" in low and "origin <> 'manual'" in low
            ops["updated"].append(args[3])
            ops["origins"].append((args[3], args[1]))
        elif "is_active = false" in low:
            assert "not is_excluded" in low
            ops["deactivated"].extend(args[0])
        return _FakeCursor()
    return handler


def test_apply_sync_merge_rules(monkeypatch):
    """병합 규칙: 신규 insert / 소멸 auto 비활성 / manual 불변 / excluded 유지 /
    재등장 auto 재활성(revived) / origin 전환(trading→holding)."""
    state = {
        "005930": {"origin": "trading", "is_excluded": False, "is_active": True},
        "011200": {"origin": "manual", "is_excluded": False, "is_active": True},
        "003490": {"origin": "trading", "is_excluded": True, "is_active": False},
        "047040": {"origin": "trading", "is_excluded": False, "is_active": True},
        "096770": {"origin": "holding", "is_excluded": False, "is_active": False},
    }
    ops = {"inserted": [], "updated": [], "origins": [], "deactivated": []}
    monkeypatch.setattr(db, "_pool", _fake_pool(_sync_handler(state, ops)))
    auto = {
        "005930": ("삼성전자", "holding"),   # 기존 auto → 갱신 + origin 전환
        "011200": ("HMM", "trading"),        # manual → 불변 (update 안 함)
        "003490": ("대한항공", "trading"),   # excluded → 되살리지 않음
        "068270": ("셀트리온", "trading"),   # 신규 → insert
        "096770": ("SK이노베이션", "holding"),  # 소멸했던 auto 재등장 → revived
        # 047040 은 auto 에 없음 → 비활성화 대상
    }
    result = asyncio.run(db.apply_sync(auto))
    assert ops["inserted"] == ["068270"]
    assert ops["updated"] == ["005930", "096770"]        # manual·excluded 는 미갱신
    assert ("005930", "holding") in ops["origins"]       # 매수 → origin 전환
    assert ops["deactivated"] == ["047040"]
    assert result["inserted"] == 1 and result["deactivated"] == 1
    assert result["revived"] == 1


def test_apply_sync_partial_failure_scopes_deactivation(monkeypatch):
    """계좌 조회만 실패(ok_origins={'trading'}) → holding 행은 소멸 판정에서 제외."""
    state = {
        "005930": {"origin": "holding", "is_excluded": False, "is_active": True},
        "047040": {"origin": "trading", "is_excluded": False, "is_active": True},
    }
    ops = {"inserted": [], "updated": [], "origins": [], "deactivated": []}
    monkeypatch.setattr(db, "_pool", _fake_pool(_sync_handler(state, ops)))
    # trading 감시목록만 성공 — holdings 는 못 가져온 상황
    result = asyncio.run(db.apply_sync({"011200": ("HMM", "trading")},
                                       ok_origins={"trading"}))
    assert "005930" not in ops["deactivated"]            # holding 행 유지 (핵심)
    assert ops["deactivated"] == ["047040"]              # trading 소멸 행만 비활성
    assert result["deactivated"] == 1


def test_run_once_account_soft_failure(monkeypatch):
    """trading 이 HTTP 200 + {ok:false} 로 계좌 실패를 알리면 holding 소멸 판정 제외."""
    ws = WatchSync()
    monkeypatch.setattr(db, "ready", True)

    class FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **k):
            if url.endswith("/api/watchlist"):
                return FakeResp({"entries": [{"code": "011200", "name": "HMM"}]})
            return FakeResp({"ok": False, "error": "키움 장애"})

    monkeypatch.setattr(watch.httpx, "AsyncClient", lambda **k: FakeClient())
    captured = {}

    async def fake_apply(auto, ok_origins=("trading", "holding")):
        captured["auto"] = auto
        captured["ok_origins"] = set(ok_origins)
        return {"inserted": 0, "revived": 0, "deactivated": 0, "total_auto": len(auto)}

    monkeypatch.setattr(db, "apply_sync", fake_apply)

    async def no_corp():
        return {}

    monkeypatch.setattr(watch.corp_codes, "mapping_for_missing", no_corp)
    asyncio.run(ws.run_once())
    assert captured["ok_origins"] == {"trading"}         # holding 은 판정 제외
    assert captured["auto"] == {"011200": ("HMM", "trading", "trade")}


def test_add_manual_conflict_contract(monkeypatch):
    """재등록 on-conflict 계약: 활성화·제외해제 + 비활성 행만 manual 승격."""
    captured = {}

    def handler(s, args):
        captured["sql"] = s.lower()
        captured["args"] = args
        # _WATCH_COLS 순서의 더미 행 (11개 컬럼)
        return _FakeCursor([(1, "005930", "삼성전자", None, 60, 5,
                             True, "trading", False, None, None)])

    monkeypatch.setattr(db, "_pool", _fake_pool(handler))
    row = asyncio.run(db.add_manual("005930", "삼성전자"))
    s = captured["sql"]
    assert "'manual'" in s                               # 신규 insert 는 manual
    conflict = s.split("on conflict", 1)[1].split("returning", 1)[0]
    assert "is_active = true" in conflict and "is_excluded = false" in conflict
    # 소스에 살아있는 auto 행은 origin 유지, 비활성 행만 manual 승격 (case 절)
    assert "case when tnm_watchlist.is_active" in conflict
    assert row["origin"] == "trading" and row["is_active"] is True


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
