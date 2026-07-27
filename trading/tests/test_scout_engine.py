"""중앙 관제 루프 — 모드가 실제로 쓰기를 막는가.

가장 중요한 회귀 감지는 **shadow 에서 감시목록에 쓰지 않는가** 다. 이 계획의
핵심 안전장치가 그것이고, 3거래일 관찰이 성립하려면 그 보장이 필요하다.

그다음이 재시작 직후 폭주 방어 — 저장소가 비어 있으면 "감시목록 전체 삭제"
diff 가 나온다.
"""
from datetime import UTC, datetime, timedelta

import pytest

from app import settings
from app.scout import engine as eng
from app.scout import model, promote, store
from app.scout.model import Signal

NOW = datetime(2026, 7, 27, 1, 0, tzinfo=UTC)      # 월 10:00 KST — 장중


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "scout.db")
    monkeypatch.setattr(eng, "STATE_FILE", tmp_path / "engine.json")
    monkeypatch.setattr(settings, "tradable_price_cap", lambda d: 30_000.0)


class _Src:
    """제어 가능한 가짜 소스."""

    def __init__(self, name, signals=None, interval=60, on=True, boom=False):
        self.name = name
        self._sig = signals or []
        self._iv = interval
        self._on = on
        self.boom = boom
        self.calls = 0

    def enabled(self):
        return self._on

    def interval_sec(self):
        return self._iv

    async def collect(self):
        self.calls += 1
        if self.boom:
            raise RuntimeError("boom")
        return list(self._sig)


def _sig(code="000001", source=model.VOLUME, strength=0.9, price=10_000):
    return Signal(code=code, name=f"종목{code}", source=source, strength=strength,
                  price=price, observed_at=datetime.now(UTC))


def _engine(sources):
    e = eng.Engine()
    e.sources = sources
    return e


@pytest.fixture
def spy_watchlist(monkeypatch):
    """감시목록 쓰기를 전부 가로챈다."""
    from app.data import watchlist as wl

    calls = []
    monkeypatch.setattr(wl, "entries", lambda: [])
    monkeypatch.setattr(wl, "add", lambda *a, **k: calls.append(("add", a, k)))
    monkeypatch.setattr(wl, "remove", lambda c: calls.append(("remove", c)))
    monkeypatch.setattr(wl, "set_mode", lambda c, collect_only: calls.append(
        ("set_mode", c, collect_only)))

    async def _notify():
        calls.append(("notify",))

    monkeypatch.setattr(wl, "notify", _notify)
    monkeypatch.setattr(eng, "snapshot_current", lambda: promote.Current(
        tier={}, since={}, protected=frozenset(), names={}))
    return calls


# --- ① shadow 는 쓰지 않는다 ---

@pytest.mark.asyncio
async def test_shadow_never_writes_to_the_watchlist(spy_watchlist):
    """이 계획의 핵심 안전장치. 여기가 깨지면 3거래일 관찰이 무의미해진다."""
    e = _engine([_Src(model.VOLUME, [_sig()])])
    out = await e.run_once()
    assert eng.mode() == "shadow"
    assert out["decisions"] >= 1 and out["applied"] == 0
    assert spy_watchlist == []


@pytest.mark.asyncio
async def test_shadow_still_records_decisions(spy_watchlist):
    """적용하지 않은 결정이야말로 '엔진이라면 이렇게 했을 것' 의 기록이다."""
    await _engine([_Src(model.VOLUME, [_sig()])]).run_once()
    rows = store.recent_decisions()
    assert rows and rows[0]["mode"] == "shadow" and rows[0]["applied"] == 0


@pytest.mark.asyncio
async def test_collect_mode_does_not_touch_trade_tier(spy_watchlist, monkeypatch):
    """매매 tier 는 아직 기존 경로 소유다."""
    eng.set_state(mode="collect")
    monkeypatch.setattr(eng, "snapshot_current", lambda: promote.Current(
        tier={"000001": promote.COLLECT},
        since={"000001": datetime.now(UTC) - timedelta(hours=2)},
        protected=frozenset(), names={}))
    await _engine([_Src(model.VOLUME, [_sig(price=12_000)])]).run_once()
    assert not [c for c in spy_watchlist if c[0] == "set_mode" and c[2] is False]


@pytest.mark.asyncio
async def test_full_mode_applies(spy_watchlist):
    eng.set_state(mode="full")
    out = await _engine([_Src(model.VOLUME, [_sig()])]).run_once()
    assert out["applied"] >= 1
    assert any(c[0] == "add" for c in spy_watchlist)
    assert any(c[0] == "notify" for c in spy_watchlist)


@pytest.mark.asyncio
async def test_freeze_stops_writing_but_keeps_watching(spy_watchlist):
    """킬 스위치 — 쓰기만 멈추고 감시목록은 현 상태로 동결한다."""
    eng.set_state(mode="full", frozen=True)
    out = await _engine([_Src(model.VOLUME, [_sig()])]).run_once()
    assert out["decisions"] >= 1 and out["applied"] == 0
    assert spy_watchlist == []


def test_unknown_mode_rejected():
    with pytest.raises(ValueError):
        eng.set_state(mode="없는모드")


def test_mode_falls_back_to_shadow_on_corrupt_state(monkeypatch, tmp_path):
    f = tmp_path / "engine.json"
    f.write_text("{ 깨진 json", encoding="utf-8")
    monkeypatch.setattr(eng, "STATE_FILE", f)
    assert eng.mode() == "shadow"


# --- ② 재시작 직후 폭주 방어 ---

@pytest.mark.asyncio
async def test_no_projection_until_every_source_polled_once(spy_watchlist):
    """저장소가 비면 '감시목록 전체 삭제' diff 가 나온다 — 재시작 직후가 그 상태다."""
    slow = _Src(model.NEWS, [], interval=900, boom=True)
    e = _engine([_Src(model.VOLUME, [_sig()]), slow])
    out = await e.run_once()
    assert e.ready() is False
    assert out["decisions"] == 0 and "대기" in out["reason"]
    assert spy_watchlist == []


@pytest.mark.asyncio
async def test_disabled_sources_do_not_block_readiness(spy_watchlist):
    e = _engine([_Src(model.VOLUME, [_sig()]), _Src(model.NEWS, [], on=False)])
    await e.run_once()
    assert e.ready() is True


@pytest.mark.asyncio
async def test_shrink_circuit_breaker(monkeypatch, spy_watchlist):
    """한 번에 대량 축소는 대개 소스 장애다 — 상한을 둔다."""
    monkeypatch.setattr(eng, "MAX_SHRINK", 3)
    codes = [f"00000{i}" for i in range(1, 9)]
    monkeypatch.setattr(eng, "snapshot_current", lambda: promote.Current(
        tier={c: promote.COLLECT for c in codes},
        since={c: NOW - timedelta(hours=2) for c in codes},
        protected=frozenset(), names={c: c for c in codes}))
    e = _engine([_Src(model.VOLUME, [])])
    rows = e.project(NOW)          # 강등은 장중에만 — 시각을 못박는다
    assert len([r for r in rows if r["to_tier"] == promote.NONE]) == 3


# --- ③ 어댑터 격리와 백오프 ---

@pytest.mark.asyncio
async def test_one_failing_source_does_not_block_others():
    bad, good = _Src(model.NEWS, boom=True), _Src(model.VOLUME, [_sig()])
    got = await _engine([bad, good]).collect_due(now=0.0)
    assert got == 1                      # 좋은 소스는 정상 수집


@pytest.mark.asyncio
async def test_consecutive_failures_back_off():
    bad = _Src(model.NEWS, boom=True, interval=60)
    e = _engine([bad])
    await e.collect_due(now=0.0)
    first = e.next_due[model.NEWS]
    await e.collect_due(now=first)
    second = e.next_due[model.NEWS] - first
    assert second > first > 0            # 주기가 점점 길어진다


@pytest.mark.asyncio
async def test_success_after_failure_restores_normal_interval():
    src = _Src(model.VOLUME, [_sig()], boom=True, interval=60)
    e = _engine([src])
    await e.collect_due(now=0.0)
    src.boom = False
    await e.collect_due(now=e.next_due[model.VOLUME])
    assert store.health()[model.VOLUME]["fails"] == 0


@pytest.mark.asyncio
async def test_source_not_polled_before_due():
    src = _Src(model.VOLUME, [_sig()], interval=60)
    e = _engine([src])
    await e.collect_due(now=0.0)
    await e.collect_due(now=30.0)        # 아직 due 아님
    assert src.calls == 1
    await e.collect_due(now=61.0)
    assert src.calls == 2


@pytest.mark.asyncio
async def test_disabled_source_is_skipped():
    src = _Src(model.VOLUME, [_sig()], on=False)
    await _engine([src]).collect_due(now=0.0)
    assert src.calls == 0


# --- ④ 상태 ---

@pytest.mark.asyncio
async def test_status_exposes_source_health(spy_watchlist):
    e = _engine([_Src(model.VOLUME, [_sig()]), _Src(model.NEWS, boom=True)])
    await e.run_once()
    st = e.status()
    assert st["mode"] == "shadow"
    by = {s["name"]: s for s in st["sources"]}
    assert by[model.VOLUME]["polled"] is True
    assert by[model.NEWS]["fails"] >= 1


def test_snapshot_marks_held_and_manual_protected(monkeypatch):
    from app.data import watchlist as wl

    monkeypatch.setattr(wl, "entries", lambda: [
        {"code": "000001", "name": "가", "source": "seed", "added": "2026-01-01"},
        {"code": "000002", "name": "나", "source": "gainer", "added": "2026-01-01",
         "collect_only": 1},
        {"code": "000003", "name": "다", "source": "scout", "added": "2026-01-01"},
    ])
    import app.trade.ledger as ledger

    monkeypatch.setattr(ledger, "open_symbols", lambda: {"000003"})
    cur = eng.snapshot_current()
    assert cur.protected == frozenset({"000001", "000003"})
    assert cur.tier == {"000001": promote.TRADE, "000002": promote.COLLECT,
                        "000003": promote.TRADE}


def test_snapshot_protects_everything_when_ledger_fails(monkeypatch):
    """보유 조회 실패 시 아무것도 빼지 않는 쪽이 안전하다."""
    from app.data import watchlist as wl

    monkeypatch.setattr(wl, "entries", lambda: [
        {"code": "000001", "name": "가", "source": "scout", "added": "2026-01-01"}])
    import app.trade.ledger as ledger

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(ledger, "open_symbols", _boom)
    assert eng.snapshot_current().protected == frozenset({"000001"})
