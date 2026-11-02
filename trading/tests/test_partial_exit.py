"""부분 체결 청산의 장부 정정 — 2026-09-29~10-26 실사고의 회귀 방지.

마감 단일가 시장가가 하루 100~250주씩만 체결됐는데 원장은 접수 시점에 **전량**으로
닫고 다음 날 잔량을 다시 편입했다. KCC건설 하나에 장부상 42,349주 '매도' vs 실제
2,453주. 고친 것: 원 청산 + 잔량 재발주의 체결을 포지션 단위로 합치고, 판 수량이
포지션 수량보다 적으면 `exit_qty` 에 남겨 손익을 그 수량으로 낸다.
"""
import json
from datetime import UTC, datetime

import pytest

from app.trade import ledger, orders


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(orders, "DB_PATH", tmp_path / "trading.db")
    return tmp_path


def _pos(pid="p1", qty=10, entry=10_000.0, ord_no="A1"):
    ledger.open_position({"id": pid, "symbol": "021320", "rule": "residual",
                          "side": "long", "qty": qty, "entry": entry,
                          "stop": entry * 0.98, "target": entry * 1.03},
                         fill=entry, ord_no=ord_no)


def _fill(ord_no, price, qty, side="sell", fee=0.0, tax=0.0):
    return {"ord_no": ord_no, "code": "021320", "name": "KCC건설", "side": side,
            "price": price, "qty": qty, "commission": fee, "tax": tax}


def _row(pid="p1"):
    with ledger._conn() as conn:
        return dict(conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone())


def test_부분_체결이면_판_수량만_실현한다():
    """원장 10주 청산 접수 → 4주만 체결: exit_qty=4, 손익도 4주 몫."""
    _pos()
    ledger.reconcile_executions([_fill("A1", 10_000, 10, side="buy", fee=15)])
    ledger.close_position("p1", 10_200, "eod", ord_no="B1")
    ledger.reconcile_executions([_fill("A1", 10_000, 10, side="buy", fee=15),
                                 _fill("B1", 10_200, 4, fee=6, tax=80)])
    r = _row()
    assert r["qty"] == 10, "의도(진입) 수량은 그대로 둔다"
    assert r["exit_qty"] == 4
    # (10,200 − 10,000) × 4 − 진입 비용 15×0.4 − 청산 6 − 세금 80 = 708
    assert r["pnl_krw"] == pytest.approx(800 - 6 - 6 - 80)


def test_전량_체결이면_exit_qty_는_비어_있다():
    _pos()
    ledger.close_position("p1", 10_200, "stop", ord_no="B1")
    ledger.reconcile_executions([_fill("A1", 10_000, 10, side="buy"),
                                 _fill("B1", 10_200, 10)])
    assert _row()["exit_qty"] is None


def test_잔량_재발주_체결도_같은_포지션의_매도로_합친다():
    """실측 2026-09-29: 원 주문 취소 → 재발주분만 15:30 체결. 원 주문번호만 보면
    매도가 통째로 빠진다."""
    _pos()
    ledger.close_position("p1", 10_000, "eod", ord_no="B1")
    ledger.reconcile_executions([_fill("A1", 10_000, 10, side="buy"),
                                 _fill("C1", 10_300, 10)],
                                exit_links={"C1": "p1"})
    r = _row()
    assert r["exit"] == 10_300 and r["exit_qty"] is None
    assert r["exit_fill_confirmed"] == 1


def test_원_주문과_재발주를_수량가중으로_합친다():
    _pos()
    ledger.close_position("p1", 10_000, "stop", ord_no="B1")
    ledger.reconcile_executions([_fill("A1", 10_000, 10, side="buy"),
                                 _fill("B1", 10_100, 3), _fill("C1", 10_400, 5)],
                                exit_links={"B1": "p1", "C1": "p1"})
    r = _row()
    assert r["exit_qty"] == 8
    assert r["exit"] == pytest.approx((10_100 * 3 + 10_400 * 5) / 8, abs=0.01)


def test_뒤이은_재발주_체결이_오면_판_수량을_다시_늘린다():
    """qty 를 덮어쓰지 않고 exit_qty 를 따로 두는 이유 — 같은 날 다시 늘어날 수 있다."""
    _pos()
    ledger.close_position("p1", 10_000, "stop", ord_no="B1")
    ledger.reconcile_executions([_fill("A1", 10_000, 10, side="buy"), _fill("B1", 10_100, 3)])
    assert _row()["exit_qty"] == 3
    ledger.reconcile_executions([_fill("A1", 10_000, 10, side="buy"), _fill("B1", 10_100, 3),
                                 _fill("C1", 10_200, 7)], exit_links={"C1": "p1"})
    assert _row()["exit_qty"] is None


def test_체결이_안_보이는_청산은_건드리지_않는다():
    """ka10076 은 한 페이지라 '안 보임' 을 '미체결' 로 읽으면 오판한다."""
    _pos()
    ledger.close_position("p1", 10_200, "eod", ord_no="B1")
    before = _row()
    ledger.reconcile_executions([_fill("A1", 10_000, 10, side="buy")])
    after = _row()
    assert after["exit_fill_confirmed"] == 0 and after["exit_qty"] is None
    assert after["exit"] == before["exit"]


def test_청산_주문번호_표는_그날_나간_청산만_묶는다():
    now = datetime.now(UTC)
    with orders._conn() as conn:
        for oid, link, ord_no, status, kind in (
                ("o1", "p1", "B1", "sent", "exit"),
                ("o2", "p1", "C1", "sent", "exit"),
                ("o3", "p2", "D1", "rejected", "exit"),      # 거부 — 체결 없음
                ("o4", None, "E1", "sent", "entry")):        # 진입 — 대상 아님
            conn.execute(
                f"INSERT INTO orders ({orders._ENTRY_COLS}) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (oid, now.isoformat(), now.isoformat(), "021320", "long", "orb", "",
                 10_000, 9_800, 10_300, 10, "021320", "sell", 10, status,
                 json.dumps({"ord_no": ord_no, "return_code": 0}), kind, link, 10_000))
    assert orders.exit_order_links() == {"B1": "p1", "C1": "p1"}
