"""수집 로스터 — 감시목록 이탈 종목의 분봉 수집 연속성 테스트."""
from datetime import UTC, datetime, timedelta

import pytest

from app import settings
from app.data import roster
from app.signals import engine as engine_mod
from app.signals.engine import SignalEngine


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(roster, "DB_PATH", tmp_path / "trading.db")


def _insert_old(code, name, days_ago):
    ts = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    with roster._conn() as conn:
        conn.execute("INSERT INTO collection_roster VALUES (?,?,?,?)",
                     (code, name, ts, ts))


def test_touch_and_active(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    roster.touch({"005930": "삼성전자", "000660": "SK하이닉스"})
    act = roster.active(30)
    assert set(act) == {"005930", "000660"}
    assert act["005930"] == "삼성전자"


def test_active_window_and_prune(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    roster.touch({"005930": "삼성전자"})
    _insert_old("111111", "낡은종목", 40)            # 40일 전 감시
    assert set(roster.active(30)) == {"005930"}       # 창(30일) 밖 → 제외
    assert set(roster.active(60)) == {"005930", "111111"}  # 창 넓히면 포함
    assert roster.prune(30) == 1                       # 낡은 항목 정리
    assert set(roster.active(60)) == {"005930"}


@pytest.mark.asyncio
async def test_collect_roster_backfills_only_dropped(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "WATCHLIST", {"005930": "삼성전자"})
    monkeypatch.setitem(settings.CONFIG, "collection",
                        {"roster_retention_days": 30, "roster_backfill_max": 200})
    # 로스터: 005930(감시중) + 000660(이탈·창 내) + 111111(이탈·창 밖)
    roster.touch({"005930": "삼성전자", "000660": "SK하이닉스"})
    _insert_old("111111", "낡은종목", 40)

    called = []

    async def fake_backfill(sym):
        called.append(sym)
        return 1

    monkeypatch.setattr(engine_mod.collector, "backfill_minutes", fake_backfill)
    eng = SignalEngine()
    n = await eng.collect_roster_once()
    # 감시중(005930)은 run_once 가 처리하므로 제외, 창 밖(111111)은 정리되어 제외
    assert called == ["000660"]
    assert n == 1
