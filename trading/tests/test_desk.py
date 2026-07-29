"""매매 데스크 — 고속 청산 감시 루프.

가장 중요한 테스트는 첫 번째다: **WS 값이 있으면 REST 를 부르지 않는다.**
이 설계의 값이 거기서 나온다 — 주기를 15배 올리면서 API 콜은 안 늘리는 것.
콜이 늘면 분리한 의미가 없고, 발굴 예산을 뺏어 원래 문제로 돌아간다.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import settings
from app.trade import desk, ledger

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 7, 30, 10, 0, tzinfo=KST)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(settings, "COSTS", {"commission_pct": 0.015,
                                            "sell_tax_pct": 0.20,
                                            "slippage_bp": 5})
    monkeypatch.setitem(settings.CONFIG, "execution", {"desk": {
        "enabled": True, "interval_sec": 2, "stale_sec": 5, "max_symbols": 5}})
    return tmp_path


def _open(oid="p1", symbol="005930", entry=10_000, stop=9_800, target=10_400, qty=10):
    ledger.open_position({"id": oid, "symbol": symbol, "side": "long",
                          "entry": entry, "stop": stop, "target": target,
                          "rule": "orb", "qty": qty, "name": symbol}, fill=entry)


class _Spy:
    """주입 3종을 기록하는 스파이."""

    def __init__(self, ws=None, rest=None, exit_ok=True):
        self.ws, self.rest_map, self.exit_ok = ws or {}, rest or {}, exit_ok
        self.rest_calls, self.exits = [], []

    def fresh(self, symbol):
        return self.ws.get(symbol)

    async def rest(self, symbol):
        self.rest_calls.append(symbol)
        return self.rest_map.get(symbol)

    async def execute(self, pos, reason, px):
        self.exits.append((pos["id"], reason, px))
        if self.exit_ok:
            ledger.close_position(pos["id"], px, reason)
        return {"ok": self.exit_ok}


# --------------------------------------------------------------------------
# 호출 정책 — 이 설계의 핵심
# --------------------------------------------------------------------------
async def test_WS_값이_있으면_REST_를_부르지_않는다(env):
    _open()
    spy = _Spy(ws={"005930": 10_100})
    stat = await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)
    assert spy.rest_calls == [], "WS 가 살아 있으면 추가 호출이 0이어야 한다"
    assert stat["ws"] == 1 and stat["rest"] == 0


async def test_WS_값이_낡으면_그_종목만_REST_로_보충한다(env):
    _open("p1", "005930")
    _open("p2", "000660", entry=20_000, stop=19_600, target=20_800)
    spy = _Spy(ws={"005930": 10_100}, rest={"000660": 20_100})   # 000660 만 낡음
    stat = await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)
    assert spy.rest_calls == ["000660"], "낡은 종목만 보충해야 한다"
    assert stat["ws"] == 1 and stat["rest"] == 1


async def test_가격을_모르면_판정하지_않는다(env):
    _open()
    spy = _Spy()                       # WS 없음 · REST 도 None
    stat = await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)
    assert stat["no_price"] == 1
    assert spy.exits == [], "가격을 모르는 채로 청산하면 안 된다"


async def test_보유가_max_symbols_를_넘으면_초과분은_건드리지_않는다(env, monkeypatch):
    monkeypatch.setitem(settings.CONFIG, "execution",
                        {"desk": {"enabled": True, "max_symbols": 2}})
    for i in range(4):
        _open(f"p{i}", f"00000{i}")
    spy = _Spy(ws={f"00000{i}": 10_100 for i in range(4)})
    stat = await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)
    assert stat["watched"] == 2, "초과분은 기존 30초 루프가 맡는다"


# --------------------------------------------------------------------------
# 청산 판정
# --------------------------------------------------------------------------
async def test_손절선에_닿으면_청산한다(env):
    _open()
    spy = _Spy(ws={"005930": 9_790})
    stat = await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)
    assert stat["exits"] == 1
    assert spy.exits == [("p1", "stop", 9_800.0)]


async def test_목표가에_닿으면_청산한다(env):
    _open()
    spy = _Spy(ws={"005930": 10_450})
    await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)
    assert spy.exits == [("p1", "target", 10_400.0)]


async def test_중간이면_아무것도_하지_않는다(env):
    _open()
    spy = _Spy(ws={"005930": 10_100})
    stat = await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)
    assert stat["exits"] == 0 and spy.exits == []


async def test_같은_포지션에_청산이_두_번_나가지_않는다(env):
    """2초 루프에서는 발주 왕복 안에 다음 사이클이 온다."""
    _open()
    spy = _Spy(ws={"005930": 9_790}, exit_ok=True)

    # 발주는 하되 원장을 닫지 않는 상황(체결 대기) — exit_pending 만으로 막아야 한다
    async def _execute(pos, reason, px):
        spy.exits.append((pos["id"], reason, px))
        return {"ok": True}

    await desk.tick(spy.fresh, spy.rest, _execute, now=NOW)
    await desk.tick(spy.fresh, spy.rest, _execute, now=NOW)
    assert len(spy.exits) == 1, "exit_pending 이 두 번째 발주를 막아야 한다"


async def test_발주_실패면_잠금을_풀어_다음_사이클에_다시_본다(env):
    _open()
    spy = _Spy(ws={"005930": 9_790}, exit_ok=False)
    await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)
    await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)
    assert len(spy.exits) == 2, "실패했으면 다시 시도해야 한다"


# --------------------------------------------------------------------------
# 라인 갱신 — 훅은 있고 규칙은 아직 없다
# --------------------------------------------------------------------------
async def test_기본_훅은_라인을_바꾸지_않는다(env):
    """규칙이 없는 동안 매매 동작은 지금과 같아야 한다."""
    _open()
    spy = _Spy(ws={"005930": 10_100})
    stat = await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)
    assert stat["lines"] == 0
    with ledger._conn() as conn:
        r = conn.execute("SELECT stop_live, target_live FROM positions").fetchone()
    assert r["stop_live"] is None and r["target_live"] is None


async def test_갱신된_라인으로_판정한다(env, monkeypatch):
    _open()
    monkeypatch.setattr(desk, "update_lines", lambda pos, price: (10_000.0, None))
    spy = _Spy(ws={"005930": 9_990})       # 원본 손절 9,800 → 아직 안 닿음
    stat = await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)
    assert stat["lines"] == 1
    assert spy.exits == [("p1", "stop", 10_000.0)], "갱신된 손절선이 판정에 쓰여야 한다"


def test_원본_손절선은_보존된다(env):
    _open()
    ledger.set_lines("p1", 10_000.0, None, now=NOW)
    with ledger._conn() as conn:
        r = conn.execute("SELECT stop, stop_live FROM positions").fetchone()
    assert r["stop"] == 9_800, "되돌릴 기준이 남아야 한다"
    assert r["stop_live"] == 10_000.0


def test_값이_안_바뀌면_쓰지_않는다(env):
    """초 단위 루프에서 매번 UPDATE 하면 SQLite 쓰기가 그만큼 잦아진다."""
    _open()
    assert ledger.set_lines("p1", 10_000.0, None, now=NOW) is True
    assert ledger.set_lines("p1", 10_000.0, None, now=NOW) is False


def test_live_가_없으면_기존_손절선을_쓴다(env):
    """데스크가 꺼져 있거나 아직 갱신 안 한 포지션은 기존 동작 그대로."""
    _open()
    assert ledger.due_exits(lambda _s: 9_790)[0]["reason"] == "stop"
    assert ledger.due_exits(lambda _s: 9_850) == []


def test_due_exits_도_갱신된_라인을_본다(env):
    """기존 30초 루프와 데스크가 같은 값으로 판정해야 한다 — 어긋나면 이중 판정이다."""
    _open()
    ledger.set_lines("p1", 10_000.0, None, now=NOW)
    assert ledger.due_exits(lambda _s: 9_990)[0]["reason"] == "stop"


# --------------------------------------------------------------------------
# 안전장치
# --------------------------------------------------------------------------
def test_꺼져_있으면_비활성이다(env, monkeypatch):
    monkeypatch.setitem(settings.CONFIG, "execution", {"desk": {"enabled": False}})
    assert desk.enabled() is False


async def test_계측이_쌓여_화면에_노출된다(env):
    """WS 로 몇 건 · REST 로 몇 건. 이 비율이 늘면 분리한 의미가 사라지는 중이다."""
    before = dict(desk.STATE)
    _open("p1", "005930")
    _open("p2", "000660", entry=20_000, stop=19_600, target=20_800)
    spy = _Spy(ws={"005930": 10_100}, rest={"000660": 20_100})
    await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)

    st = desk.status()
    assert st["ws"] - before["ws"] == 1 and st["rest"] - before["rest"] == 1
    assert st["cycles"] - before["cycles"] == 1
    assert st["last_tick"] == "10:00:00"
    assert st["enabled"] is True and st["interval_sec"] == 2


def test_장중에만_돈다():
    assert desk.in_session(datetime(2026, 7, 30, 10, 0, tzinfo=KST)) is True
    assert desk.in_session(datetime(2026, 7, 30, 15, 45, tzinfo=KST)) is False
    assert desk.in_session(datetime(2026, 8, 1, 10, 0, tzinfo=KST)) is False   # 토


async def test_청산_대기중인_포지션은_건너뛴다(env):
    _open()
    ledger.set_exit_pending("p1", 1)
    spy = _Spy(ws={"005930": 9_790})
    stat = await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)
    assert stat["watched"] == 0 and spy.exits == []


async def test_void_포지션은_대상이_아니다(env):
    _open()
    ledger.void_position("p1", "미체결")
    spy = _Spy(ws={"005930": 9_790})
    stat = await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)
    assert stat["watched"] == 0


# --------------------------------------------------------------------------
# 신선도 판정 (BarAggregator)
# --------------------------------------------------------------------------
async def test_틱이_오면_신선하고_오래되면_None(monkeypatch):
    from app.data.collector import BarAggregator

    agg = BarAggregator()
    monkeypatch.setattr("app.data.collector.store.upsert_bars", lambda *a, **k: None)
    t = [1000.0]
    monkeypatch.setattr("app.data.collector.time.monotonic", lambda: t[0])

    await agg.on_tick("005930", 10_100.0, 5, "100000")
    assert agg.fresh_price("005930", 5) == 10_100.0

    t[0] = 1004.0
    assert agg.fresh_price("005930", 5) == 10_100.0    # 4초 — 아직 신선
    t[0] = 1006.0
    assert agg.fresh_price("005930", 5) is None        # 6초 — 못 믿는다
    assert agg.snapshot("005930") is not None, "봉 자체는 남아 있다(차트용)"


def test_한_번도_못_받은_종목은_None():
    from app.data.collector import BarAggregator

    agg = BarAggregator()
    assert agg.age_sec("005930") is None
    assert agg.fresh_price("005930", 5) is None
