"""감시 신호(금액 제한 없음) vs 승인대기 주문(잔고 참고) 분리 동작 테스트."""
import types

import pytest

from app import settings
from app.signals import engine as engine_mod
from app.signals.engine import SignalEngine
from app.signals.rules import Signal


def _prep(monkeypatch, eng, sig):
    """run_once 의존성(잔고 동기화·가드·데이터·규칙)을 테스트용으로 고정."""
    monkeypatch.setattr(settings, "WATCHLIST", {"005930": "삼성전자"})
    eng.equity_synced = True
    eng._fired_restored = True          # 주문 이력 DB(로컬 아티팩트)에 의존하지 않게

    async def _noop_sync():
        return None

    async def _noop_backfill(sym):
        return None

    # 기본값도 모킹한다 — 대기열 무조건 생성(2026-08-03) 이후에는 qty 0 경로도
    # propose 를 부르므로, 안 걸면 테스트가 실 DB 에 주문을 남긴다(실측: 이
    # 오염이 같은 파일의 뒤 테스트 4건을 _fired 복원으로 전멸시켰다).
    monkeypatch.setattr(engine_mod.orders, "propose", lambda s, q: "oid-fix")

    async def _noop_send(order_id, qty=None):
        return {"status": "sent", "message": ""}

    monkeypatch.setattr(engine_mod.orders, "approve_and_send", _noop_send)
    monkeypatch.setattr(eng, "_sync_equity", _noop_sync)
    monkeypatch.setattr(eng, "day_guard_status",
                        lambda: {"halted": False, "reason": "", "pct": 0.0})
    monkeypatch.setattr(eng, "_today_df",
                        lambda sym: (types.SimpleNamespace(empty=False), None))
    monkeypatch.setattr(eng, "_rules_for", lambda sym: {})
    monkeypatch.setattr(engine_mod.collector, "backfill_minutes", _noop_backfill)
    # 진입 시간창 게이트를 무력화 — 벽시계를 읽어 15:00 이후 실행 시 전 신호가
    # 차단돼 테스트가 시간대에 따라 갈린다(실측 2026-08-03 15:47). 창 동작
    # 자체는 test_entry_window 가 시각을 주입해 검증한다.
    monkeypatch.setattr(engine_mod.rules, "entry_window_note",
                        lambda rule, now, cfg: None)
    monkeypatch.setattr(engine_mod.rules, "evaluate_all",
                        lambda df, cfg, prev: [sig])


@pytest.mark.asyncio
async def test_affordable_signal_creates_pending_order(monkeypatch):
    eng = SignalEngine(equity=10_000_000)
    sig = Signal(rule="orb", side="long", entry=10_000, stop=9_800,
                 target=10_400, reason="테스트")
    _prep(monkeypatch, eng, sig)
    calls = []
    monkeypatch.setattr(engine_mod.orders, "propose",
                        lambda s, q: (calls.append(q), "oid1")[1])

    found = await eng.run_once()
    assert len(found) == 1
    assert found[0]["actionable"] is True
    assert found[0]["order_id"] == "oid1"
    assert calls and calls[0] >= 1                  # 잔고 충분 → 발주 수량 제안
    assert eng.last_signals[0]["symbol"] == "005930"


@pytest.mark.asyncio
async def test_qty_zero_note_risk_vs_balance(monkeypatch):
    # 저가주(HMM 케이스): 매수여력은 충분하나 손절폭이 리스크 한도보다 넓어 0주.
    eng = SignalEngine(equity=142_589)
    sig = Signal(rule="orb", side="long", entry=21_000, stop=20_100,
                 target=22_000, reason="x")  # dist=900 > 0.5%예산 713; 여력=6주
    _prep(monkeypatch, eng, sig)
    found = await eng.run_once()
    # 대기열 무조건 생성(2026-08-03): 0주로 삼키지 않고 1주 제안 + 수동 승인만
    assert found[0]["qty"] == 1
    assert found[0].get("manual_only") is True
    assert "리스크 한도" in found[0]["note"]      # 잔고 문제가 아님을 명시
    assert "잔고 부족" not in found[0]["note"]


@pytest.mark.asyncio
async def test_collect_only_skipped_but_backfilled(monkeypatch):
    eng = SignalEngine(equity=10_000_000)
    eng.equity_synced = True
    monkeypatch.setattr(settings, "WATCHLIST",
                        {"005930": "삼성전자", "006400": "삼성SDI"})
    monkeypatch.setattr(settings, "COLLECT_ONLY", {"006400"})
    monkeypatch.setitem(settings.CONFIG, "collection",
                        {"realtime_trading_only": True})

    async def _noop_sync():
        return None

    backfilled, evaluated = [], []

    async def fake_backfill(sym):
        backfilled.append(sym)

    monkeypatch.setattr(eng, "_sync_equity", _noop_sync)
    monkeypatch.setattr(eng, "_effective_regime", lambda: "중립")  # 갭바이어스가 _today_df 안 부르게
    monkeypatch.setattr(eng, "day_guard_status",
                        lambda: {"halted": False, "reason": "", "pct": 0.0})
    monkeypatch.setattr(eng, "_today_df",
                        lambda s: (evaluated.append(s),
                                   (types.SimpleNamespace(empty=False), None))[1])
    monkeypatch.setattr(eng, "_rules_for", lambda s: {})
    monkeypatch.setattr(engine_mod.collector, "backfill_minutes", fake_backfill)
    monkeypatch.setattr(engine_mod.rules, "evaluate_all", lambda df, cfg, prev: [])

    await eng.run_once()
    # 장중 실시간 백필은 매매 대상만 — 수집전용은 마감 백필(eod_backfill_once)에서
    # 하루치를 통째로 받는다(분봉 API 1회 = 900봉 ≈ 2.3거래일).
    assert backfilled == ["005930"]
    assert evaluated == ["005930"]                    # 매매 평가도 006400 제외


@pytest.mark.asyncio
async def test_long_only_blocks_short_orders(monkeypatch):
    # 롱 전용 모드: 숏 신호는 기록만 하고 발주하지 않는다(잔고 충분해도).
    monkeypatch.setitem(settings.RISK, "long_only", True)
    eng = SignalEngine(equity=10_000_000)
    sig = Signal(rule="orb", side="short", entry=10_000, stop=10_200,
                 target=9_400, reason="하락 이탈")
    _prep(monkeypatch, eng, sig)
    calls = []
    monkeypatch.setattr(engine_mod.orders, "propose",
                        lambda s, q: (calls.append(q), "oid")[1])

    found = await eng.run_once()
    assert len(found) == 1
    assert found[0]["actionable"] is False
    assert "롱 전용" in found[0]["note"]
    assert calls == []                              # 숏은 발주되지 않는다


@pytest.mark.asyncio
async def test_long_only_allows_long_orders(monkeypatch):
    monkeypatch.setitem(settings.RISK, "long_only", True)
    eng = SignalEngine(equity=10_000_000)
    sig = Signal(rule="orb", side="long", entry=10_000, stop=9_800,
                 target=10_400, reason="롱 신호")
    _prep(monkeypatch, eng, sig)
    calls = []
    monkeypatch.setattr(engine_mod.orders, "propose",
                        lambda s, q: (calls.append(q), "oidL")[1])

    found = await eng.run_once()
    assert found[0]["actionable"] is True and calls    # 롱은 정상 발주


@pytest.mark.asyncio
async def test_unaffordable_signal_still_reaches_the_queue(monkeypatch):
    """자산 142,589원으로 44만원짜리 종목 — 1주도 못 사지만 **대기열에는 오른다.**

    사용자 결정(2026-08-03): 계좌가 발주 불가능한 상태여도 승인 대기열에는
    무조건 올린다. 자동발주만 제외(manual_only) — 입금 후 수동 승인 가능.
    """
    eng = SignalEngine(equity=142_589)
    sig = Signal(rule="orb", side="long", entry=441_500, stop=428_500,
                 target=460_000, reason="고가주 신호")
    _prep(monkeypatch, eng, sig)
    calls = []
    monkeypatch.setattr(engine_mod.orders, "propose",
                        lambda s, q: (calls.append(q), "oid")[1])
    auto = []

    async def _spy(order_id, qty=None):
        auto.append(order_id)
        return {"status": "sent", "message": ""}

    monkeypatch.setattr(engine_mod.orders, "approve_and_send", _spy)
    found = await eng.run_once()
    assert len(found) == 1
    assert found[0]["qty"] == 1                     # 1주로 제안
    assert found[0]["actionable"] is True
    assert found[0]["order_id"] == "oid"
    assert found[0]["fundable"] is False            # 살 수 없다는 사실은 표시
    assert found[0].get("manual_only") is True
    assert calls == [1] and auto == []              # 대기열 생성, 자동발주 없음
