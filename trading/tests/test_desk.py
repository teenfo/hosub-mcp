"""매매 데스크 — 고속 청산 감시 루프.

가장 중요한 테스트는 첫 번째다: **WS 값이 있으면 REST 를 부르지 않는다.**
이 설계의 값이 거기서 나온다 — 주기를 15배 올리면서 API 콜은 안 늘리는 것.
콜이 늘면 분리한 의미가 없고, 발굴 예산을 뺏어 원래 문제로 돌아간다.
"""
import asyncio
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
    monkeypatch.setattr(desk, "STATE_FILE", tmp_path / "desk.json")
    monkeypatch.setattr(settings, "COSTS", {"commission_pct": 0.015,
                                            "sell_tax_pct": 0.20,
                                            "slippage_bp": 5})
    monkeypatch.setitem(settings.CONFIG, "execution", {"stop_mode": "auto", "desk": {
        "enabled": True, "interval_sec": 2, "stale_sec": 5, "max_symbols": 0}})
    # 승인 규약은 명시한다 — 기본값에 기대면 config 가 바뀔 때 조용히 뒤집힌다
    monkeypatch.setitem(settings.RISK, "auto_approve", True)
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


async def test_기본값은_보유_전량을_본다(env):
    """데스크의 목적은 **보유 종목 전부**를 초 단위로 보는 것이다.

    상한을 걸면 그 초과분이 2초 감시 밖으로 나간다 — 수동 매수 등으로 보유가
    늘었을 때 하필 그 종목만 30초로 떨어진다.
    """
    for i in range(8):
        _open(f"p{i}", f"00000{i}")
    spy = _Spy(ws={f"00000{i}": 10_100 for i in range(8)})
    stat = await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)
    assert stat["watched"] == 8


async def test_상한을_두면_그만큼만_본다(env, monkeypatch):
    """레이트리밋이 문제가 될 때를 위한 안전장치. 기본값은 아니다."""
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


async def test_목표가에_닿으면_청산한다(env, monkeypatch):
    """추종을 끈 경우다. **켜져 있으면 목표가가 늘 달아나 닿지 않는다** —
    아래 `test_목표가는_늘_현재가_위로_달아난다` 참조."""
    monkeypatch.setitem(settings.CONFIG, "execution",
                        {"desk": {"enabled": True, "trailing": False}})
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


def test_켜지면_손절_목표는_30초_루프에서_빠진다(env):
    """`_ledger_loop` 이 `desk.owns()` 로 거른다 — 소유권을 하나로 둔다."""
    assert desk.owns("stop") is True and desk.owns("target") is True
    assert desk.owns("timeout") is False, "시각 기준이라 데스크로 넘기지 않는다"
    assert desk.owns("eod") is False


def test_꺼지면_손절_목표는_승인_대기로_올라간다(env):
    """사용자 결정(2026-07-29) — 자동 발주 대신 사람이 보고 정한다.

    되돌리기의 실체이기도 하다: 끄는 순간 **감시가 비지는 않는다.** 도달 사실이
    승인 대기로 올라오므로 놓치지 않고, 발주 여부만 사람 손으로 넘어간다.
    """
    desk.set_state(enabled=False)
    assert desk.exit_route("stop") == "approve"
    assert desk.exit_route("target") == "approve"
    assert desk.exit_route("timeout") == "auto", "시간 손절은 기존 규약 그대로다"
    assert desk.exit_route("eod") == "auto"


def test_켜지면_손절_목표는_데스크_몫이다(env):
    assert desk.exit_route("stop") == "skip"
    assert desk.exit_route("target") == "skip"
    assert desk.exit_route("timeout") == "auto"


def test_상한_때문에_못_보는_동안에는_소유권을_가져가지_않는다(env, monkeypatch):
    """겹치는 것보다 **비는 것**이 훨씬 위험하다.

    상한이 걸리면 초과분은 데스크가 건너뛴다. 그때도 소유권을 주장하면 그 종목은
    데스크도 30초 루프도 보지 않는다 — 손절이 아무도 없는 채로 남는다.
    """
    monkeypatch.setitem(settings.CONFIG, "execution",
                        {"desk": {"enabled": True, "max_symbols": 2}})
    _open("p1", "005930")
    _open("p2", "000660")
    assert desk.owns("stop") is True, "상한 안이면 데스크가 다 본다"

    _open("p3", "035720")                  # 상한 초과
    assert desk.owns("stop") is False, "못 보는 동안에는 30초 루프가 받아야 한다"
    assert desk.exit_route("stop") == "auto", \
        "구멍에서는 보수적인 쪽이 자동 청산이다 — 승인 대기로 미루면 손절이 늦는다"


async def test_데스크가_직접_발주한다(env):
    """판정만 빠르고 발주가 30초 루프를 타면 빨라진 의미가 없다.

    같은 사이클 안에서 `execute_exit` 이 불려야 한다 — 그것이 이 분리의 목적이다.
    """
    _open()
    spy = _Spy(ws={"005930": 9_790})
    await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)
    assert spy.exits == [("p1", "stop", 9_800.0)]
    assert ledger.positions(status="open") == [], "같은 사이클에 원장이 닫혀야 한다"


# --------------------------------------------------------------------------
# 승인 규약 — 데스크를 켜는 것은 주기를 바꾸는 것이지 규칙을 바꾸는 게 아니다
# --------------------------------------------------------------------------
async def test_승인_모드에서는_목표_청산을_승인_대기로_올린다(env, monkeypatch):
    """이게 갈리면 데스크를 켜는 순간 목표 청산이 조용히 자동 발주로 바뀐다."""
    monkeypatch.setitem(settings.RISK, "auto_approve", False)
    monkeypatch.setitem(settings.CONFIG, "execution",     # 추종 켜면 목표가 달아난다
                        {"desk": {"enabled": True, "trailing": False}})
    _open()
    proposed = []
    spy = _Spy(ws={"005930": 10_450})          # 목표 도달
    stat = await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW,
                           propose_exit=lambda p, r, px: proposed.append((p["id"], r, px)))
    assert spy.exits == [], "목표는 항상 승인이다"
    assert proposed == [("p1", "target", 10_400.0)] and stat["proposed"] == 1


async def test_승인_모드여도_손절은_stop_mode_를_따른다(env, monkeypatch):
    """손절은 계좌 보호다 — 30초 루프와 같은 규약."""
    monkeypatch.setitem(settings.RISK, "auto_approve", False)
    monkeypatch.setitem(settings.CONFIG, "execution",
                        {"stop_mode": "auto", "desk": {"enabled": True}})
    _open()
    spy = _Spy(ws={"005930": 9_790})
    await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW,
                    propose_exit=lambda *a: None)
    assert spy.exits == [("p1", "stop", 9_800.0)]


async def test_stop_mode_가_approve_면_손절도_승인_대기다(env, monkeypatch):
    monkeypatch.setitem(settings.RISK, "auto_approve", False)
    monkeypatch.setitem(settings.CONFIG, "execution",
                        {"stop_mode": "approve", "desk": {"enabled": True}})
    _open()
    proposed = []
    spy = _Spy(ws={"005930": 9_790})
    await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW,
                    propose_exit=lambda p, r, px: proposed.append((p["id"], r, px)))
    assert spy.exits == [] and proposed == [("p1", "stop", 9_800.0)]


# --------------------------------------------------------------------------
# 라인 갱신 — 훅은 있고 규칙은 아직 없다
# --------------------------------------------------------------------------
async def test_추종을_끄면_진입_시_고정값_그대로다(env, monkeypatch):
    monkeypatch.setitem(settings.CONFIG, "execution",
                        {"desk": {"enabled": True, "trailing": False}})
    _open()
    spy = _Spy(ws={"005930": 10_100})
    stat = await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)
    assert stat["lines"] == 0
    with ledger._conn() as conn:
        r = conn.execute("SELECT stop_live, target_live FROM positions").fetchone()
    assert r["stop_live"] is None and r["target_live"] is None


# --------------------------------------------------------------------------
# 라인 추종 — 사용자 지침 2026-07-29 (+ 같은 날 보강)
#   ① 오르면 같은 갭으로 따라 올리고, 내려도 그 자리에 둔다
#   ② 목표까지 50% 를 오면 손절 간격을 목표갭의 20% 로 좁힌다
#   ③ 익절선은 진입가 +3% 를 넘지 않는다 (이게 있어야 목표에 닿는다)
# 실거래 손익이 여기서 갈린다. 규칙을 값으로 못박는다.
#
# 기준 포지션: 진입 10,000 · 손절 9,800(갭 200) · 목표 10,400(목표갭 400)
#   → 좁히기 발동 10,200(50%) · 좁힌 간격 80(20%) · 익절 상한 10,300(+3%)
# --------------------------------------------------------------------------
def _lines(oid="p1"):
    with ledger._conn() as conn:
        r = conn.execute("SELECT stop_live, target_live FROM positions WHERE id=?",
                         (oid,)).fetchone()
    return (r["stop_live"], r["target_live"])


async def test_발동_전에는_원래_갭으로_따라_올린다(env):
    _open()
    spy = _Spy(ws={"005930": 10_150})           # 50% 미만
    stat = await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)
    assert stat["lines"] == 1
    assert _lines() == (9_950.0, 10_300.0)      # 10,150−200 · 목표는 상한


async def test_목표의_절반을_오면_손절_간격이_좁아진다(env):
    """이익 구간에서는 되밀림을 덜 허용해 확정분을 키운다."""
    _open()
    spy = _Spy(ws={"005930": 10_200})           # 정확히 50%
    await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)
    assert _lines()[0] == 10_120.0, "간격이 200 → 80 으로 좁아진다"


async def test_가격이_내려도_라인은_그_자리에_남는다(env):
    """이 한 줄이 '이득 보전' 의 실체다."""
    _open()
    spy = _Spy(ws={"005930": 10_250})
    await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)
    assert _lines()[0] == 10_170.0

    spy.ws["005930"] = 10_150                   # 되밀림
    stat = await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)
    assert stat["lines"] == 0, "안 바뀌면 쓰지도 않는다"
    assert _lines()[0] == 10_170.0


async def test_손절선은_한_번도_내려가지_않는다(env):
    _open()
    spy = _Spy(ws={"005930": 10_290})
    await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)
    high = _lines()
    for px in (10_250, 10_230, 10_215):         # 계속 밀려도
        spy.ws["005930"] = px
        await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)
        assert _lines() == high


async def test_진입가_아래에서는_손절선이_원래_자리에_있다(env):
    """물려 있는 동안 손절선이 따라 내려가면 손실이 커진다."""
    _open()
    spy = _Spy(ws={"005930": 9_900})
    await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)
    assert _lines()[0] is None, "손절선은 안 움직인다(목표만 상한에 걸린다)"
    assert ledger.due_exits(lambda _s: 9_850) == [], "원래 손절 9,800 그대로다"


async def test_따라_올라간_손절선에_닿으면_청산한다(env):
    """추종의 목적 — 되밀릴 때 반납분만 내주고 나온다."""
    _open()
    spy = _Spy(ws={"005930": 10_290})
    await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)   # 손절선 10,210
    assert spy.exits == []

    spy.ws["005930"] = 10_200
    await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)
    assert spy.exits == [("p1", "stop", 10_210.0)], "원래 손절 9,800 이 아니다"


async def test_익절선은_진입가_3퍼센트를_넘지_않는다(env):
    """상한이 없으면 목표가가 늘 달아나 **익절이 영원히 안 된다.**"""
    _open()
    spy = _Spy(ws={"005930": 10_299})
    await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)
    assert _lines()[1] == 10_300.0, "10,299+400 이 아니라 상한에서 멈춘다"


async def test_상한에_닿으면_익절_청산이_일어난다(env):
    """조정 지침의 목적 — 원래 룰에서는 이 청산이 불가능했다."""
    _open()
    spy = _Spy(ws={"005930": 10_300})
    await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)
    assert spy.exits == [("p1", "target", 10_300.0)]


async def test_상한은_원래_목표가보다_낮아도_적용된다(env):
    """규칙에 따라 목표가 3% 를 넘게 잡힌다(target_r 1.5 × 손절 2.5% = 3.75%).

    '하향은 없다' 는 추종 방향에 대한 것이고, 상한은 그와 별개로 걸어 둔
    이익 확정선이다.
    """
    ledger.open_position({"id": "p9", "symbol": "000660", "side": "long",
                          "entry": 10_000, "stop": 9_750, "target": 10_375,
                          "rule": "orb", "qty": 3, "name": "000660"}, fill=10_000)
    spy = _Spy(ws={"000660": 10_050})
    await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)
    assert _lines("p9")[1] == 10_300.0


async def test_숏은_방향을_뒤집는다(env):
    """숏에서 유리한 움직임은 하락이다. 문구대로 올리면 손실이 커진다."""
    ledger.open_position({"id": "s1", "symbol": "034220", "side": "short",
                          "entry": 10_000, "stop": 10_200, "target": 9_600,
                          "rule": "orb", "qty": 5, "name": "034220"}, fill=10_000)
    spy = _Spy(ws={"034220": 9_850})            # 50% 미발동(9,800 이 발동선)
    await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)
    assert _lines("s1") == (10_050.0, 9_700.0), "손절선이 내려와야 한다"

    spy.ws["034220"] = 9_900                    # 되밀림
    await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)
    assert _lines("s1") == (10_050.0, 9_700.0)


async def test_숏도_절반을_오면_간격이_좁아지고_상한이_걸린다(env):
    ledger.open_position({"id": "s2", "symbol": "034220", "side": "short",
                          "entry": 10_000, "stop": 10_200, "target": 9_600,
                          "rule": "orb", "qty": 5, "name": "034220"}, fill=10_000)
    spy = _Spy(ws={"034220": 9_800})            # 정확히 50%
    await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)
    assert _lines("s2") == (9_880.0, 9_700.0)   # 9,800+80 · 상한 −3%


def test_원본_손절선은_추종_뒤에도_남는다(env):
    """되돌릴 기준이자 일지·백테스트가 읽는 값이다."""
    _open()
    assert desk.update_lines(ledger.positions(status="open")[0], 10_250) \
        == (10_170.0, 10_300.0)
    with ledger._conn() as conn:
        r = conn.execute("SELECT stop, target FROM positions").fetchone()
    assert (r["stop"], r["target"]) == (9_800, 10_400)


# --------------------------------------------------------------------------
# 시간 손절 — 손절선이 움직이면 다시 센다
# --------------------------------------------------------------------------
async def test_손절선이_움직이면_시간_손절_시계가_다시_돈다(env, monkeypatch):
    """손절선이 올라갔다는 것은 방향이 아직 맞다는 실측이다.

    그 자리를 진입 시각 기준 시계로 자르면 이기고 있는 자리를 닫는다.
    """
    monkeypatch.setitem(settings.RULES, "max_hold_min", 30)
    _open()
    with ledger._conn() as conn:                # 진입한 지 29분 됐다고 두고
        conn.execute("UPDATE positions SET opened=?",
                     ((NOW - timedelta(minutes=29)).isoformat(timespec="seconds"),))
    later = NOW + timedelta(minutes=5)          # → 34분. 원래라면 시간 손절이다
    assert ledger.due_exits(lambda _s: 10_100, now=later)[0]["reason"] == "timeout"

    spy = _Spy(ws={"005930": 10_250})           # 손절선이 움직인다
    await desk.tick(spy.fresh, spy.rest, spy.execute, now=NOW)
    assert ledger.due_exits(lambda _s: 10_250, now=later) == [], \
        "손절선이 움직인 시각부터 다시 센다"


def test_손절선이_안_움직이면_원래_시계_그대로다(env, monkeypatch):
    """진입 후 값이 안 나온 자리는 자를 근거가 그대로 남아 있다."""
    monkeypatch.setitem(settings.RULES, "max_hold_min", 30)
    _open()
    with ledger._conn() as conn:
        conn.execute("UPDATE positions SET opened=?",
                     ((NOW - timedelta(minutes=31)).isoformat(timespec="seconds"),))
    assert ledger.due_exits(lambda _s: 10_000, now=NOW)[0]["reason"] == "timeout"


def test_목표가만_바뀌면_시계는_그대로다(env):
    """보유시간을 늘리는 근거는 손절선이 움직였다는 사실이다."""
    _open()
    ledger.set_lines("p1", None, 10_300.0, now=NOW)
    with ledger._conn() as conn:
        r = conn.execute("SELECT opened, stop_moved FROM positions").fetchone()
    assert r["stop_moved"] is None
    assert ledger.hold_since(r) == r["opened"]


def test_갭이_없으면_건드리지_않는다(env):
    """손절선이 진입가와 같은 이상한 행에서 라인을 0으로 밀지 않는다."""
    pos = {"side": "long", "entry": 10_000, "stop": 10_000, "target": 10_000,
           "stop_live": None, "target_live": None}
    assert desk.update_lines(pos, 10_500) is None


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


# --------------------------------------------------------------------------
# 런타임 오버라이드 — 배포 없이 끌 수 있어야 한다
# --------------------------------------------------------------------------
def test_런타임_오버라이드가_config_를_이긴다(env):
    """초 단위로 실거래 청산을 내는 루프다 — 배포를 기다려서 끌 수는 없다."""
    assert desk.enabled() is True            # config 는 켜짐
    desk.set_state(enabled=False)
    assert desk.enabled() is False
    desk.set_state(enabled=True)
    assert desk.enabled() is True


def test_오버라이드는_준_값만_바꾼다(env):
    desk.set_state(interval_sec=3)
    assert desk.enabled() is True, "건드리지 않은 값은 config 를 따른다"
    assert desk._num("interval_sec") == 3.0


def test_주기는_하한_아래로_못_내린다(env):
    """0초 주기는 이벤트 루프를 굶긴다."""
    desk.set_state(interval_sec=0)
    assert desk._num("interval_sec") == 0.5


def test_오버라이드_파일이_깨져도_기본값으로_돈다(env):
    (env / "desk.json").write_text("{ 깨진 json")
    assert desk.enabled() is True            # config 값으로 폴백


async def test_끄고_켜는_것이_재시작_없이_먹힌다(env, monkeypatch):
    """루프는 항상 떠 있고 **매 사이클 설정을 다시 읽는다** — 그래서 즉시 반영된다.

    장중 여부는 고정한다. 실제 시각에 따라 결과가 달라지면 판정이 성립하지 않는다.
    """
    monkeypatch.setattr(desk, "in_session", lambda now=None: True)
    monkeypatch.setitem(settings.CONFIG, "execution",
                        {"desk": {"enabled": True, "interval_sec": 0.5}})
    _open()
    spy = _Spy(ws={"005930": 9_790})
    task = asyncio.create_task(desk.loop(spy.fresh, spy.rest, spy.execute,
                                         lambda: True))
    try:
        desk.set_state(enabled=False)
        await asyncio.sleep(0.2)
        assert spy.exits == [], "꺼진 동안에는 청산이 나가면 안 된다"

        desk.set_state(enabled=True)
        for _ in range(30):                      # 다음 사이클을 기다린다
            await asyncio.sleep(0.05)
            if spy.exits:
                break
        assert spy.exits, "다시 켜면 재시작 없이 돌아야 한다"
    finally:
        task.cancel()


def test_status_가_오버라이드를_드러낸다(env):
    """화면이 '설정값과 다르게 돌고 있다' 를 말할 수 있어야 한다."""
    assert desk.status()["override"] == {}
    desk.set_state(enabled=False)
    assert desk.status()["override"] == {"enabled": False}
    assert desk.status()["enabled"] is False


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
