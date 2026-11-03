"""살아 있는 청산 매도와 잔량 편입 — 2026-11-03 실사고(mock)의 회귀 방지.

KCC건설 잔량 674주: 09:05 시장가(체결 0) → 09:06 잔량 시장가 재발주 → 09:06~09:19 에
걸쳐 173·264·402…496·674주로 조금씩 체결. 3분 유예가 지나자 아직 팔리는 중인 272주가
residual 로 다시 편입됐고(09:09:56), 그 행의 장중 매도는 '매도가능수량 부족' 으로 거부,
매도가 끝나자 놓친 청산(②)이 그 행을 원 포지션의 재발주 주문번호로 닫았다 — 원 포지션은
496주에서 멈추고 새 행(178주)이 674주 주문의 비용을 통째로 가져갔다.
"""
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import settings
from app.trade import fills, ledger, orders

KST = ZoneInfo("Asia/Seoul")
SYM = "021320"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(orders, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(settings, "CONFIG", {"execution": {"residual_adopt": True}})
    return tmp_path


def _today() -> str:
    return datetime.now(KST).date().isoformat()


def _residual(pid="res1", qty=674, entry=5_629.0):
    # 실제 편입 행은 손절·목표가 NULL 이지만 open_position 은 숫자를 요구한다 —
    # 여기서 재는 경로(편입·대사·놓친 청산)는 손절가를 보지 않는다.
    ledger.open_position({"id": pid, "symbol": SYM, "rule": "residual", "side": "long",
                          "qty": qty, "entry": entry, "stop": entry * 0.98,
                          "target": entry * 1.03}, fill=entry, ord_no=None)


def _exit_order(oid, link, ord_no, status="sent", created=None):
    created = created or datetime.now(UTC)
    with orders._conn() as conn:
        conn.execute(
            f"INSERT INTO orders ({orders._ENTRY_COLS}) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (oid, created.isoformat(), created.isoformat(), SYM, "long", "residual", "",
             5_629, None, None, 674, SYM, "sell", 674, status,
             json.dumps({"ord_no": ord_no, "return_code": 0}), "exit", link, 5_629))


def _sell(ord_no, qty, price=5_279.0, fee=None, tax=None):
    amt = price * qty
    return {"ord_no": ord_no, "code": SYM, "name": "KCC건설", "side": "sell",
            "price": price, "qty": qty, "time": "091900",
            "commission": round(amt * 0.00015) if fee is None else fee,
            "tax": round(amt * 0.0018) if tax is None else tax}


def _row(pid):
    with ledger._conn() as conn:
        r = conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()
    return dict(r) if r else None


def _sold_slowly(filled: int):
    """09:05 원 주문 B1(체결 0) + 09:06 재발주 C1(filled 주 체결) 상태를 만든다."""
    _residual()
    t0 = datetime.now(UTC) - timedelta(minutes=10)
    ledger.close_position("res1", 5_629.0, "residual", ord_no="B1")
    _exit_order("o1", "res1", "B1", created=t0)
    _exit_order("o2", "res1", "C1", created=t0 + timedelta(seconds=30))
    execs = [_sell("C1", filled)]
    ledger.reconcile_executions(execs, exit_links=orders.exit_order_links())
    return execs


def test_재발주가_아직_체결_중이면_그_종목은_진행_중인_청산이다():
    _sold_slowly(402)
    assert _row("res1")["exit_qty"] == 402
    assert orders.working_exit_symbols() == {SYM}


def test_진행_중인_청산의_남은_주식은_잔량으로_다시_편입하지_않는다():
    """09:09:56 의 재편입 — 3분 유예는 지났지만 매도가 살아 있다."""
    execs = _sold_slowly(402)
    day = _today()
    fills.store_today(execs, day)
    grace = orders.recent_exit_symbols() | orders.working_exit_symbols(day)
    got = fills.sync_from_fills(day, holdings={SYM: 272},
                                holding_info={SYM: {"avg_price": 5_629, "name": "KCC건설"}},
                                exit_grace=grace, exit_links=orders.exit_order_links(day))
    assert got["residual"] == 0
    assert [p for p in ledger.positions(status="open") if p["symbol"] == SYM] == []


def test_매도가_끝나면_원_포지션이_전량을_가져간다():
    _sold_slowly(402)
    day = _today()
    execs = [_sell("C1", 674)]
    fills.store_today(execs, day)
    links = orders.exit_order_links(day)
    ledger.reconcile_executions(execs, exit_links=links, day=day)
    r = _row("res1")
    assert r["exit_qty"] is None and r["exit"] == 5_279.0 and r["exit_fill_confirmed"] == 1
    assert orders.working_exit_symbols(day) == set()
    got = fills.sync_from_fills(day, holdings={SYM: 0}, exit_grace=set(), exit_links=links)
    assert got["missed_exit"] == 0 and got["residual"] == 0


def test_마지막_청산_주문이_거부면_진행_중이_아니다():
    """살아 있는 매도가 없으니 남은 주식은 고아 — 편입해 판다."""
    _residual()
    ledger.close_position("res1", 5_629.0, "residual", ord_no="B1")
    t0 = datetime.now(UTC) - timedelta(minutes=10)
    _exit_order("o1", "res1", "B1", created=t0)
    _exit_order("o2", "res1", "C1", status="rejected", created=t0 + timedelta(seconds=30))
    assert orders.working_exit_symbols() == set()


def test_체결이_아직_안_보여도_마지막_주문이_접수면_진행_중이다():
    _residual()
    ledger.close_position("res1", 5_629.0, "residual", ord_no="B1")
    _exit_order("o1", "res1", "B1")
    assert _row("res1")["exit_fill_confirmed"] == 0
    assert orders.working_exit_symbols() == {SYM}


def test_어제_닫힌_청산은_진행_중으로_보지_않는다():
    """마감 단일가 부분 체결의 잔량은 다음 날 09:00 에 편입돼야 한다."""
    _sold_slowly(44)
    with ledger._conn() as conn:
        conn.execute("UPDATE positions SET closed='2026-11-02T15:20:00+09:00' WHERE id='res1'")
    assert orders.working_exit_symbols() == set()


def test_다른_포지션의_재발주_매도로_놓친_청산을_닫지_않는다():
    """종전 코드가 만든 재편입 행(178주)이 남아 있어도 원 포지션의 매도를 가져가지 않고,
    보유가 0 이 되면 잔량 해소(void)로 정리된다 — 손익은 원 포지션 한 곳에만."""
    _sold_slowly(496)
    _residual("res2", qty=178)
    day = _today()
    execs = [_sell("C1", 674)]
    fills.store_today(execs, day)
    links = orders.exit_order_links(day)
    got = fills.sync_from_fills(day, holdings={SYM: 0}, exit_grace=set(), exit_links=links)
    assert got["missed_exit"] == 0 and got["residual_cleared"] == 1
    assert _row("res2")["status"] == "void"
    ledger.reconcile_executions(execs, exit_links=links, day=day)
    r = _row("res1")
    assert r["exit_qty"] is None and r["exit"] == 5_279.0


def test_원장이_모르는_매도면_놓친_청산으로_닫는다():
    """② 본래 용도는 그대로 — 주문 응답을 잃어 원장이 모르는 매도(번호 미상)."""
    _residual("res2", qty=178)
    day = _today()
    fills.store_today([_sell("Z9", 178, price=5_250.0)], day)
    got = fills.sync_from_fills(day, holdings={SYM: 0}, exit_grace=set(), exit_links={})
    assert got["missed_exit"] == 1
    r = _row("res2")
    assert r["status"] == "closed" and r["exit_ord_no"] == "Z9" and r["exit"] == 5_250.0


def test_자기_청산_주문의_매도면_놓친_청산으로_닫는다():
    """발주는 접수됐는데 원장 닫기가 실패한 포지션 — 주문 표가 그 포지션을 가리킨다."""
    _residual("res2", qty=178)
    day = _today()
    _exit_order("o9", "res2", "D1")
    fills.store_today([_sell("D1", 178)], day)
    got = fills.sync_from_fills(day, holdings={SYM: 0}, exit_grace=set(),
                                exit_links=orders.exit_order_links(day))
    assert got["missed_exit"] == 1 and _row("res2")["exit_ord_no"] == "D1"
