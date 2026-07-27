"""사이클 내 자금 배분 — 우선순위 정렬 + 종목당 비중 상한 + 남은 현금 차감.

회귀 배경(실측): 상한도 정렬도 없던 시절, 감시목록 순회 순서(종목코드 순)로
발주가 나가면서 앞자리 ORB 2건이 예수금의 85%를 소진하고 뒤이은 16건이 전부
'1주도 매수 불가'로 밀렸다. 신호 품질과 무관한 구조적 편중.
"""
import types

import pytest

from app import settings
from app.signals import engine as engine_mod
from app.signals.engine import SignalEngine
from app.signals.rules import Signal

RULES = {"orb": {"enabled": True, "priority": 1.0},
         "momentum": {"enabled": True, "priority": 0.6}}


def _prep(monkeypatch, eng, per_symbol: dict, rules_cfg=None):
    """run_once 의존성 고정. per_symbol: 종목코드 → 그 종목이 낼 Signal 목록."""
    monkeypatch.setattr(settings, "WATCHLIST",
                        {s: f"종목{s}" for s in per_symbol})
    monkeypatch.setattr(settings, "COLLECT_ONLY", set())
    monkeypatch.setattr(settings, "RULES", rules_cfg or RULES)
    eng.equity_synced = True
    eng._fired_restored = True          # 주문 이력 DB(로컬 아티팩트)에 의존하지 않게

    async def _noop_sync():
        return None

    async def _noop_backfill(sym):
        return None

    async def _noop_send(order_id):     # auto_approve=true 라도 실제 발주는 막는다
        return {"status": "sent", "message": ""}

    monkeypatch.setattr(engine_mod.orders, "approve_and_send", _noop_send)
    # 포지션 수는 각 테스트가 eng.state 로 직접 지정한다(장부 DB 비의존).
    monkeypatch.setattr(eng, "_sync_open_positions", lambda: None)

    cur = {"sym": ""}
    monkeypatch.setattr(eng, "_sync_equity", _noop_sync)
    monkeypatch.setattr(eng, "_effective_regime", lambda: "중립")
    monkeypatch.setattr(eng, "day_guard_status",
                        lambda: {"halted": False, "reason": "", "pct": 0.0})
    monkeypatch.setattr(eng, "_today_df",
                        lambda s: (cur.update(sym=s),
                                   (types.SimpleNamespace(empty=False), None))[1])
    monkeypatch.setattr(eng, "_rules_for", lambda s: rules_cfg or RULES)
    monkeypatch.setattr(engine_mod.collector, "backfill_minutes", _noop_backfill)
    monkeypatch.setattr(engine_mod.rules, "evaluate_all",
                        lambda df, cfg, prev: list(per_symbol[cur["sym"]]))


def _sig(rule, entry, stop, target):
    return Signal(rule=rule, side="long", entry=entry, stop=stop,
                  target=target, reason="테스트")


@pytest.mark.asyncio
async def test_better_signal_orders_first_despite_symbol_order(monkeypatch):
    """감시목록 앞자리(momentum)보다 뒷자리(orb)가 먼저 발주된다."""
    eng = SignalEngine(equity=1_000_000)
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", 0)
    _prep(monkeypatch, eng, {
        "000100": [_sig("momentum", 10_000, 9_800, 10_400)],
        "900000": [_sig("orb", 10_000, 9_800, 10_400)],
    })
    sent = []
    monkeypatch.setattr(engine_mod.orders, "propose",
                        lambda s, q: (sent.append(s.symbol), "oid")[1])

    found = await eng.run_once()
    assert sent == ["900000", "000100"]              # 품질 순 — 선착순 아님
    assert [f["symbol"] for f in found] == ["900000", "000100"]
    assert found[0]["priority"] > found[1]["priority"]


@pytest.mark.asyncio
async def test_weight_cap_leaves_capital_for_later_signals(monkeypatch):
    """비중 상한이 없으면 첫 신호가 전액을 쓰고 뒤 신호는 0주가 된다."""
    monkeypatch.setitem(settings.RISK, "risk_per_trade_pct", 5.0)
    signals = {
        "000100": [_sig("orb", 100_000, 99_000, 101_500)],
        "000200": [_sig("orb", 100_000, 99_000, 101_500)],
        "000300": [_sig("orb", 100_000, 99_000, 101_500)],
    }
    monkeypatch.setattr(engine_mod.orders, "propose", lambda s, q: "oid")

    # 상한 없음 → 첫 종목이 5주(50만) 다 쓰고 나머지는 0주
    eng = SignalEngine(equity=500_000)
    eng.state.max_positions = 9
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", 0)
    _prep(monkeypatch, eng, signals)
    qtys = [f["qty"] for f in await eng.run_once()]
    assert qtys == [5, 0, 0]

    # 상한 20%(=1주) → 세 종목 모두 진입
    eng2 = SignalEngine(equity=500_000)
    eng2.state.max_positions = 9
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", 20)
    _prep(monkeypatch, eng2, signals)
    assert [f["qty"] for f in await eng2.run_once()] == [1, 1, 1]


@pytest.mark.asyncio
async def test_note_explains_weight_cap(monkeypatch):
    """1주 값이 비중 상한을 넘으면 '잔고 부족'이 아니라 '비중 상한'으로 표시."""
    eng = SignalEngine(equity=560_000)
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", 20)
    _prep(monkeypatch, eng, {
        "000100": [_sig("orb", 150_000, 148_000, 153_000)],   # 상한 112,000 초과
    })
    found = await eng.run_once()
    assert found[0]["qty"] == 0
    assert "비중 상한" in found[0]["note"]
    assert "잔고 부족" not in found[0]["note"]


@pytest.mark.asyncio
async def test_spent_cash_deducted_within_cycle(monkeypatch):
    """앞선 발주가 쓴 금액만큼 뒤 신호의 매수여력이 줄어든다."""
    eng = SignalEngine(equity=250_000)
    eng.state.max_positions = 9
    monkeypatch.setitem(settings.RISK, "risk_per_trade_pct", 50.0)
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", 80)
    _prep(monkeypatch, eng, {
        "000100": [_sig("orb", 100_000, 90_000, 115_000)],       # 우선 발주
        "000200": [_sig("momentum", 100_000, 90_000, 115_000)],
    })
    monkeypatch.setattr(engine_mod.orders, "propose", lambda s, q: "oid")
    found = await eng.run_once()
    assert found[0]["qty"] == 2                   # 80% 상한 = 2주(20만)
    assert found[1]["qty"] == 0                   # 남은 5만 < 1주
    assert "잔고 부족" in found[1]["note"]
    assert "가용" in found[1]["note"]             # 자산이 아닌 '남은 현금' 기준


@pytest.mark.asyncio
async def test_already_fired_signal_not_reordered(monkeypatch):
    """이미 발주된 신호는 정렬 대상에서 빠져 재발주되지 않는다."""
    eng = SignalEngine(equity=1_000_000)
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", 0)
    _prep(monkeypatch, eng, {"000100": [_sig("orb", 10_000, 9_800, 10_400)]})
    sent = []
    monkeypatch.setattr(engine_mod.orders, "propose",
                        lambda s, q: (sent.append(s.symbol), "oid")[1])
    assert len(await eng.run_once()) == 1
    assert await eng.run_once() == []              # 2회차는 조용히 스킵
    assert sent == ["000100"]


@pytest.mark.asyncio
async def test_max_positions_still_enforced(monkeypatch):
    """우선순위가 높아도 동시 포지션 한도는 그대로 막는다."""
    eng = SignalEngine(equity=1_000_000)
    eng.state.max_positions = 1
    eng.state.open_positions = 1                  # 이미 한도 소진
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", 0)
    _prep(monkeypatch, eng, {
        "000100": [_sig("orb", 10_000, 9_800, 10_400)],
        "000200": [_sig("momentum", 10_000, 9_800, 10_400)],
    })
    sent = []
    monkeypatch.setattr(engine_mod.orders, "propose",
                        lambda s, q: (sent.append(s.symbol), "oid")[1])

    found = await eng.run_once()
    assert len(found) == 2 and sent == []         # 수량은 나오지만 발주는 없음
    assert all(f["actionable"] is False for f in found)
    assert all("최대 동시 포지션" in f["note"] for f in found)


@pytest.mark.asyncio
async def test_cycle_stops_at_position_limit(monkeypatch):
    """한 사이클 안에서도 한도를 넘어 발주하지 않는다.

    회귀: open_positions 가 어디서도 갱신되지 않아 항상 0 이었고, max_positions
    가 사실상 무제한이었다(설정 3 인데 실계좌에 5종목이 열린 원인).
    """
    eng = SignalEngine(equity=10_000_000)
    eng.state.max_positions = 2
    eng.state.open_positions = 0
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", 0)
    _prep(monkeypatch, eng, {
        f"00010{i}": [_sig("orb", 10_000, 9_800, 10_400)] for i in range(4)
    })
    sent = []
    monkeypatch.setattr(engine_mod.orders, "propose",
                        lambda s, q: (sent.append(s.symbol), "oid")[1])

    found = await eng.run_once()
    assert len(sent) == 2                         # 한도까지만 발주
    assert sum(f["actionable"] for f in found) == 2
    assert "최대 동시 포지션" in found[-1]["note"]


@pytest.mark.asyncio
async def test_open_positions_synced_from_ledger(monkeypatch):
    """사이클 시작 시 장부의 실제 열린 포지션 수로 한도 기준을 맞춘다."""
    from app.trade import ledger

    eng = SignalEngine(equity=10_000_000)
    monkeypatch.setitem(settings.RISK, "max_positions", 3)
    monkeypatch.setattr(ledger, "open_count", lambda: 3)
    eng._sync_open_positions()
    assert eng.state.open_positions == 3 and eng.state.max_positions == 3
    assert eng.state.can_open()[0] is False

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(ledger, "open_count", _boom)
    eng._sync_open_positions()                    # 조회 실패는 직전 값 유지
    assert eng.state.open_positions == 3
