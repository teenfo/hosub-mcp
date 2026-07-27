"""청산 품질 보완 — 실체결가 반영 · 시간 손절 · 종목 중복 진입 차단.

2026-07-27 실거래 분석에서 나온 세 가지 결함의 회귀 방지.
"""
import types
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import settings
from app.signals import engine as engine_mod
from app.signals.engine import SignalEngine
from app.signals.rules import Signal
from app.trade import ledger

KST = ZoneInfo("Asia/Seoul")


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(settings, "WATCHLIST", {"005930": "삼성전자"})
    monkeypatch.setattr(settings, "COSTS", {"commission_pct": 0.015,
                                            "sell_tax_pct": 0.15, "slippage_bp": 5})


def _order(oid="a1", symbol="005930", rule="orb", entry=10_000, stop=9_800,
           target=10_400, qty=10):
    return {"id": oid, "symbol": symbol, "side": "long", "entry": entry,
            "stop": stop, "target": target, "rule": rule, "qty": qty}


def _fill(ord_no, symbol, price, qty=10):
    return {"ord_no": ord_no, "symbol": symbol, "price": price, "qty": qty,
            "state": "체결"}


# --- ① 청산 실체결가 반영 ---
# 시장가 손절은 stop 가격에 팔리지 않는다. 이론가로 남기면 실현손익이 실제보다
# 좋게 기록되고, 그 숫자를 가드·성과·일지가 전부 쓴다.

def test_exit_fill_corrects_pnl(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    ledger.open_position(_order(), fill=10_000)
    # 이론가 9,800(손절선)으로 먼저 기록 — 실체결은 아직 안 왔다
    assert ledger.close_position("a1", 9_800, "stop", ord_no="EX1") is True
    p = ledger.positions(status="closed")[0]
    assert p["exit"] == 9_800 and p["exit_fill_confirmed"] == 0
    theoretical = p["pnl_pct"]

    # 실제로는 9,700 에 체결 — 손익이 정정되어야 한다
    assert ledger.record_fill(_fill("EX1", "005930", 9_700)) is True
    p = ledger.positions(status="closed")[0]
    assert p["exit"] == 9_700 and p["exit_fill_confirmed"] == 1
    assert p["pnl_pct"] < theoretical            # 실제가 더 나쁘다
    assert p["model_exit"] == 9_800              # 이론가는 비교용으로 남는다
    assert round(p["pnl_krw"]) == round(p["qty"] * p["entry"] * p["pnl_pct"] / 100)


def test_exit_fill_arriving_first_is_used(tmp_path, monkeypatch):
    """실시간 체결이 청산 기록보다 먼저 와도 실측가가 쓰인다(순서 무관)."""
    _fresh(tmp_path, monkeypatch)
    ledger.open_position(_order(), fill=10_000)
    ledger.record_fill(_fill("EX2", "005930", 9_650))     # 먼저 도착
    ledger.close_position("a1", 9_800, "stop", ord_no="EX2")
    p = ledger.positions(status="closed")[0]
    assert p["exit"] == 9_650 and p["exit_fill_confirmed"] == 1
    assert p["model_exit"] == 9_800


def test_exit_fill_better_than_theory(tmp_path, monkeypatch):
    """유리하게 체결된 경우도 그대로 반영한다(과소평가 방지)."""
    _fresh(tmp_path, monkeypatch)
    ledger.open_position(_order(target=10_400), fill=10_000)
    ledger.close_position("a1", 10_400, "target", ord_no="EX3")
    before = ledger.positions(status="closed")[0]["pnl_pct"]
    ledger.record_fill(_fill("EX3", "005930", 10_500))
    p = ledger.positions(status="closed")[0]
    assert p["exit"] == 10_500 and p["pnl_pct"] > before


def test_entry_fill_still_works(tmp_path, monkeypatch):
    """진입 체결 경로는 그대로 — 청산 분기가 가로채지 않는다."""
    _fresh(tmp_path, monkeypatch)
    ledger.open_position(_order(), fill=10_000, ord_no="EN1")
    assert ledger.record_fill(_fill("EN1", "005930", 10_050)) is True
    p = ledger.positions(status="open")[0]
    assert p["entry"] == 10_050 and p["fill_confirmed"] == 1


def test_unknown_ord_no_is_safe(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    ledger.open_position(_order(), fill=10_000)
    assert ledger.record_fill(_fill("NOPE", "005930", 9_000)) is False
    assert ledger.positions(status="open")[0]["entry"] == 10_000   # 무변


def test_close_without_ord_no_keeps_theoretical(tmp_path, monkeypatch):
    """주문번호가 없으면(수동 청산 등) 종전대로 이론가로 기록한다."""
    _fresh(tmp_path, monkeypatch)
    ledger.open_position(_order(), fill=10_000)
    ledger.close_position("a1", 9_800, "manual")
    p = ledger.positions(status="closed")[0]
    assert p["exit"] == 9_800 and p["exit_fill_confirmed"] == 0


# --- ② 시간 손절 ---

def test_max_hold_min_resolution(monkeypatch):
    monkeypatch.setattr(settings, "RULES", {
        "max_hold_min": 45, "orb": {"max_hold_min": 30}, "pullback": {}})
    assert ledger.max_hold_min("orb") == 30          # 규칙별 설정 우선
    assert ledger.max_hold_min("pullback") == 45     # 전역 기본값
    assert ledger.max_hold_min("없는규칙") == 45
    monkeypatch.setattr(settings, "RULES", {"orb": {"max_hold_min": "bad"}})
    assert ledger.max_hold_min("orb") == 0           # 잘못된 값 → 끔
    monkeypatch.setattr(settings, "RULES", {})
    assert ledger.max_hold_min("orb") == 0           # 미설정 → 끔


def test_timeout_exit_after_limit(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "RULES", {"orb": {"max_hold_min": 30}})
    ledger.open_position(_order(), fill=10_000)
    opened = datetime.fromisoformat(ledger.positions(status="open")[0]["opened"])

    # 손절·목표 어느 쪽도 아닌 가격
    assert ledger.due_exits(lambda s: 10_100, now=opened + timedelta(minutes=29)) == []
    due = ledger.due_exits(lambda s: 10_100, now=opened + timedelta(minutes=31))
    assert len(due) == 1
    assert due[0]["reason"] == "timeout"
    assert due[0]["exit_px"] == 10_100              # 시간 손절은 현재가로 정리


def test_stop_takes_priority_over_timeout(tmp_path, monkeypatch):
    """손절선에 닿았으면 시간과 무관하게 손절이 먼저다."""
    _fresh(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "RULES", {"orb": {"max_hold_min": 1}})
    ledger.open_position(_order(), fill=10_000)
    opened = datetime.fromisoformat(ledger.positions(status="open")[0]["opened"])
    due = ledger.due_exits(lambda s: 9_700, now=opened + timedelta(minutes=99))
    assert due[0]["reason"] == "stop" and due[0]["exit_px"] == 9_800


def test_timeout_disabled_when_zero(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "RULES", {"max_hold_min": 0})
    ledger.open_position(_order(), fill=10_000)
    opened = datetime.fromisoformat(ledger.positions(status="open")[0]["opened"])
    assert ledger.due_exits(lambda s: 10_100, now=opened + timedelta(hours=5)) == []


def test_held_minutes():
    now = datetime(2026, 7, 27, 10, 30, tzinfo=KST)
    assert ledger.held_minutes("2026-07-27T10:00:00+09:00", now) == 30
    assert ledger.held_minutes("2026-07-27T10:00:00", now) == 30    # tz 없어도
    assert ledger.held_minutes("깨진값", now) is None


# --- ③ 종목 중복 진입 차단 ---

def _prep(monkeypatch, eng, per_symbol, held):
    monkeypatch.setattr(settings, "WATCHLIST", {s: f"종목{s}" for s in per_symbol})
    monkeypatch.setattr(settings, "COLLECT_ONLY", set())
    monkeypatch.setattr(settings, "RULES", {"orb": {"enabled": True, "priority": 1.0},
                                            "pullback": {"enabled": True}})
    eng.equity_synced = True
    eng._fired_restored = True

    async def _noop():
        return None

    async def _noop1(sym):
        return None

    async def _send(oid):
        return {"status": "sent", "message": ""}

    cur = {"sym": ""}
    monkeypatch.setattr(eng, "_sync_equity", _noop)
    monkeypatch.setattr(eng, "_sync_open_positions", lambda: None)
    monkeypatch.setattr(eng, "_effective_regime", lambda: "중립")
    monkeypatch.setattr(eng, "day_guard_status",
                        lambda: {"halted": False, "reason": "", "pct": 0.0})
    monkeypatch.setattr(eng, "_held_symbols", lambda: set(held))
    monkeypatch.setattr(eng, "_today_df",
                        lambda s: (cur.update(sym=s),
                                   (types.SimpleNamespace(empty=False), None))[1])
    monkeypatch.setattr(eng, "_rules_for", lambda s: settings.RULES)
    monkeypatch.setattr(engine_mod.collector, "backfill_minutes", _noop1)
    monkeypatch.setattr(engine_mod.orders, "approve_and_send", _send)
    monkeypatch.setattr(engine_mod.rules, "evaluate_all",
                        lambda df, cfg, prev: list(per_symbol[cur["sym"]]))


def _sig(rule="orb"):
    return Signal(rule=rule, side="long", entry=10_000, stop=9_800,
                  target=10_400, reason="테스트")


@pytest.mark.asyncio
async def test_two_rules_same_symbol_only_one_enters(monkeypatch):
    """같은 종목에 규칙 두 개가 신호를 내도 한 번만 진입한다.
    (회귀 2026-07-27: 신일전자 momentum + pullback 둘 다 진입해 둘 다 손절)"""
    eng = SignalEngine(equity=10_000_000)
    eng.state.max_positions = 9
    monkeypatch.setitem(settings.RISK, "one_position_per_symbol", True)
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", 0)
    _prep(monkeypatch, eng, {"002700": [_sig("orb"), _sig("pullback")]}, held=set())
    sent = []
    monkeypatch.setattr(engine_mod.orders, "propose",
                        lambda s, q: (sent.append(s.rule), "oid")[1])

    found = await eng.run_once()
    assert sent == ["orb"]                       # 우선순위 높은 쪽만
    blocked = [f for f in found if not f["actionable"]]
    assert len(blocked) == 1 and "종목 중복" in blocked[0]["note"]


@pytest.mark.asyncio
async def test_already_held_symbol_blocked(monkeypatch):
    eng = SignalEngine(equity=10_000_000)
    monkeypatch.setitem(settings.RISK, "one_position_per_symbol", True)
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", 0)
    _prep(monkeypatch, eng, {"002700": [_sig("orb")]}, held={"002700"})
    sent = []
    monkeypatch.setattr(engine_mod.orders, "propose",
                        lambda s, q: (sent.append(s.rule), "oid")[1])

    found = await eng.run_once()
    assert sent == [] and "종목 중복" in found[0]["note"]


@pytest.mark.asyncio
async def test_other_symbols_unaffected(monkeypatch):
    eng = SignalEngine(equity=10_000_000)
    eng.state.max_positions = 9
    monkeypatch.setitem(settings.RISK, "one_position_per_symbol", True)
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", 0)
    _prep(monkeypatch, eng, {"002700": [_sig()], "005930": [_sig()]},
          held={"002700"})
    sent = []
    monkeypatch.setattr(engine_mod.orders, "propose",
                        lambda s, q: (sent.append(s.symbol), "oid")[1])

    await eng.run_once()
    assert sent == ["005930"]                    # 보유하지 않은 종목은 정상 진입


@pytest.mark.asyncio
async def test_can_be_turned_off(monkeypatch):
    eng = SignalEngine(equity=10_000_000)
    eng.state.max_positions = 9
    monkeypatch.setitem(settings.RISK, "one_position_per_symbol", False)
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", 0)
    _prep(monkeypatch, eng, {"002700": [_sig("orb"), _sig("pullback")]}, held=set())
    sent = []
    monkeypatch.setattr(engine_mod.orders, "propose",
                        lambda s, q: (sent.append(s.rule), "oid")[1])

    await eng.run_once()
    assert sent == ["orb", "pullback"]           # 종전 동작


def test_open_symbols(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    assert ledger.open_symbols() == set()
    ledger.open_position(_order("s1", symbol="005930"), fill=10_000)
    ledger.open_position(_order("s2", symbol="000660"), fill=10_000)
    assert ledger.open_symbols() == {"005930", "000660"}
    ledger.close_position("s1", 9_800, "stop")
    assert ledger.open_symbols() == {"000660"}
