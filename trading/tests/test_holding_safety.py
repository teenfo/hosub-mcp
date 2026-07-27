"""자동 교체가 실거래를 해치지 않게 하는 방어선.

2026-07-27 발견. 감시목록의 source별 전량 교체(`replace_scanned`)가 보유 여부를
보지 않아, 보유 종목이 순위에서 밀리면 감시목록에서 삭제됐다. 그러면:

  감시목록 이탈 → WS 구독 해제 → aggregator.snapshot() = None
                → engine._collect_targets() 에서 제외(장중 분봉 백필 중단)
                → 청산 감시(30초)가 ledger.latest_price 로 폴백
                → 그 값은 이탈 종목 수집 루프(15분 주기)로만 갱신

즉 **손절·목표 판정이 최대 15분 묵은 가격으로** 돈다. 실거래 손실로 직결된다.
여기 있는 것은 기능 테스트가 아니라 그 회귀를 막는 방어선이다.
"""
import pytest

from app import settings
from app.data import watchlist
from app.trade import ledger


@pytest.fixture
def wl(tmp_path, monkeypatch):
    monkeypatch.setattr(watchlist, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(settings, "WATCHLIST", {})
    monkeypatch.setattr(settings, "COLLECT_ONLY", set())
    return tmp_path


def _held(monkeypatch, codes):
    monkeypatch.setattr(ledger, "open_symbols", lambda: set(codes))


def _codes(source=None):
    return {e["code"] for e in watchlist.entries()
            if source is None or e["source"] == source}


# --- ① 보유 종목은 순위에서 밀려도 남는다 ---

def test_replace_scanned_keeps_held_symbol(wl, monkeypatch):
    _held(monkeypatch, set())
    watchlist.replace_active([{"code": "000660", "name": "SK하이닉스"},
                              {"code": "005930", "name": "삼성전자"}])
    assert _codes("active") == {"000660", "005930"}

    # 005930 을 매수해 보유 중인 상태에서, 다음 스캔 결과에서 빠졌다
    _held(monkeypatch, {"005930"})
    watchlist.replace_active([{"code": "000660", "name": "SK하이닉스"}])
    assert "005930" in _codes(), "보유 종목이 감시목록에서 사라졌다 — 손절 감시가 묵은 가격으로 돈다"
    assert "005930" in settings.WATCHLIST      # 런타임 집합에도 남아야 WS·백필 대상


def test_replace_scanned_drops_unheld_symbol(wl, monkeypatch):
    """보유하지 않은 종목은 종전대로 교체된다 — 보호가 과하면 목록이 안 줄어든다."""
    _held(monkeypatch, set())
    watchlist.replace_active([{"code": "000660", "name": "A"},
                              {"code": "005930", "name": "B"}])
    watchlist.replace_active([{"code": "000660", "name": "A"}])
    assert _codes("active") == {"000660"}


def test_replace_scanned_empty_picks_keeps_held(wl, monkeypatch):
    """조회 실패로 빈 목록이 와도 보유 종목은 지켜야 한다.
    (scanner 는 '성공한 스캔은 빈 결과라도 반영' 규약이라 이 경로가 실제로 열린다)"""
    _held(monkeypatch, set())
    watchlist.replace_active([{"code": "000660", "name": "A"},
                              {"code": "005930", "name": "B"}])
    _held(monkeypatch, {"000660"})
    watchlist.replace_active([])
    assert _codes() == {"000660"}


def test_replace_auto_keeps_held_symbol(wl, monkeypatch):
    """야간 발굴 교체 경로도 같은 결함을 갖고 있었다."""
    _held(monkeypatch, set())
    watchlist.replace_auto([{"code": "000660", "name": "A"},
                            {"code": "005930", "name": "B"}])
    _held(monkeypatch, {"005930"})
    watchlist.replace_auto([{"code": "000660", "name": "A"}])
    assert "005930" in _codes()


def test_replace_auto_empty_picks_keeps_held(wl, monkeypatch):
    _held(monkeypatch, set())
    watchlist.replace_auto([{"code": "005930", "name": "B"}])
    _held(monkeypatch, {"005930"})
    watchlist.replace_auto([])
    assert "005930" in _codes()


def test_manual_and_seed_still_untouched(wl, monkeypatch):
    """기존 규약(다른 소스는 건드리지 않음)이 깨지지 않았는지."""
    _held(monkeypatch, set())
    watchlist.add("005930", "삼성전자", source="manual")
    watchlist.replace_active([{"code": "000660", "name": "A"}])
    watchlist.replace_active([])
    rows = {e["code"]: e["source"] for e in watchlist.entries()}
    assert rows == {"005930": "manual"}


def test_ledger_failure_does_not_wipe_watchlist(wl, monkeypatch):
    """보유 조회가 실패하면 아무것도 지우지 않는다 — 모르는 상태에서 삭제 금지."""
    _held(monkeypatch, set())
    watchlist.replace_active([{"code": "000660", "name": "A"},
                              {"code": "005930", "name": "B"}])

    def boom():
        raise RuntimeError("DB 잠김")

    monkeypatch.setattr(ledger, "open_symbols", boom)
    watchlist.replace_active([])
    assert _codes() == {"000660", "005930"}


# --- ② WS 재구독은 집합이 바뀔 때만 ---

@pytest.mark.asyncio
async def test_ws_update_noop_when_unchanged():
    """재접속은 소켓을 닫고 LOGIN+REG 를 다시 하는 동안 틱을 잃는다.
    스캐너가 60초마다 같은 결과로 알림을 띄우므로 no-op 이 없으면 계속 샌다."""
    from app.kiwoom.ws import RealtimeFeed

    feed = RealtimeFeed(on_tick=None)
    closed = []

    class FakeWS:
        async def close(self):
            closed.append(1)

    feed._symbols = {"000660", "005930"}
    feed._ws = FakeWS()
    await feed.update(["005930", "000660"])       # 순서만 다른 같은 집합
    assert closed == [], "집합이 같은데 재접속했다"

    await feed.update(["005930"])                 # 실제로 바뀌면 재접속
    assert closed == [1]
    assert feed._symbols == {"005930"}


# --- ③ 마감 백필이 이탈 종목까지 덮는다 ---

@pytest.mark.asyncio
async def test_eod_backfill_covers_dropped_symbols(monkeypatch):
    """15:30 직전에 빠진 종목의 그날 분봉이 확정되지 않으면 백테스트에 구멍이 난다."""
    from app.data import roster
    from app.signals import engine as engine_mod
    from app.signals.engine import SignalEngine

    monkeypatch.setattr(settings, "WATCHLIST", {"000001": "남은 종목"})
    monkeypatch.setattr(settings, "COLLECT_ONLY", set())
    monkeypatch.setitem(settings.CONFIG, "collection",
                        {"deep_backfill": False, "roster_retention_days": 30})
    monkeypatch.setattr(roster, "active", lambda days: {"000002": "빠진 종목"})
    called = []

    async def fake(sym):
        called.append(sym)

    monkeypatch.setattr(engine_mod.collector, "backfill_minutes", fake)
    assert await SignalEngine().eod_backfill_once() == 2
    assert set(called) == {"000001", "000002"}


@pytest.mark.asyncio
async def test_eod_backfill_survives_roster_failure(monkeypatch):
    """로스터 조회가 실패해도 감시목록 마감 확정은 계속된다."""
    from app.data import roster
    from app.signals import engine as engine_mod
    from app.signals.engine import SignalEngine

    monkeypatch.setattr(settings, "WATCHLIST", {"000001": "a"})
    monkeypatch.setattr(settings, "COLLECT_ONLY", set())
    monkeypatch.setitem(settings.CONFIG, "collection", {"deep_backfill": False})

    def boom(days):
        raise RuntimeError("로스터 오류")

    monkeypatch.setattr(roster, "active", boom)
    called = []

    async def fake(sym):
        called.append(sym)

    monkeypatch.setattr(engine_mod.collector, "backfill_minutes", fake)
    assert await SignalEngine().eod_backfill_once() == 1
    assert called == ["000001"]


@pytest.mark.asyncio
async def test_eod_backfill_no_duplicate_calls(monkeypatch):
    """감시목록과 로스터에 겹치는 종목을 두 번 부르지 않는다(레이트리밋 낭비)."""
    from app.data import roster
    from app.signals import engine as engine_mod
    from app.signals.engine import SignalEngine

    monkeypatch.setattr(settings, "WATCHLIST", {"000001": "a", "000002": "b"})
    monkeypatch.setattr(settings, "COLLECT_ONLY", set())
    monkeypatch.setitem(settings.CONFIG, "collection", {"deep_backfill": False})
    monkeypatch.setattr(roster, "active", lambda days: {"000002": "b", "000003": "c"})
    called = []

    async def fake(sym):
        called.append(sym)

    monkeypatch.setattr(engine_mod.collector, "backfill_minutes", fake)
    await SignalEngine().eod_backfill_once()
    assert sorted(called) == ["000001", "000002", "000003"]
