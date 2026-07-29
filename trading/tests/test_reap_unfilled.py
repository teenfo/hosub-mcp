"""미체결 포지션 회수 — 2026-07-29 실거래 결함의 회귀 방지.

키움 REST 가 주문번호를 주면 원장은 곧바로 포지션을 연다. 거래소가 그 뒤에 주문을
거부해도 되돌리는 경로가 없어서, 현물 계좌 개별주 공매도 2건이 체결 없이 포지션으로
남았고 −1,025원이 실현손익·일일 손실 가드에 들어갔다.

가장 중요한 테스트는 **오탐 쪽**이다 — 같은 날 고려산업은 `fill_confirmed=0` 이었지만
계좌에 52주가 실재했다. 원장만 보고 지웠으면 실제 보유 종목의 청산 감시가 사라진다.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import settings
from app.trade import ledger

KST = ZoneInfo("Asia/Seoul")
T0 = datetime(2026, 7, 29, 9, 33, tzinfo=KST)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(settings, "COSTS", {"commission_pct": 0.015,
                                            "sell_tax_pct": 0.20,
                                            "slippage_bp": 5})
    return tmp_path


def _open(oid, symbol, *, qty=10, entry=10_000, opened=T0, confirmed=False,
          side="long", name=None):
    ledger.open_position({"id": oid, "symbol": symbol, "side": side,
                          "entry": entry, "stop": entry * 0.98,
                          "target": entry * 1.04, "rule": "orb", "qty": qty,
                          "name": name or symbol}, fill=entry)
    with ledger._conn() as conn:
        conn.execute("UPDATE positions SET opened=?, fill_confirmed=? WHERE id=?",
                     (opened.isoformat(timespec="seconds"), 1 if confirmed else 0, oid))


def _status(oid):
    with ledger._conn() as conn:
        r = conn.execute("SELECT status, exit_reason FROM positions WHERE id=?",
                         (oid,)).fetchone()
    return (r["status"], r["exit_reason"])


LATER = T0 + timedelta(minutes=10)


# --------------------------------------------------------------------------
# 회수 판정
# --------------------------------------------------------------------------
def test_계좌에_없고_유예가_지나면_회수한다(db):
    _open("p1", "034220")
    reaped = ledger.reap_unfilled({}, now=LATER)
    assert [r["id"] for r in reaped] == ["p1"]
    st, why = _status("p1")
    assert st == "void"
    assert "미체결" in why


def test_계좌에_있으면_미체결이어도_건드리지_않는다(db):
    """고려산업 회귀 — fill_confirmed=0 이지만 실제로 보유 중인 경우."""
    _open("p1", "002140", qty=52)
    assert ledger.reap_unfilled({"002140": 52}, now=LATER) == []
    assert _status("p1")[0] == "open"


def test_유예_전에는_건드리지_않는다(db):
    _open("p1", "034220")
    soon = T0 + timedelta(minutes=2)
    assert ledger.reap_unfilled({}, now=soon) == []
    assert _status("p1")[0] == "open"


def test_체결_확인된_포지션은_대상이_아니다(db):
    _open("p1", "034220", confirmed=True)
    assert ledger.reap_unfilled({}, now=LATER) == []
    assert _status("p1")[0] == "open"


def test_계좌_조회_실패면_아무것도_하지_않는다(db):
    """None = '모름'. 조회 실패로 전 포지션을 지우는 것이 훨씬 위험하다."""
    _open("p1", "034220")
    assert ledger.reap_unfilled(None, now=LATER) == []
    assert _status("p1")[0] == "open"


def test_보유수량_0이면_유령이다(db):
    _open("p1", "034220")
    assert len(ledger.reap_unfilled({"034220": 0}, now=LATER)) == 1
    assert _status("p1")[0] == "void"


def test_회수는_멱등이다(db):
    _open("p1", "034220")
    assert len(ledger.reap_unfilled({}, now=LATER)) == 1
    assert ledger.reap_unfilled({}, now=LATER) == []


def test_여러_건_중_유령만_고른다(db):
    _open("p1", "034220")                      # 유령
    _open("p2", "002140", qty=52)              # 실재
    _open("p3", "010140")                      # 유령
    reaped = {r["id"] for r in ledger.reap_unfilled({"002140": 52}, now=LATER)}
    assert reaped == {"p1", "p3"}
    assert _status("p2")[0] == "open"


# --------------------------------------------------------------------------
# void 가 집계에서 빠지는가 — 이것이 이 기능의 목적이다
# --------------------------------------------------------------------------
def test_void_는_실현손익에서_빠진다(db):
    _open("p1", "034220", qty=11, entry=9_020)
    _open("p2", "011200", qty=5, entry=20_500, confirmed=True)
    ledger.close_position("p1", 9_030, "manual")
    ledger.close_position("p2", 21_000, "target")

    before = ledger.realized_today(500_000)
    assert before["trades"] == 2

    # 청산된 뒤에도 계좌에 없으면 유령이다 — 1회성 정정 경로
    assert ledger.void_position("p1", "미체결")
    after = ledger.realized_today(500_000)
    assert after["trades"] == 1
    assert after["krw"] > before["krw"], "유령 손실이 빠지면 실현손익이 개선된다"


def test_void_는_기본_조회와_통계에서_빠진다(db):
    _open("p1", "034220")
    _open("p2", "011200", confirmed=True)
    ledger.reap_unfilled({"011200": 10}, now=LATER)

    assert {p["id"] for p in ledger.positions()} == {"p2"}
    assert {p["id"] for p in ledger.positions("open")} == {"p2"}
    assert ledger.open_symbols() == {"011200"}
    assert ledger.open_count() == 1
    assert {p["id"] for p in ledger.positions("void")} == {"p1"}


def test_void_는_청산_대상에서_빠진다(db):
    """유령 포지션에 청산 주문이 나가면 안 된다."""
    _open("p1", "034220", entry=10_000)
    ledger.reap_unfilled({}, now=LATER)
    assert ledger.due_exits(lambda _s: 9_000) == []


def test_void_는_일지에서_빠진다(db, monkeypatch, tmp_path):
    from app import journal

    monkeypatch.setattr(journal, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(journal, "JOURNAL_DIR", tmp_path / "journal")
    _open("p1", "034220", qty=11, entry=9_020)
    _open("p2", "011200", qty=5, entry=20_500, confirmed=True)
    ledger.close_position("p1", 9_030, "manual")
    ledger.close_position("p2", 21_000, "target")
    ledger.void_position("p1", "미체결")

    day = str(ledger.positions("closed")[0]["closed"])[:10]
    entry = journal.build(day)
    assert entry["trades"]["trades"] == 1
    assert [p["symbol"] for p in entry["positions"]] == ["011200"]


def test_void_는_shadow_관측에서_빠진다(db, monkeypatch):
    from app.trade import breakeven

    monkeypatch.setitem(settings.CONFIG, "execution",
                        {"breakeven_shadow": {"enabled": True, "arm_at_pct": 50}})
    _open("p1", "034220")
    ledger.reap_unfilled({}, now=LATER)
    assert breakeven.observe(lambda _s: 10_300, now=LATER) == 0


def test_void_position_은_없는_id_에_False(db):
    assert ledger.void_position("nope") is False
