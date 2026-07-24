"""Codex 리뷰 반영 회귀 테스트 — 시간대 필터·차단해제 재발주·청산 exit_pending 등."""
import pandas as pd
import pytest

from app import settings
from app.signals import rules
from app.signals.engine import SignalEngine
from app.signals.rules import Signal
from app.trade import ledger, orders


def _bars(n=30, close=102.5):
    idx = pd.date_range("2026-07-20 09:00", periods=n, freq="1min")
    rows = [{"open": 101, "high": 102, "low": 100, "close": 101, "volume": 1000}] * (n - 1)
    rows.append({"open": 101, "high": close + 0.2, "low": 101, "close": close, "volume": 2000})
    return pd.DataFrame(rows, index=idx)


def test_entry_before_time_filter():
    cfg = {"orb": {"enabled": True, "range_start": "09:00", "range_end": "09:15",
                   "target_r": 1.5, "entry_before": "11:00"}}
    early = _bars(30)                                   # 마지막 봉 09:29
    assert [s.rule for s in rules.evaluate_all(early, cfg)] == ["orb"]
    late = _bars(150)                                   # 마지막 봉 11:29 → 시간창 밖
    assert rules.evaluate_all(late, cfg) == []


@pytest.mark.asyncio
async def test_blocked_signal_revives_when_actionable(monkeypatch):
    """잔고 부족 등으로 비발주 기록된 신호가, 이후 발주 가능해지면 되살아난다."""
    import types
    from app.signals import engine as engine_mod

    monkeypatch.setattr(settings, "WATCHLIST", {"005930": "삼성전자"})
    eng = SignalEngine(equity=1_000)                     # 처음엔 1주도 못 사는 자산
    eng.equity_synced = True

    async def _noop():
        return None

    monkeypatch.setattr(eng, "_sync_equity", _noop)
    monkeypatch.setattr(eng, "_effective_regime", lambda: "중립")
    monkeypatch.setattr(eng, "day_guard_status",
                        lambda: {"halted": False, "reason": "", "pct": 0.0})
    monkeypatch.setattr(eng, "_today_df",
                        lambda s: (types.SimpleNamespace(empty=False), None))
    monkeypatch.setattr(eng, "_rules_for", lambda s: {})

    async def _nb(sym):
        return None

    monkeypatch.setattr(engine_mod.collector, "backfill_minutes", _nb)
    monkeypatch.setattr(engine_mod.rules, "evaluate_all",
                        lambda df, cfg, prev: [Signal(
                            rule="orb", side="long", entry=10_000, stop=9_800,
                            target=10_400, reason="x")])
    calls = []
    monkeypatch.setattr(engine_mod.orders, "propose",
                        lambda s, q: (calls.append(q), "oid")[1])

    r1 = await eng.run_once()
    assert r1 and r1[0]["actionable"] is False and calls == []   # 잔고 부족 기록만
    r2 = await eng.run_once()
    assert r2 == []                                               # 같은 상태 반복 기록 안 함
    eng.equity = 10_000_000                                       # 입금 후
    r3 = await eng.run_once()
    assert r3 and r3[0]["actionable"] is True and len(calls) == 1  # 되살아나 발주


def await_(coro):
    import asyncio
    return asyncio.run(coro)


def test_exit_rejected_clears_exit_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(orders, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(settings, "WATCHLIST", {"005930": "삼성전자"})
    monkeypatch.setattr(settings, "COSTS", {"commission_pct": 0.015,
                                            "sell_tax_pct": 0.15, "slippage_bp": 5})
    ledger.open_position({"id": "p1", "symbol": "005930", "side": "long",
                          "rule": "orb", "qty": 5, "entry": 10_000, "stop": 9_800,
                          "target": 10_400, "exec_symbol": None}, fill=10_000)
    ex = ledger.due_exits(lambda s: 10_500)[0]
    oid = orders.propose_exit(ex, "target", ex["exit_px"])
    assert ledger.due_exits(lambda s: 10_500) == []      # exit_pending=1

    async def fake_order(side, symbol, qty, price=0):
        return {"return_code": 40, "return_msg": "장 마감"}   # 브로커 거부

    monkeypatch.setattr("app.kiwoom.client.client.order", fake_order)
    res = await_(orders.approve_and_send(oid))
    assert res["ok"] is False
    # 거부 후 exit_pending 이 풀려 다시 청산 제안 가능해야 한다
    assert ledger.due_exits(lambda s: 10_500)[0]["id"] == "p1"


def test_qty_override_rejected_for_inverse_mapped(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(orders, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(settings, "INVERSE_ETF", "114800")
    monkeypatch.setattr(settings, "RISK", {"signal_ttl_min": 10})
    sig = Signal(rule="orb", side="short", entry=10_000, stop=10_200,
                 target=9_400, reason="x", symbol="005930")
    oid = orders.propose(sig, qty=5)                      # 숏 → 인버스 매수 매핑
    res = await_(orders.approve_and_send(oid, qty=3))
    assert res["ok"] is False and "인버스" in res["error"]  # 수량 조정 미지원
    assert orders.get(oid)["status"] == "pending"          # 주문은 그대로 대기
