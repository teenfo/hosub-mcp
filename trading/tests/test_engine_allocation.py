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

    # 상한 없음 → 첫 종목이 5주(50만)를 다 쓴다. 뒤 신호도 **수량은 계산되고
    # 승인대기에 올라가되** 살 수 없다는 것이 fundable 로 표시된다(2026-07-29).
    eng = SignalEngine(equity=500_000)
    eng.state.max_positions = 9
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", 0)
    _prep(monkeypatch, eng, signals)
    found = await eng.run_once()
    assert [f["qty"] for f in found] == [5, 5, 5]
    assert [f["fundable"] for f in found] == [True, False, False]
    assert "자금 부족" in found[1]["note"]

    # 상한 20%(=1주) → 세 종목 모두 진입하고 셋 다 자금이 된다
    eng2 = SignalEngine(equity=500_000)
    eng2.state.max_positions = 9
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", 20)
    _prep(monkeypatch, eng2, signals)
    found2 = await eng2.run_once()
    assert [f["qty"] for f in found2] == [1, 1, 1]
    assert all(f["fundable"] for f in found2)


@pytest.mark.asyncio
async def test_over_weight_reaches_the_queue_but_not_auto_approval(monkeypatch):
    """1주 값이 비중 상한을 넘어도 **승인대기에는 올린다** — 보이지 않으면
    판단할 수 없다. 다만 자동승인은 집어가지 않는다(비중 규칙은 안전장치다).
    """
    eng = SignalEngine(equity=560_000)
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", 20)
    monkeypatch.setitem(settings.RISK, "auto_approve", True)
    sent = []
    monkeypatch.setattr(engine_mod.orders, "propose", lambda s, q: "oid")

    async def _spy(order_id, qty=None):
        sent.append(order_id)
        return {"status": "sent", "message": ""}

    monkeypatch.setattr(engine_mod.orders, "approve_and_send", _spy)
    _prep(monkeypatch, eng, {
        "000100": [_sig("orb", 150_000, 148_000, 153_000)],   # 상한 112,000 초과
    })
    found = await eng.run_once()
    assert found[0]["qty"] == 1 and found[0]["over_weight"] is True
    assert found[0]["actionable"] is True and found[0]["order_id"] == "oid"
    assert "비중 상한" in found[0]["note"]
    assert sent == [], "자동승인이 비중 상한을 우회하면 안 된다"


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
    assert found[0]["qty"] == 2 and found[0]["fundable"] is True   # 80% 상한 = 2주
    # 뒤 신호는 수량이 계산돼 승인대기에는 오르지만 남은 5만으로는 못 산다
    assert found[1]["qty"] == 2 and found[1]["fundable"] is False
    assert "자금 부족" in found[1]["note"]
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
    """한도 도달 시 **자동발주는 막되 대기열에는 올린다**.

    사용자 결정(2026-08-03): 계좌가 발주 불가능한 상태여도 승인 대기열에는
    무조건 올린다 — 종전에는 하루 16건(실측 8/3)이 기록만 남고 대기열에
    흔적이 없어, 자리를 정리하면 살 수 있었다는 사실을 사용자가 몰랐다.
    """
    eng = SignalEngine(equity=1_000_000)
    eng.state.max_positions = 1
    eng.state.open_positions = 1                  # 이미 한도 소진
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", 0)
    _prep(monkeypatch, eng, {
        "000100": [_sig("orb", 10_000, 9_800, 10_400)],
        "000200": [_sig("momentum", 10_000, 9_800, 10_400)],
    })
    sent, auto = [], []
    monkeypatch.setattr(engine_mod.orders, "propose",
                        lambda s, q: (sent.append(s.symbol), "oid")[1])

    async def _spy_send(order_id, qty=None):
        auto.append(order_id)
        return {"status": "sent", "message": ""}

    monkeypatch.setattr(engine_mod.orders, "approve_and_send", _spy_send)
    found = await eng.run_once()
    assert len(found) == 2 and len(sent) == 2     # 대기열 생성
    assert auto == []                             # 자동발주는 없음
    assert all(f["actionable"] for f in found)
    assert all(f.get("manual_only") for f in found)
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

    auto = []

    async def _spy_send(order_id, qty=None):
        auto.append(order_id)
        return {"status": "sent", "message": ""}

    monkeypatch.setattr(engine_mod.orders, "approve_and_send", _spy_send)
    found = await eng.run_once()
    # 대기열은 4건 전부 생기고, **자동발주만** 한도(2)에서 멈춘다
    assert len(sent) == 4
    assert len(auto) == 2
    assert sum(bool(f.get("manual_only")) for f in found) == 2
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


@pytest.mark.asyncio
async def test_pending_order_fires_a_slack_notification(monkeypatch):
    """승인대기 주문이 생기는 **바로 그 지점**에서 알림이 나가야 한다.

    나중(승인·발주 시점)이면 늦다 — 사용자가 승인할지 정하려고 받는 알림이다.
    자금이 모자란 건도 보낸다: 입금하거나 다른 포지션을 정리하는 판단을 하려면
    신호가 났다는 사실을 알아야 한다.
    """
    sent = []

    async def spy(text):
        sent.append(text)

    monkeypatch.setattr(engine_mod.slack, "notify", spy)
    monkeypatch.setattr(engine_mod.orders, "propose", lambda s, q: "oid")
    eng = SignalEngine(equity=500_000)
    eng.state.max_positions = 9
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", 0)
    monkeypatch.setitem(settings.RISK, "risk_per_trade_pct", 5.0)
    _prep(monkeypatch, eng, {
        "000100": [_sig("orb", 100_000, 99_000, 101_500)],
        "000200": [_sig("orb", 400_000, 396_000, 406_000)],   # 자금 부족
    })
    await eng.run_once()
    assert len(sent) == 2, "자금이 모자란 건도 알린다"
    assert any("종목000100" in t for t in sent)
    assert any("🟡" in t for t in sent), "자금 부족은 제목에 드러난다"


# --- 조회 부하 축소: 동시 조회 종목 수를 낮춘다 (2026-07-30 사용자 결정) ---
#
# 모든 주기의 우선순위는 매매 데스크다. 데스크가 2초를 지키려면 같은 예산을 쓰는
# 다른 호출을 줄여야 하고, 줄이는 방식은 **동시 조회 종목 수**를 낮추는 것이다.

def _eng():
    return SignalEngine(equity=1_000_000)


def test_batch_returns_all_when_disabled(monkeypatch):
    """0 이면 종전대로 전부 — 되돌리기 경로가 살아 있어야 한다."""
    monkeypatch.setitem(settings.CONFIG, "collection", {"backfill_per_cycle": 0})
    targets = [f"{i:06d}" for i in range(20)]
    assert _eng()._backfill_batch(targets) == targets


def test_batch_caps_the_count(monkeypatch):
    monkeypatch.setitem(settings.CONFIG, "collection", {"backfill_per_cycle": 5})
    got = _eng()._backfill_batch([f"{i:06d}" for i in range(20)])
    assert len(got) == 5


def test_batch_rotates_so_nothing_starves(monkeypatch):
    """라운드로빈 — n종목·배치 k면 ceil(n/k) 사이클에 전부 한 번씩 돈다."""
    monkeypatch.setitem(settings.CONFIG, "collection", {"backfill_per_cycle": 4})
    e = _eng()
    targets = [f"{i:06d}" for i in range(12)]
    seen = set()
    for _ in range(3):
        seen |= set(e._backfill_batch(targets))
    assert seen == set(targets), "3사이클에 12종목 전부 돌아야 한다"


def test_held_symbols_are_always_in_the_batch(monkeypatch):
    """청산 판정이 걸린 종목의 분봉이 낡는 것은 다른 종목과 무게가 다르다."""
    monkeypatch.setitem(settings.CONFIG, "collection", {"backfill_per_cycle": 3})
    e = _eng()
    e._backfill_held = {"000011"}
    targets = [f"{i:06d}" for i in range(20)]
    for _ in range(6):
        assert "000011" in e._backfill_batch(targets)


def test_batch_never_exceeds_cap_even_with_many_holdings(monkeypatch):
    monkeypatch.setitem(settings.CONFIG, "collection", {"backfill_per_cycle": 2})
    e = _eng()
    e._backfill_held = {"000001", "000002", "000003"}
    got = e._backfill_batch([f"{i:06d}" for i in range(10)])
    assert len(got) == 2 and set(got) <= e._backfill_held


# --- 계좌 상태와 무관하게 대기열은 생긴다 (사용자 결정 2026-08-03) ---

@pytest.mark.asyncio
async def test_가드_도달_자동모드에서도_대기열은_생긴다(monkeypatch):
    """종전에는 가드+자동 모드가 사이클을 통째로 끝내 신호가 기록조차 안 됐다."""
    eng = SignalEngine(equity=1_000_000)
    eng.state.max_positions = 5
    monkeypatch.setitem(settings.RISK, "auto_approve", True)
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", 0)
    _prep(monkeypatch, eng, {"000100": [_sig("orb", 10_000, 9_800, 10_400)]})
    monkeypatch.setattr(eng, "day_guard_status",
                        lambda: {"halted": True, "reason": "일일 손실 한도 도달",
                                 "pct": -2.1})
    sent, auto = [], []
    monkeypatch.setattr(engine_mod.orders, "propose",
                        lambda s, q: (sent.append(s.symbol), "oid")[1])

    async def _spy(order_id, qty=None):
        auto.append(order_id)
        return {"status": "sent", "message": ""}

    monkeypatch.setattr(engine_mod.orders, "approve_and_send", _spy)
    found = await eng.run_once()
    assert len(sent) == 1 and auto == []          # 대기열 생성, 자동발주 정지
    assert found[0]["guard_warn"]                 # '한도 도달 후 진입' 표시


@pytest.mark.asyncio
async def test_리스크_기준_0주는_1주로_제안된다(monkeypatch):
    """고가주라 리스크 예산으로 0주가 나와도 — 보이지 않으면 판단할 수 없다."""
    eng = SignalEngine(equity=100_000)
    eng.state.max_positions = 5
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", 0)
    _prep(monkeypatch, eng, {"000100": [_sig("orb", 200_000, 196_000, 208_000)]})
    sent, auto = [], []
    monkeypatch.setattr(engine_mod.orders, "propose",
                        lambda s, q: (sent.append((s.symbol, q)), "oid")[1])

    async def _spy(order_id, qty=None):
        auto.append(order_id)
        return {"status": "sent", "message": ""}

    monkeypatch.setattr(engine_mod.orders, "approve_and_send", _spy)
    found = await eng.run_once()
    assert sent == [("000100", 1)]                # 1주로 제안
    assert auto == []                             # 자동발주 제외
    assert found[0].get("manual_only")
    assert "리스크 기준 수량 0주" in found[0]["note"]


def test_슬랙_문구가_수동_승인_상태를_구분한다():
    from app.notify import slack

    rec = {"name": "가", "symbol": "000100", "qty": 1, "entry": 10_000,
           "rule": "orb", "side": "long", "stop": 9_800, "target": 10_400,
           "manual_only": True, "note": "최대 동시 포지션(4) 도달 — 자동발주 제외"}
    text = slack.pending_order_text(rec)
    assert "발주 불가 상태 — 수동 승인만" in text
    assert "최대 동시 포지션" in text


# --- 인버스 발주 우선권 (사용자 결정 2026-08-03 — 하락장 수익 최우선) ---

def _inv_prep(monkeypatch, eng, regime):
    monkeypatch.setitem(settings.CONFIG, "inverse_etfs", ["114800"])
    monkeypatch.setitem(settings.CONFIG, "regime_gate",
                        dict(settings.CONFIG.get("regime_gate", {}),
                             inverse_priority=True,
                             inverse_priority_regimes=["약세"],
                             enabled=False))       # 인버스 '차단' 게이트는 끔
    monkeypatch.setattr(eng, "_effective_regime", lambda: regime)
    eng.regime = regime


@pytest.mark.asyncio
async def test_약세_국면에서는_인버스가_먼저_발주된다(monkeypatch):
    """실측 8/3: 인버스 신호가 포지션 한도 경쟁에서 롱에 밀려 미발주 —
    같은 사이클에서 자리·자금을 인버스가 먼저 가져가야 한다."""
    eng = SignalEngine(equity=1_000_000)
    eng.state.max_positions = 1                    # 자리 하나를 두고 경쟁
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", 0)
    _prep(monkeypatch, eng, {
        "000100": [_sig("orb", 10_000, 9_800, 10_400)],       # 롱(규칙 우선순위 높음)
        "114800": [_sig("momentum", 10_000, 9_800, 10_400)],  # 인버스(낮음)
    })
    _inv_prep(monkeypatch, eng, "약세")
    sent = []
    monkeypatch.setattr(engine_mod.orders, "propose",
                        lambda s, q: (sent.append(s.symbol), "oid")[1])
    found = await eng.run_once()
    assert found[0]["symbol"] == "114800"          # 인버스가 맨 앞
    assert sent[0] == "114800"


@pytest.mark.asyncio
async def test_약세가_아니면_순서는_규칙_우선순위_그대로다(monkeypatch):
    eng = SignalEngine(equity=1_000_000)
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", 0)
    _prep(monkeypatch, eng, {
        "000100": [_sig("orb", 10_000, 9_800, 10_400)],
        "114800": [_sig("momentum", 10_000, 9_800, 10_400)],
    })
    _inv_prep(monkeypatch, eng, "중립")
    monkeypatch.setattr(engine_mod.orders, "propose", lambda s, q: "oid")
    found = await eng.run_once()
    assert found[0]["symbol"] == "000100"          # orb(1.0) > momentum(0.6)


@pytest.mark.asyncio
async def test_우선권은_config_로_끈다(monkeypatch):
    eng = SignalEngine(equity=1_000_000)
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", 0)
    _prep(monkeypatch, eng, {
        "000100": [_sig("orb", 10_000, 9_800, 10_400)],
        "114800": [_sig("momentum", 10_000, 9_800, 10_400)],
    })
    _inv_prep(monkeypatch, eng, "약세")
    monkeypatch.setitem(settings.CONFIG, "regime_gate",
                        dict(settings.CONFIG["regime_gate"],
                             inverse_priority=False))
    monkeypatch.setattr(engine_mod.orders, "propose", lambda s, q: "oid")
    found = await eng.run_once()
    assert found[0]["symbol"] == "000100"


# --- 약세 리스크 프로필 (사용자 결정 2026-08-03 후속 — 예약 슬롯 + 인버스 비중) ---

def _bear_profile(monkeypatch, eng, regime, slots=1, weight=40,
                  held=frozenset()):
    """우선권 설정 위에 약세 프로필을 명시적으로 얹는다(config 기본값 비의존).
    held: 장부 보유 종목 — 로컬 DB 아티팩트에 좌우되지 않게 항상 고정한다."""
    from app.trade import ledger as ledger_mod
    _inv_prep(monkeypatch, eng, regime)
    monkeypatch.setitem(settings.CONFIG, "regime_gate",
                        dict(settings.CONFIG["regime_gate"],
                             inverse_reserved_slots=slots,
                             inverse_weight_pct=weight))
    monkeypatch.setattr(ledger_mod, "open_symbols", lambda: set(held))


@pytest.mark.asyncio
async def test_약세_예약슬롯은_롱의_마지막_자리를_막는다(monkeypatch):
    """우선권(정렬)으로 못 푸는 부분 — 롱이 자리를 전부 점유하면 인버스가
    영영 못 들어온다. 마지막 자리는 롱에게 주지 않는다(한도 내 예약)."""
    eng = SignalEngine(equity=1_000_000)
    eng.state.max_positions = 2
    eng.state.open_positions = 1
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", 0)
    _prep(monkeypatch, eng, {"000100": [_sig("orb", 10_000, 9_800, 10_400)]})
    _bear_profile(monkeypatch, eng, "약세")
    monkeypatch.setattr(engine_mod.orders, "propose", lambda s, q: "oid")
    found = await eng.run_once()
    assert found[0].get("manual_only") is True     # 대기열엔 남되 자동발주 제외
    assert "인버스 예약" in found[0]["note"]


@pytest.mark.asyncio
async def test_약세_인버스는_예약과_무관하게_전체_한도를_쓴다(monkeypatch):
    eng = SignalEngine(equity=1_000_000)
    eng.state.max_positions = 2
    eng.state.open_positions = 1
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", 0)
    _prep(monkeypatch, eng, {"114800": [_sig("momentum", 10_000, 9_800, 10_400)]})
    _bear_profile(monkeypatch, eng, "약세")
    monkeypatch.setattr(engine_mod.orders, "propose", lambda s, q: "oid")
    found = await eng.run_once()
    assert not found[0].get("manual_only")         # 1 < 2 — 전체 한도 기준 통과


@pytest.mark.asyncio
async def test_인버스가_이미_열려있으면_예약은_소진된다(monkeypatch):
    """예약은 '인버스 몫 확보'까지다 — 이미 인버스가 자리를 차지했으면
    남은 자리는 롱이 쓴다(총 노출을 놀리지 않는다)."""
    eng = SignalEngine(equity=1_000_000)
    eng.state.max_positions = 2
    eng.state.open_positions = 1                   # 그 1건이 인버스
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", 0)
    _prep(monkeypatch, eng, {"000100": [_sig("orb", 10_000, 9_800, 10_400)]})
    _bear_profile(monkeypatch, eng, "약세", held={"114800"})
    monkeypatch.setattr(engine_mod.orders, "propose", lambda s, q: "oid")
    found = await eng.run_once()
    assert not found[0].get("manual_only")


@pytest.mark.asyncio
async def test_약세_인버스는_별도_비중상한을_쓴다(monkeypatch):
    """일반 상한 25%면 2주에 갇힌다 — 인버스는 40% 상한으로 4주."""
    eng = SignalEngine(equity=1_000_000)
    eng.state.max_positions = 9
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", 25)
    monkeypatch.setitem(settings.RISK, "risk_per_trade_pct", 5.0)
    _prep(monkeypatch, eng, {
        "000100": [_sig("orb", 100_000, 99_000, 103_000)],
        "114800": [_sig("momentum", 100_000, 99_000, 103_000)],
    })
    _bear_profile(monkeypatch, eng, "약세")
    monkeypatch.setattr(engine_mod.orders, "propose", lambda s, q: "oid")
    found = await eng.run_once()
    by = {f["symbol"]: f for f in found}
    assert by["000100"]["qty"] == 2                # 25% = 25만 원 → 2주
    assert by["114800"]["qty"] == 4                # 40% = 40만 원 → 4주


@pytest.mark.asyncio
async def test_중립에서는_약세_프로필이_적용되지_않는다(monkeypatch):
    eng = SignalEngine(equity=1_000_000)
    eng.state.max_positions = 2
    eng.state.open_positions = 1
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", 25)
    monkeypatch.setitem(settings.RISK, "risk_per_trade_pct", 5.0)
    _prep(monkeypatch, eng, {
        "000100": [_sig("orb", 100_000, 99_000, 103_000)],
        "114800": [_sig("momentum", 100_000, 99_000, 103_000)],
    })
    _bear_profile(monkeypatch, eng, "중립")
    monkeypatch.setattr(engine_mod.orders, "propose", lambda s, q: "oid")
    found = await eng.run_once()
    by = {f["symbol"]: f for f in found}
    assert not by["000100"].get("manual_only")     # 예약 없음 — 마지막 자리 사용
    assert by["114800"]["qty"] == 2                # 인버스도 일반 상한 25%
