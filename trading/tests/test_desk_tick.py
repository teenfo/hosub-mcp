"""틱 드리븐 판정 — 즉시 청산·스로틀·루프와의 경쟁(원자적 잠금)."""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import settings
from app.trade import desk, ledger

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 7, 30, 10, 0, tzinfo=KST)      # 목요일 장중


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(desk, "STATE_FILE", tmp_path / "desk.json")
    monkeypatch.setitem(settings.CONFIG, "execution", {"stop_mode": "auto", "desk": {
        "enabled": True, "interval_sec": 2, "stale_sec": 5, "max_symbols": 0}})
    monkeypatch.setitem(settings.RISK, "auto_approve", True)
    monkeypatch.setattr(desk, "in_session", lambda now=None: True)
    desk._REST_CACHE.clear()
    desk._POS_CACHE["rows"] = {}
    desk._TICK_AT.clear()
    desk.STATE["degraded"] = False
    return tmp_path


def _open(pid="p1", symbol="005930"):
    ledger.open_position({"id": pid, "symbol": symbol, "entry": 10_000.0,
                          "stop": 9_800.0, "target": 10_400.0, "side": "long",
                          "qty": 10, "rule": "orb"}, fill=10_000.0, ord_no="0001")
    return ledger.positions(status="open")[0]


class _Exec:
    def __init__(self):
        self.calls = []

    async def __call__(self, pos, reason, px):
        self.calls.append((pos["symbol"], reason, px))
        return {"ok": True}


def test_틱_도착_즉시_청산이_나간다(env):
    pos = _open()
    ex = _Exec()
    desk._HOOKS.update(execute_exit=ex, propose_exit=None)
    desk._POS_CACHE["rows"] = {"005930": dict(pos)}
    asyncio.run(desk.on_tick("005930", 9_750.0))     # 손절선 아래 틱
    assert ex.calls and ex.calls[0][1] == "stop"
    assert desk.STATE["tick_judged"] >= 1
    # 잠긴 포지션은 캐시에서 빠져 같은 틱 경로가 반복 발주하지 않는다
    assert "005930" not in desk._POS_CACHE["rows"]


def test_스로틀_안에서는_판정하지_않는다(env):
    pos = _open()
    ex = _Exec()
    desk._HOOKS.update(execute_exit=ex, propose_exit=None)
    desk._POS_CACHE["rows"] = {"005930": dict(pos)}
    desk._TICK_AT.clear()
    asyncio.run(desk.on_tick("005930", 10_100.0))    # 판정만(청산선 아님)
    asyncio.run(desk.on_tick("005930", 9_750.0))     # 0.3초 안 — 스킵돼야 한다
    assert ex.calls == []


def test_보유가_아니면_아무_일도_없다(env):
    ex = _Exec()
    desk._HOOKS.update(execute_exit=ex, propose_exit=None)
    desk._POS_CACHE["rows"] = {}
    asyncio.run(desk.on_tick("000660", 9_000.0))
    assert ex.calls == [] and desk.STATE.get("degraded") is False


def test_루프와_틱이_경쟁해도_발주는_한_번(env):
    """claim_exit(원자적 UPDATE)이 같은 포지션의 이중 발주를 막는다."""
    pos = _open()
    ex = _Exec()
    stat = {"lines": 0, "proposed": 0, "exits": 0}
    # 두 경로가 같은 포지션 스냅샷을 들고 거의 동시에 판정하는 상황
    asyncio.run(desk._judge_one(dict(pos), 9_750.0, NOW, ex, None, stat))
    asyncio.run(desk._judge_one(dict(pos), 9_750.0, NOW, ex, None, stat))
    assert len(ex.calls) == 1
    assert stat["exits"] == 1


def test_외부_포지션은_틱_판정_대상이_아니다(env):
    ex = _Exec()
    desk._HOOKS.update(execute_exit=ex, propose_exit=None)
    desk._POS_CACHE["rows"] = {"035420": {"id": "x", "symbol": "035420",
                                          "side": "long", "stop": None}}
    asyncio.run(desk.on_tick("035420", 1.0))
    assert ex.calls == []
