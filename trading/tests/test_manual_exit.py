"""수동 청산·고아 제외 — 2026-07-29 실거래 결함의 회귀 방지.

`POST /api/positions/{id}/close` 는 **주문을 내지 않고 원장만 닫았다**. 화면 버튼은
'청산' 이라고 보이는데 계좌에는 종목이 남아, 원장과 계좌가 어긋난 고아가 생겼다.
그날 고아 종목이 만들어진 원인 중 하나다.

가장 중요한 테스트는 첫 두 개다 — **실제 매도가 나가는가**, **발주 실패 시 원장이
닫히지 않는가**. 둘째가 없으면 첫째를 고쳐도 실패 경로로 같은 결함이 남는다.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import settings
from app.trade import ledger, orders

KST = ZoneInfo("Asia/Seoul")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(orders, "DB_PATH", tmp_path / "orders.db", raising=False)
    monkeypatch.setattr(settings, "COSTS", {"commission_pct": 0.015,
                                            "sell_tax_pct": 0.20,
                                            "slippage_bp": 5})
    return tmp_path


def _open(oid="p1", symbol="005930", qty=10, entry=10_000):
    ledger.open_position({"id": oid, "symbol": symbol, "side": "long",
                          "entry": entry, "stop": entry * 0.98,
                          "target": entry * 1.04, "rule": "orb", "qty": qty,
                          "name": symbol}, fill=entry)


def _status(oid="p1"):
    with ledger._conn() as conn:
        r = conn.execute("SELECT status, exit_ord_no FROM positions WHERE id=?",
                         (oid,)).fetchone()
    return (r["status"], r["exit_ord_no"])


# --------------------------------------------------------------------------
# 청산 — 실제 매도가 나가야 한다
# --------------------------------------------------------------------------
async def test_수동_청산이_실제_매도를_발주한다(env, monkeypatch):
    """이번 버그의 회귀 테스트 — 주문 없이 원장만 닫으면 안 된다."""
    sent = []

    async def _order(side, symbol, qty, price=0):
        sent.append((side, symbol, qty, price))
        return {"ord_no": "0999001", "return_code": 0}

    from app.kiwoom import client as client_mod
    monkeypatch.setattr(client_mod.client, "order", _order)

    _open()
    r = await orders.execute_exit(ledger.positions("open")[0], "manual", 10_100)
    assert r["ok"] is True
    assert sent == [("sell", "005930", 10, 0)], "시장가 매도가 나가야 한다"
    st, ord_no = _status()
    assert st == "closed"
    assert ord_no == "0999001", "청산 주문번호가 원장에 남아야 실측 정정이 가능하다"


async def test_발주가_실패하면_원장이_닫히지_않는다(env, monkeypatch):
    """계좌에는 남았는데 원장만 닫히는 상황을 막는다 — 고아의 직접 원인이다."""
    async def _order(side, symbol, qty, price=0):
        raise RuntimeError("주문 거부")

    from app.kiwoom import client as client_mod
    monkeypatch.setattr(client_mod.client, "order", _order)

    _open()
    r = await orders.execute_exit(ledger.positions("open")[0], "manual", 10_100)
    assert r["ok"] is False
    assert _status()[0] == "open", "발주 실패면 포지션이 열린 채로 남아야 한다"
    assert len(ledger.positions("open")) == 1


# --------------------------------------------------------------------------
# 제외 — 주문 없이 원장만 정리한다
# --------------------------------------------------------------------------
def test_제외는_주문을_내지_않고_void_만_한다(env, monkeypatch):
    called = []

    async def _order(*a, **k):
        called.append(a)
        return {}

    from app.kiwoom import client as client_mod
    monkeypatch.setattr(client_mod.client, "order", _order)

    _open()
    assert ledger.void_position("p1", "수동 제외") is True
    assert called == [], "제외는 주문 경로를 타면 안 된다"
    assert _status()[0] == "void"
    assert ledger.positions("open") == []


def test_void_된_포지션은_청산_대상이_아니다(env):
    _open()
    ledger.void_position("p1", "수동 제외")
    assert ledger.due_exits(lambda _s: 9_000) == []


def test_void_는_실현손익에서_빠진다(env):
    _open("p1", qty=10, entry=10_000)
    _open("p2", symbol="000660", qty=5, entry=20_000)
    ledger.close_position("p1", 9_800, "manual")
    ledger.close_position("p2", 21_000, "target")
    before = ledger.realized_today(500_000)
    ledger.void_position("p1", "수동 제외")
    after = ledger.realized_today(500_000)
    assert after["trades"] == before["trades"] - 1
    assert after["krw"] > before["krw"]


# --------------------------------------------------------------------------
# 일지 — 집계 전에 대사한다
# --------------------------------------------------------------------------
async def test_일지가_집계_전에_대사를_돌린다(env, monkeypatch, tmp_path):
    from app import journal

    monkeypatch.setattr(journal, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(journal, "JOURNAL_DIR", tmp_path / "journal")
    monkeypatch.setattr(settings, "KIWOOM_APP_KEY", "k")
    order = []

    async def _execs():
        order.append("reconcile")
        return {}

    def _build(day=None):
        order.append("build")
        return {"date": "2026-07-29", "facts": [], "trades": {"trades": 0},
                "positions": [], "carried": [], "summary": {}}

    from app.kiwoom import client as client_mod
    monkeypatch.setattr(client_mod.client, "executions", _execs)
    monkeypatch.setattr(journal, "build", _build)

    await journal.generate("2026-07-29", with_summary=False)
    assert order == ["reconcile", "build"], "대사가 집계보다 먼저여야 한다"


async def test_대사_실패해도_일지는_만들되_사실에_남긴다(env, monkeypatch, tmp_path):
    from app import journal

    monkeypatch.setattr(journal, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(journal, "JOURNAL_DIR", tmp_path / "journal")
    monkeypatch.setattr(settings, "KIWOOM_APP_KEY", "k")

    async def _execs():
        raise RuntimeError("조회 실패")

    from app.kiwoom import client as client_mod
    monkeypatch.setattr(client_mod.client, "executions", _execs)

    entry = await journal.generate("2026-07-29", with_summary=False)
    assert entry["date"] == "2026-07-29"
    assert any("대사에 실패" in f for f in entry["facts"]), \
        "모델 비용 기준임을 조용히 넘기면 안 된다"


# --------------------------------------------------------------------------
# 2026-07-29 실측 — 단일가 대기 중 계좌에서 주문을 취소한 종목에 청산을 눌렀다
# --------------------------------------------------------------------------
def test_거부된_청산은_원장을_닫지_않는다(tmp_path, monkeypatch):
    """키움은 거부도 HTTP 200 으로 준다(return_code 20).

    HTTP 성공만 보고 닫으면 **계좌에 남은 채 원장만 닫히는 진짜 고아**가 된다.
    """
    import asyncio

    from app.kiwoom.client import client
    from app.trade import ledger, orders

    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(orders, "DB_PATH", tmp_path / "t.db")
    ledger.open_position({"id": "p1", "symbol": "007810", "side": "long", "qty": 2,
                          "entry": 42_700, "stop": 41_000, "target": 44_000,
                          "rule": "orb"}, fill=42_700)

    async def rejected(*a, **k):
        return {"return_code": 20,
                "return_msg": "[2000](800033:매도가능수량이 부족합니다. 0주 매도가능)"}

    monkeypatch.setattr(client, "order", rejected)
    got = asyncio.run(orders.execute_exit(
        ledger.positions(status="open")[0], "manual", 42_200))

    assert got["ok"] is False and got["status"] == "rejected"
    assert "매도가능수량" in got["message"]
    assert ledger.positions(status="open"), "거부됐으면 포지션이 열린 채여야 한다"


def test_수락된_청산은_원장을_닫는다(tmp_path, monkeypatch):
    """거부 판정이 정상 경로까지 막으면 안 된다."""
    import asyncio

    from app.kiwoom.client import client
    from app.trade import ledger, orders

    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(orders, "DB_PATH", tmp_path / "t.db")
    ledger.open_position({"id": "p1", "symbol": "007810", "side": "long", "qty": 2,
                          "entry": 42_700, "stop": 41_000, "target": 44_000,
                          "rule": "orb"}, fill=42_700)

    async def ok(*a, **k):
        return {"return_code": 0, "ord_no": "0702299"}

    monkeypatch.setattr(client, "order", ok)
    got = asyncio.run(orders.execute_exit(
        ledger.positions(status="open")[0], "manual", 42_200))
    assert got["ok"] is True
    assert ledger.positions(status="open") == []


def test_accepted_는_return_code_로_판정한다():
    from app.trade import orders

    assert orders.accepted({"return_code": 0}) is True
    assert orders.accepted({"return_code": "0"}) is True
    assert orders.accepted({}) is True             # 코드가 없으면 성공 취급(구버전)
    assert orders.accepted({"return_code": 20}) is False
