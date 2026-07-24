import pytest

from app import settings
from app.signals.engine import SignalEngine
from app.trade import risk


@pytest.mark.asyncio
async def test_sync_equity_sets_real_balance(monkeypatch):
    monkeypatch.setattr(settings, "KIWOOM_APP_KEY", "x")
    eng = SignalEngine()
    assert eng.equity_synced is False           # 최초엔 미동기화(가짜 기본값)

    async def fake_balance():
        return {"return_code": 0, "prsm_dpst_aset_amt": "142589"}

    monkeypatch.setattr("app.kiwoom.client.client.balance", fake_balance)
    await eng._sync_equity()
    assert eng.equity_synced is True
    assert eng.equity == 142589.0 and eng.state.equity == 142589.0


@pytest.mark.asyncio
async def test_sync_equity_failure_stays_unsynced(monkeypatch):
    monkeypatch.setattr(settings, "KIWOOM_APP_KEY", "x")
    eng = SignalEngine()

    async def boom():
        raise RuntimeError("network")

    monkeypatch.setattr("app.kiwoom.client.client.balance", boom)
    await eng._sync_equity()
    assert eng.equity_synced is False           # 실패 시 신규 사이징 보류됨


def test_position_size_zero_when_unaffordable():
    # 예탁 142,589원으로 44만원짜리 삼성SDI(손절폭 13,000) → 수량 0
    assert risk.position_size(142_589, 0.5, 441_500, 454_500) == 0
    # 가짜 1천만원이었으면 3주가 나왔음(버그 재현)
    assert risk.position_size(10_000_000, 0.5, 441_500, 454_500) == 3
