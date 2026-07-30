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
from app.scout import model, promote, scoring, store
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
async def test_다시_켜진_소스는_첫_수집까지_투영을_막는다(spy_watchlist):
    """장중 소스를 마감 후 끄기 시작하면서 생긴 구멍의 회귀 테스트다.

    다음 날 09:00 에 어제 신호는 TTL 로 만료됐는데 `polled` 에 이름이 남아 있으면
    `ready()` 가 True 가 되고, 장중 소스 점수 0 인 상태로 투영해 **개장 직후
    대량 강등**이 나간다. 종전에는 밤새 폴링했으므로 이 창이 없었다.
    """
    vol = _Src(model.VOLUME, [_sig()])
    e = _engine([vol, _Src(model.NEWS, [_sig(code="000002", source=model.NEWS)])])
    await e.run_once()
    assert e.ready() is True                 # 장중 — 둘 다 폴링했다

    vol._on = False                          # 마감
    await e.run_once()
    assert model.VOLUME not in e.polled, "꺼진 소스는 '보고했다' 로 남지 않는다"
    assert e.ready() is True                 # 꺼진 소스는 ready 를 막지 않는다

    vol._on = True                           # 다음 날 개장
    e.next_due[model.VOLUME] = float("inf")  # 아직 첫 수집 차례가 안 왔다
    out = await e.run_once()
    assert e.ready() is False, "첫 수집 전에 투영하면 장중 점수 0 으로 강등이 나간다"
    assert out["decisions"] == 0 and spy_watchlist == []


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


# --- ⑦ 승격 후보에만 현재가를 실측한다 (레이트리밋) ---
#
# 전 후보에 부르면 30초 사이클마다 27콜(오늘 실측 고유 종목 수)이다.
# 수집 tier 에 있는 것만 매매로 올라갈 수 있으므로 그 집합에만 부른다 —
# 실측 기준 하루 1~2종목.

def _quoter(monkeypatch, calls, fail=()):
    """시세 조회를 가로챈다.

    2026-07-30: 종목당 1콜(ka10006)에서 **묶음 1콜**(ka10095)로 바꿨다. full
    전환 직후 mrkcond 가 분당 72~85콜까지 올라 전체 예산의 절반을 먹었다.
    `calls` 는 이제 '조회된 종목' 을 그대로 담는다 — 호출 횟수가 아니라 어느
    종목이 조회됐는지가 이 테스트들이 확인하려던 것이다.
    """
    from app.kiwoom import client as kc

    async def wi(codes):
        calls.extend(codes)
        if any(c in fail for c in codes):
            raise RuntimeError("조회 실패")
        return {"return_code": 0, "atn_stk_infr": [
            {"stk_cd": c, "stk_nm": f"종목{c}", "cur_prc": "-10100",
             "flu_rt": "-1.5", "cntr_str": "88.8", "trde_qty": "1000",
             "trde_prica": "5000"} for c in codes]}

    monkeypatch.setattr(kc.client, "watch_info", wi)
    monkeypatch.setattr(settings, "KIWOOM_APP_KEY", "K")
    # 장중이어야 한다 — 밖이면 아무것도 안 부르고 테스트가 공허해진다
    monkeypatch.setattr(promote, "in_session", lambda now=None: True)


@pytest.mark.asyncio
async def test_quotes_are_fetched_only_for_promotion_candidates(monkeypatch):
    calls = []
    _quoter(monkeypatch, calls)
    e = _engine([_Src(model.VOLUME, [_sig("000001"), _sig("000002")])])
    await e.collect_due()
    e.candidates = scoring.aggregate(store.live(), datetime.now(UTC))
    cur = promote.Current(tier={"000001": promote.COLLECT}, since={},
                          protected=frozenset(), names={})
    assert await e.refresh_quotes(cur) == 1
    assert calls == ["000001"]          # 수집 tier 인 것 하나만


@pytest.mark.asyncio
async def test_no_quote_calls_outside_the_session(monkeypatch):
    calls = []
    _quoter(monkeypatch, calls)
    monkeypatch.setattr(promote, "in_session", lambda now=None: False)
    e = _engine([_Src(model.VOLUME, [_sig("000001")])])
    await e.collect_due()
    e.candidates = scoring.aggregate(store.live(), datetime.now(UTC))
    cur = promote.Current(tier={"000001": promote.COLLECT}, since={},
                          protected=frozenset(), names={})
    assert await e.refresh_quotes(cur) == 0 and calls == []


@pytest.mark.asyncio
async def test_a_failed_quote_leaves_the_candidate_stale(monkeypatch):
    """못 받으면 승격을 미룬다 — 틀린 값으로 매매를 여는 것보다 싸다."""
    calls = []
    _quoter(monkeypatch, calls, fail={"000001"})
    e = _engine([_Src(model.NIGHTLY, [_sig("000001", source=model.NIGHTLY)])])
    await e.collect_due()
    e.candidates = scoring.aggregate(store.live(), datetime.now(UTC))
    cur = promote.Current(tier={"000001": promote.COLLECT}, since={},
                          protected=frozenset(), names={})
    assert await e.refresh_quotes(cur) == 0
    assert e.candidates[0].quote is None
    assert e.candidates[0].price_fresh is False      # 게이트가 보류한다
