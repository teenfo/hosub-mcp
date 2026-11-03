"""주문번호는 그날 안에서만 유일하다 — 날짜를 넘는 충돌의 회귀 방지 (2026-11-03).

실측(mock): 주문번호가 날마다 다시 매겨져 날짜별 범위가 겹친다(10/28 0036524~0069848,
10/29 0042002~0072233). 원장 조회가 번호만 보던 때 청산 8건이 다른 날 **다른 종목**
체결가를 가져갔다 — 0066703 은 10/29 S-Oil(122,100)과 10/30 HMM(22,950)이 서로의
청산가를 바꿔 가졌다. 측정이 '모의 체결가 오염' 으로 빼던 8건이 전부 이것이었다.
"""
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.trade import fills, ledger, orders

KST = ZoneInfo("Asia/Seoul")
OLD_OPEN = "2026-10-29T10:00:00+09:00"
OLD_CLOSE = "2026-10-29T15:20:00+09:00"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(orders, "DB_PATH", tmp_path / "trading.db")
    return tmp_path


def _today() -> str:
    return datetime.now(KST).date().isoformat()


def _open(pid, symbol, ord_no, entry=10_000.0, qty=10):
    ledger.open_position({"id": pid, "symbol": symbol, "rule": "orb", "side": "long",
                          "qty": qty, "entry": entry, "stop": entry * 0.98,
                          "target": entry * 1.03}, fill=entry, ord_no=ord_no)


def _age(pid, opened=OLD_OPEN, closed=OLD_CLOSE):
    """지난 거래일의 포지션으로 만든다."""
    with ledger._conn() as conn:
        conn.execute("UPDATE positions SET opened=?, closed=? WHERE id=?",
                     (opened, closed if closed else None, pid))


def _row(pid):
    with ledger._conn() as conn:
        return dict(conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone())


def _exec(ord_no, code, side, price, qty=10, fee=5.0, tax=0.0):
    return {"ord_no": ord_no, "code": code, "name": code, "side": side,
            "price": price, "qty": qty, "commission": fee, "tax": tax}


def _ws_fill_row(ts, ord_no, symbol, price, qty):
    with ledger._conn() as conn:
        conn.execute("INSERT INTO exec_fills VALUES (?,?,?,?,?,?,?)",
                     (ts, ord_no, symbol, price, qty, "체결", 0))


# ── reconcile_executions(REST 체결내역 대사) ─────────────────────────────

def test_오늘_진입_체결은_같은_번호의_지난_포지션이_아니라_오늘_포지션에_붙는다():
    _open("old", "010950", "0066703", entry=121_500.0)
    ledger.close_position("old", 122_100.0, "target", ord_no="X0")
    _age("old")
    before = _row("old")
    _open("new", "011200", "0066703", entry=23_000.0)
    ledger.reconcile_executions([_exec("0066703", "011200", "buy", 22_950.0, fee=12)])
    new, old = _row("new"), _row("old")
    assert new["fill_confirmed"] == 1 and new["entry"] == 22_950.0
    assert new["entry_fee_krw"] == 12
    assert (old["entry"], old["entry_fee_krw"]) == (before["entry"], before["entry_fee_krw"])


def test_오늘_청산_체결은_같은_번호로_닫힌_지난_포지션에_붙지_않는다():
    _open("old", "010950", "E0", entry=121_500.0)
    ledger.close_position("old", 122_100.0, "target", ord_no="0066703")
    _age("old")
    _open("new", "011200", "E1", entry=23_500.0)
    ledger.close_position("new", 23_000.0, "stop", ord_no="0066703")
    ledger.reconcile_executions([_exec("E1", "011200", "buy", 23_500.0),
                                 _exec("0066703", "011200", "sell", 22_950.0)])
    assert _row("new")["exit"] == 22_950.0 and _row("new")["exit_fill_confirmed"] == 1
    assert _row("old")["exit"] == 122_100.0


def test_대사는_인자로_받은_날의_포지션만_본다():
    """마감 후 동기화가 날을 넘겨 돌아도 그 날 기준으로 붙인다."""
    _open("old", "010950", "0066703", entry=121_500.0)
    _age("old", closed=None)
    got = ledger.reconcile_executions([_exec("0066703", "010950", "buy", 121_400.0)],
                                      day="2026-10-29")
    assert got["entries"] == 1 and _row("old")["entry"] == 121_400.0


# ── record_fill(실시간 체결) ─────────────────────────────────────────────

def test_실시간_청산_체결이_지난_포지션의_청산가를_덮지_않는다():
    """10/30 HMM 체결이 10/29 S-Oil 의 청산가를 22,950 으로 덮던 경로."""
    _open("old", "010950", "E0", entry=121_500.0)
    ledger.close_position("old", 122_100.0, "target", ord_no="0066703")
    _age("old")
    _open("new", "011200", "E1", entry=23_500.0)
    ledger.close_position("new", 23_000.0, "stop", ord_no="0066703")
    ledger.record_fill({"ord_no": "0066703", "symbol": "011200", "price": 22_950.0,
                        "qty": 10, "state": "체결"})
    assert _row("old")["exit"] == 122_100.0
    assert _row("new")["exit"] == 22_950.0 and _row("new")["exit_fill_confirmed"] == 1


def test_실시간_진입_체결은_지난날_같은_번호의_오픈_포지션을_건드리지_않는다():
    _open("old", "035420", "0040224", entry=200_000.0)      # 외부·잔량처럼 날을 넘긴 오픈
    _age("old", closed=None)
    _open("new", "005930", "0040224", entry=70_000.0)
    assert ledger.record_fill({"ord_no": "0040224", "symbol": "005930",
                               "price": 70_100.0, "qty": 10}) is True
    assert _row("old")["entry"] == 200_000.0 and _row("new")["entry"] == 70_100.0


def test_실시간_청산_체결_종목이_다르면_가격은_두고_확정만_한다():
    _open("new", "011200", "E1", entry=23_500.0)
    ledger.close_position("new", 23_000.0, "stop", ord_no="B1")
    ledger.record_fill({"ord_no": "B1", "symbol": "114800", "price": 1_235.0, "qty": 10})
    r = _row("new")
    assert r["exit"] == 23_000.0 and r["exit_fill_confirmed"] == 1


# ── exec_fills 조회(fill_price_of·filled_qty) ───────────────────────────

def test_청산_기록은_지난날_같은_번호의_체결가를_쓰지_않는다():
    """10/30 HMM 포지션이 닫히며 10/29 S-Oil 체결가 122,100 을 '이미 도착한 체결' 로
    집어 확정 표시까지 하던 경로."""
    _ws_fill_row("2026-10-29T15:30:01+09:00", "0066703", "010950", 122_100.0, 10)
    _open("new", "011200", "E1", entry=23_500.0)
    ledger.close_position("new", 23_000.0, "stop", ord_no="0066703")
    r = _row("new")
    assert r["exit"] == 23_000.0 and r["exit_fill_confirmed"] == 0


def test_체결_수량은_그날_수신분만_센다():
    _ws_fill_row("2026-10-29T10:00:00+09:00", "X1", "021320", 5_500.0, 7)
    assert ledger.filled_qty("X1") == 0
    _ws_fill_row(datetime.now(KST).isoformat(timespec="seconds"), "X1", "021320",
                 5_500.0, 3)
    assert ledger.filled_qty("X1") == 3
    assert ledger.fill_price_of("X1") == 5_500.0


# ── sync_from_fills ③ 외부 매매 ─────────────────────────────────────────

def test_오늘_수동_매수는_지난_포지션과_번호가_같아도_외부로_잡힌다():
    _open("old", "005930", "0777777")
    ledger.close_position("old", 10_100.0, "target", ord_no="0777778")
    _age("old")
    day = _today()
    fills.store_today([_exec("0777777", "035420", "buy", 200_000.0, qty=3) | {"time": "100000"}],
                      day)
    got = fills.sync_from_fills(day, None, now=datetime.now(KST))
    assert got["external"] == 1


def test_재발주_청산_매도는_외부_왕복으로_읽지_않는다():
    """같은 날 수동 매수가 있어도 원장이 아는 재발주 매도(exit_links)는 외부 체결이 아니다."""
    day = _today()
    fills.store_today([_exec("0777777", "021320", "buy", 5_600.0, qty=5) | {"time": "100000"},
                       _exec("C1", "021320", "sell", 5_500.0, qty=5) | {"time": "100500"}],
                      day)
    got = fills.sync_from_fills(day, None, now=datetime.now(KST),
                                exit_links={"C1": "res1"})
    ext = [p for p in ledger.positions(status="open") if p["rule"] == "external"]
    assert got["external"] == 1 and ext and ext[0]["qty"] == 5
    assert got["external_closed"] == 0


def test_청산_주문번호_표도_그날_것만_묶는다():
    """orders.exit_order_links 는 원래 그날 범위였다 — 이번 정정의 기준선."""
    old = (datetime.now(UTC) - timedelta(days=5)).isoformat()
    with orders._conn() as conn:
        conn.execute(
            f"INSERT INTO orders ({orders._ENTRY_COLS}) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("o1", old, old, "021320", "long", "orb", "", 10_000, 9_800, 10_300, 10,
             "021320", "sell", 10, "sent", json.dumps({"ord_no": "0066703"}),
             "exit", "old", 10_000))
    assert orders.exit_order_links() == {}
