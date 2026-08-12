"""원장 env(real/mock) 마킹 — 모의 표본이 실계좌 측정에 섞이지 않게.

실사고 2026-08-06~12: 사용자가 mock 키로 전환한 일주일치 모의 거래가
positions·broker_fills·signal_log 에 무표식으로 쌓여 실계좌 이력과 섞였다.
측정(타임아웃 반사실·불일치일 등)은 실거래 표본을 전제하므로, 기록 시점의
계좌 환경이 행에 남아야 표본을 정확히 자를 수 있다.
"""
import pytest

from app import journal, settings
from app.trade import fills, ledger


@pytest.fixture(autouse=True)
def _tmp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(journal, "DB_PATH", tmp_path / "j.db")


def _order(oid="o1"):
    return {"id": oid, "symbol": "005930", "rule": "orb", "side": "long",
            "qty": 1, "entry": 100.0, "stop": 99.0, "target": 102.0}


def test_포지션은_기록_시점의_env_를_남긴다(monkeypatch):
    monkeypatch.setattr(settings, "KIWOOM_ENV", "mock")
    ledger.open_position(_order())
    with ledger._conn() as conn:
        assert conn.execute("SELECT env FROM positions WHERE id='o1'") \
                   .fetchone()[0] == "mock"


def test_실계좌로_돌아오면_새_행은_real_로_남는다(monkeypatch):
    monkeypatch.setattr(settings, "KIWOOM_ENV", "mock")
    ledger.open_position(_order("o1"))
    monkeypatch.setattr(settings, "KIWOOM_ENV", "real")
    ledger.open_position(_order("o2"))
    with ledger._conn() as conn:
        rows = dict(conn.execute("SELECT id, env FROM positions").fetchall())
    assert rows == {"o1": "mock", "o2": "real"}


def test_broker_daily_에도_env_가_남는다(monkeypatch):
    monkeypatch.setattr(settings, "KIWOOM_ENV", "mock")
    fills.store_daily("2026-08-12", {"ok": True, "realized": -100.0,
                                     "commission": 10.0, "tax": 5.0})
    with fills._conn() as conn:
        assert conn.execute("SELECT env FROM broker_daily WHERE d='2026-08-12'") \
                   .fetchone()[0] == "mock"


def test_broker_fills_에도_env_가_남는다(monkeypatch):
    monkeypatch.setattr(settings, "KIWOOM_ENV", "real")
    fills.store_today([{"ord_no": "1", "code": "005930", "name": "삼성전자",
                        "side": "buy", "qty": 1, "price": 100.0,
                        "time": "090001"}], day="2026-08-12")
    with fills._conn() as conn:
        assert conn.execute("SELECT env FROM broker_fills LIMIT 1") \
                   .fetchone()[0] == "real"


def test_signal_log_에도_env_가_남는다(monkeypatch):
    monkeypatch.setattr(settings, "KIWOOM_ENV", "mock")
    journal.record_signal("2026-08-12", {"symbol": "005930", "rule": "orb",
                                         "ts": "2026-08-12T09:30:00",
                                         "actionable": True})
    with journal._conn() as conn:
        assert conn.execute("SELECT env FROM signal_log LIMIT 1") \
                   .fetchone()[0] == "mock"
