"""일일 가드의 적용 범위.

설계: 일일 목표·손실 한도는 **완전 자동 발주를 위한 안전장치**다. 사람이 개입하지
않고 주문이 나가는 모드에서만 신규 진입을 끊는다. 자동 발주를 끄면 모든 주문이
사용자 승인을 거치므로 가드는 경고만 하고 진입을 막지 않는다(사용자 책임).

또한 가드는 '매매'만 멈춘다 — 분봉 수집은 계속되어야 한다(차트·백테스트·다음 날
지표가 이 데이터를 쓴다). (회귀: 수집이 가드 뒤에 있어 함께 멈추던 구조)
"""
import types
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import settings
from app.signals import engine as engine_mod
from app.signals.engine import SignalEngine
from app.signals.rules import Signal

KST = ZoneInfo("Asia/Seoul")
HALTED = {"halted": True, "reason": "일일 손실 한도(-1.5%) 도달 — 신규 진입 중단",
          "pct": -1.86}
OK = {"halted": False, "reason": "", "pct": 0.3}


def _prep(monkeypatch, eng, guard, sig=None):
    """run_once 의존성 고정. backfilled 리스트로 수집 여부를 관찰한다."""
    monkeypatch.setattr(settings, "WATCHLIST", {"005930": "삼성전자"})
    monkeypatch.setattr(settings, "COLLECT_ONLY", set())
    monkeypatch.setattr(settings, "RULES", {"orb": {"enabled": True}})
    eng.equity_synced = True
    eng._fired_restored = True
    backfilled: list[str] = []

    async def _noop_sync():
        return None

    async def fake_backfill(sym):
        backfilled.append(sym)

    async def _noop_send(order_id):
        return {"status": "sent", "message": ""}

    monkeypatch.setattr(eng, "_sync_equity", _noop_sync)
    monkeypatch.setattr(eng, "_sync_open_positions", lambda: None)
    monkeypatch.setattr(eng, "_effective_regime", lambda: "중립")
    monkeypatch.setattr(eng, "day_guard_status", lambda: dict(guard))
    monkeypatch.setattr(eng, "_today_df",
                        lambda s: (types.SimpleNamespace(empty=False), None))
    monkeypatch.setattr(eng, "_rules_for", lambda s: settings.RULES)
    monkeypatch.setattr(engine_mod.collector, "backfill_minutes", fake_backfill)
    monkeypatch.setattr(engine_mod.orders, "approve_and_send", _noop_send)
    monkeypatch.setattr(engine_mod.rules, "evaluate_all",
                        lambda df, cfg, prev: [sig] if sig else [])
    return backfilled


def _sig():
    return Signal(rule="orb", side="long", entry=10_000, stop=9_800,
                  target=10_400, reason="테스트")


# --- 수집은 가드와 무관하게 계속된다 ---

@pytest.mark.asyncio
async def test_backfill_runs_even_when_guard_halts(monkeypatch):
    eng = SignalEngine(equity=1_000_000)
    monkeypatch.setitem(settings.RISK, "auto_approve", True)
    backfilled = _prep(monkeypatch, eng, HALTED, _sig())

    assert await eng.run_once() == []          # 신호는 나가지 않고
    assert backfilled == ["005930"]            # 분봉 수집은 돌았다


@pytest.mark.asyncio
async def test_backfill_runs_even_when_equity_unsynced(monkeypatch):
    """잔고 조회 실패로 매매가 보류돼도 수집은 이어진다."""
    eng = SignalEngine(equity=1_000_000)
    backfilled = _prep(monkeypatch, eng, OK, _sig())
    eng.equity_synced = False

    assert await eng.run_once() == []
    assert backfilled == ["005930"]


# --- 가드는 자동 발주 모드에서만 진입을 막는다 ---

@pytest.mark.asyncio
async def test_guard_blocks_entries_in_auto_mode(monkeypatch):
    eng = SignalEngine(equity=1_000_000)
    monkeypatch.setitem(settings.RISK, "auto_approve", True)
    _prep(monkeypatch, eng, HALTED, _sig())
    sent = []
    monkeypatch.setattr(engine_mod.orders, "propose",
                        lambda s, q: (sent.append(s.symbol), "oid")[1])

    assert await eng.run_once() == []
    assert sent == []                           # 주문 자체가 만들어지지 않는다


@pytest.mark.asyncio
async def test_guard_allows_manual_approval_when_auto_off(monkeypatch):
    """자동 발주를 끄면 가드가 걸려도 승인대기 주문이 생긴다(사용자 책임)."""
    eng = SignalEngine(equity=1_000_000)
    monkeypatch.setitem(settings.RISK, "auto_approve", False)
    _prep(monkeypatch, eng, HALTED, _sig())
    sent = []
    monkeypatch.setattr(engine_mod.orders, "propose",
                        lambda s, q: (sent.append(s.symbol), "oid")[1])

    found = await eng.run_once()
    assert len(found) == 1 and found[0]["actionable"] is True
    assert sent == ["005930"]
    assert "일일 손실 한도" in found[0]["guard_warn"]   # 경고는 남는다
    assert "auto_status" not in found[0]               # 자동 발주는 안 한다


@pytest.mark.asyncio
async def test_no_guard_warn_when_not_halted(monkeypatch):
    eng = SignalEngine(equity=1_000_000)
    monkeypatch.setitem(settings.RISK, "auto_approve", False)
    _prep(monkeypatch, eng, OK, _sig())
    monkeypatch.setattr(engine_mod.orders, "propose", lambda s, q: "oid")

    found = await eng.run_once()
    assert "guard_warn" not in found[0]


# --- 화면 표시 ---

def _at(monkeypatch, dt):
    import app.signals.engine as eng_mod

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return dt

    monkeypatch.setattr(eng_mod, "datetime", FakeDT)
    monkeypatch.setattr(settings, "KIWOOM_APP_KEY", "K")


def test_status_shows_halted_only_in_auto_mode(monkeypatch):
    eng = SignalEngine()
    eng.guard = dict(HALTED)
    _at(monkeypatch, datetime(2026, 7, 27, 10, 30, tzinfo=KST))

    monkeypatch.setitem(settings.RISK, "auto_approve", True)
    s = eng.market_status()
    assert s["phase"] == "halted" and s["entries_blocked"] is True
    assert s["scanning"] is False          # 신호 평가는 멈춤
    assert s["collecting"] is True         # 수집은 계속 — 배너가 구분해 알린다
    assert s["label"] == HALTED["reason"]   # 사유 그대로 (문구 중복 없이)
    assert s["label"].count("신규 진입 중단") == 1
    assert s["guard"]["manual_override"] is False

    monkeypatch.setitem(settings.RISK, "auto_approve", False)
    s = eng.market_status()
    assert s["phase"] == "open" and s["entries_blocked"] is False
    assert s["scanning"] is True           # 수동 승인 모드 — 계속 감시
    assert s["guard"]["halted"] is True    # 도달 사실은 그대로 알림
    assert s["guard"]["manual_override"] is True


def test_status_normal_when_guard_clear(monkeypatch):
    eng = SignalEngine()
    eng.guard = dict(OK)
    monkeypatch.setitem(settings.RISK, "auto_approve", True)
    _at(monkeypatch, datetime(2026, 7, 27, 10, 30, tzinfo=KST))
    s = eng.market_status()
    assert s["phase"] == "open" and s["scanning"] is True
    assert s["entries_blocked"] is False and s["guard"]["halted"] is False


def test_status_guard_does_not_override_closed(monkeypatch):
    """장이 닫혀 있으면 가드보다 '마감'이 먼저다(오해 방지)."""
    eng = SignalEngine()
    eng.guard = dict(HALTED)
    monkeypatch.setitem(settings.RISK, "auto_approve", True)
    _at(monkeypatch, datetime(2026, 7, 27, 16, 30, tzinfo=KST))
    s = eng.market_status()
    assert s["phase"] == "closed" and s["collecting"] is False


def test_status_without_guard_snapshot(monkeypatch):
    """첫 사이클 전(guard 미계산)에도 예외 없이 정상 상태를 보고한다."""
    eng = SignalEngine()
    _at(monkeypatch, datetime(2026, 7, 27, 10, 30, tzinfo=KST))
    s = eng.market_status()
    assert s["phase"] == "open" and s["guard"]["halted"] is False
