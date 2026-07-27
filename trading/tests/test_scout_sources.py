"""어댑터 — 각 발굴 기능이 신호를 낼 뿐 **감시목록에 쓰지 않는다**.

여기서 지키는 것 셋:
  1. 어댑터가 감시목록을 건드리지 않는다 (shadow 안전의 전제)
  2. **이미 감시 중인 종목도 계속 보고한다** — 안 그러면 승격/강등 발진이 난다
  3. 조회가 전부 실패하면 '빈 결과' 가 아니라 **예외**다 — 빈 결과로 보고하면
     TTL 이 무의미해지고 목록이 증발한다
"""
import pytest

from app import settings
from app.scout import model
from app.scout.sources import intraday, slow


class _FakeClient:
    def __init__(self, rank=None, surge=None, fail=()):
        self._rank = rank or {}
        self._surge = surge or {}
        self._fail = set(fail)

    async def trade_value_rank(self, market):
        if "value" in self._fail:
            raise RuntimeError("boom")
        return self._rank

    async def change_rate_rank(self, market):
        if market in self._fail or "rate" in self._fail:
            raise RuntimeError(f"boom {market}")
        return self._rank

    async def volume_surge_rank(self, market):
        if "surge" in self._fail:
            raise RuntimeError("boom")
        return self._surge


def _rank_payload(codes, change=5.0, price=10_000, tv=999_999):
    return {"rows": [{"stk_cd": c, "stk_nm": f"종목{c}", "cur_prc": str(price),
                      "flu_rt": str(change), "trde_prica": str(tv),
                      "now_trde_qty": "1000000"} for c in codes]}


def _surge_payload(codes, surge=800.0, change=1.0, price=10_000):
    return {"trde_qty_sdnin": [
        {"stk_cd": c, "stk_nm": f"종목{c}", "cur_prc": str(price),
         "flu_rt": str(change), "sdnin_rt": str(surge),
         "now_trde_qty": "500000"} for c in codes]}


@pytest.fixture
def fake_client(monkeypatch):
    """kiwoom.client 를 갈아 끼운다 — 네트워크 없이 어댑터만 잰다."""
    holder = {}

    def install(**kw):
        import app.kiwoom.client as mod

        holder["c"] = _FakeClient(**kw)
        monkeypatch.setattr(mod, "client", holder["c"])
        return holder["c"]

    return install


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setattr(settings, "KIWOOM_APP_KEY", "test-key")


@pytest.fixture(autouse=True)
def _clean_watchlist(monkeypatch):
    monkeypatch.setattr(settings, "WATCHLIST", {})


# --- ① 감시목록에 쓰지 않는다 ---

@pytest.mark.asyncio
async def test_adapters_never_touch_the_watchlist(fake_client, monkeypatch):
    """shadow 안전의 전제. 어댑터가 replace_* 를 부르면 그 자체가 사고다."""
    import app.data.watchlist as wl

    called = []
    for fn in ("replace_active", "replace_gainers", "replace_auto",
               "replace_scanned", "add", "remove"):
        monkeypatch.setattr(wl, fn, lambda *a, _f=fn, **k: called.append(_f))
    fake_client(rank=_rank_payload(["000001", "000002"]),
                surge=_surge_payload(["000003"]))
    for src in (intraday.VolumeSource(), intraday.GainersSource(),
                intraday.PresurgeSource()):
        await src.collect()
    assert called == []


# --- ② 이미 감시 중이어도 계속 보고한다 (발진 방지) ---

@pytest.mark.asyncio
async def test_watched_symbols_are_still_reported(fake_client, monkeypatch):
    """소스가 보고를 멈추면 TTL 만료 → 강등 → 다시 보고 → 승격 발진이 난다.

    히스테리시스로 못 막는다 — 히스테리시스는 신호가 진동할 때 유효하지
    신호가 사라질 때는 무력하다.
    """
    monkeypatch.setattr(settings, "WATCHLIST", {"000001": "종목000001"})
    fake_client(rank=_rank_payload(["000001", "000002"]),
                surge=_surge_payload(["000001", "000003"]))
    vol = await intraday.VolumeSource().collect()
    gai = await intraday.GainersSource().collect()
    pre = await intraday.PresurgeSource().collect()
    for got in (vol, gai, pre):
        assert "000001" in {s.code for s in got}


# --- ③ 조회 실패를 '결과 없음' 으로 보고하지 않는다 ---

@pytest.mark.asyncio
async def test_all_markets_failing_raises(fake_client, monkeypatch):
    """전 시장 실패를 빈 목록으로 보고하면 tier 가 통째로 증발한다.

    지금 코드가 정확히 그 상태다 — API 1회 실패 → filter_gainers 가 [] →
    replace_gainers([]) → 감시목록에서 gainer 전체 삭제.
    """
    monkeypatch.setitem(settings.CONFIG, "gainers",
                        dict(settings.CONFIG.get("gainers", {}), markets=["001", "101"]))
    fake_client(rank=_rank_payload(["000001"]), fail=("rate",))
    with pytest.raises(RuntimeError):
        await intraday.GainersSource().collect()


@pytest.mark.asyncio
async def test_partial_market_failure_still_reports(fake_client, monkeypatch):
    """한 시장이 죽어도 나머지는 낸다 — 전부 죽었을 때만 예외다."""
    monkeypatch.setitem(settings.CONFIG, "gainers",
                        dict(settings.CONFIG.get("gainers", {}), markets=["001", "101"]))
    fake_client(rank=_rank_payload(["000001"]), fail=("101",))
    got = await intraday.GainersSource().collect()
    assert {s.code for s in got} == {"000001"}


# --- ④ 정규화 ---

@pytest.mark.asyncio
async def test_rank_strength_follows_list_order(fake_client):
    fake_client(rank=_rank_payload(["000001", "000002", "000003"]))
    got = await intraday.VolumeSource().collect()
    assert got[0].strength == 1.0            # 1위
    assert got[-1].strength == 0.0           # 꼴찌
    assert [s.source for s in got] == [model.VOLUME] * 3


@pytest.mark.asyncio
async def test_presurge_uses_surge_rate_not_rank(fake_client):
    """목록 길이에 따라 세기가 흔들리면 안 된다 — 급증률 자체를 쓴다."""
    fake_client(surge=_surge_payload(["000001"], surge=3160.0))
    got = await intraday.PresurgeSource().collect()
    assert got[0].strength == pytest.approx(1.0, abs=0.01)
    assert got[0].raw == 3160.0


@pytest.mark.asyncio
async def test_signals_carry_price_for_tradability_gate(fake_client):
    """매매 tier 승격은 체결가능성으로 판정한다 — 가격이 실려야 한다."""
    fake_client(rank=_rank_payload(["000001"], price=412_000))
    got = await intraday.VolumeSource().collect()
    assert got[0].price == 412_000


# --- ⑤ 느린 소스 ---

@pytest.mark.asyncio
async def test_nightly_consumes_existing_picks(monkeypatch):
    """발굴을 다시 돌리지 않는다 — 3,900종목 배치는 엔진 주기로 못 돈다."""
    import app.discovery as disc

    ran = []
    monkeypatch.setattr(disc, "latest_picks", lambda: ("2026-07-27", [
        {"code": "000001", "name": "가", "score": 3, "close": 5_000,
         "reasons": ["거래량급증"]},
        {"code": "000002", "name": "나", "score": 2, "close": 7_000, "reasons": []}]))
    monkeypatch.setattr(disc.Discovery, "run_once",
                        lambda self: ran.append(1))     # 불리면 안 된다
    got = await slow.NightlySource().collect()
    assert ran == []
    assert [s.strength for s in got] == [1.0, pytest.approx(2 / 3)]
    assert got[0].evidence["reasons"] == ["거래량급증"]


@pytest.mark.asyncio
async def test_nightly_empty_before_first_batch(monkeypatch):
    import app.discovery as disc

    monkeypatch.setattr(disc, "latest_picks", lambda: (None, []))
    assert await slow.NightlySource().collect() == []


@pytest.mark.asyncio
async def test_news_skips_negative_impact(monkeypatch):
    """악재는 매수 후보가 아니다."""
    payload = {"items": [
        {"ticker": "000001", "name": "가", "score": 80, "category": "공급계약",
         "impact_direction": "positive", "title": "수주", "url": "u"},
        {"ticker": "000002", "name": "나", "score": 90, "category": "소송",
         "impact_direction": "negative", "title": "피소", "url": "u"},
        {"ticker": "", "name": "다", "score": 95, "impact_direction": "positive"},
    ]}
    _install_httpx(monkeypatch, payload)
    monkeypatch.setattr(settings, "TNM_TOKEN", "tnm-token")
    got = await slow.NewsSource().collect()
    assert [s.code for s in got] == ["000001"]
    assert got[0].strength == pytest.approx(0.8)
    assert got[0].kind == "news:공급계약"


@pytest.mark.asyncio
async def test_news_asks_for_the_right_status(monkeypatch):
    """'done' 은 존재하지 않는 상태값이라 조용히 0건이 온다 — 실제로 그랬다."""
    seen = {}
    _install_httpx(monkeypatch, {"items": []}, seen)
    monkeypatch.setattr(settings, "TNM_TOKEN", "t")
    await slow.NewsSource().collect()
    assert seen["params"]["status"] == "ok" == settings.TNM_STATUS_OK


@pytest.mark.asyncio
async def test_news_disabled_without_token(monkeypatch):
    monkeypatch.setattr(settings, "TNM_TOKEN", "")
    assert slow.NewsSource().enabled() is False


@pytest.mark.asyncio
async def test_news_uses_tnm_token_not_our_own(monkeypatch):
    """서비스마다 토큰이 따로다 — 자기 것을 보내면 401 이 온다.

    실측 2026-07-27: 이 어댑터 첫 배포에서 정확히 그렇게 실패했다.
    """
    seen = {}
    _install_httpx(monkeypatch, {"items": []}, seen)
    monkeypatch.setattr(settings, "INTERNAL_TOKEN", "trading-own")
    monkeypatch.setattr(settings, "TNM_TOKEN", "tnm-token")
    await slow.NewsSource().collect()
    assert seen["headers"]["X-Internal-Token"] == "tnm-token"


@pytest.mark.asyncio
async def test_manual_never_expires_and_is_max_strength(monkeypatch):
    import app.data.watchlist as wl

    monkeypatch.setattr(wl, "entries", lambda: [
        {"code": "000001", "name": "가", "source": "seed", "added": "x"},
        {"code": "000002", "name": "나", "source": "manual", "added": "y"},
        {"code": "000003", "name": "다", "source": "gainer", "added": "z"},
    ])
    got = await slow.ManualSource().collect()
    assert {s.code for s in got} == {"000001", "000002"}    # 자동 편입분은 제외
    assert all(s.strength == 1.0 and s.ttl_sec == 0 for s in got)


def _install_httpx(monkeypatch, payload, seen=None):
    import httpx

    class _Res:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            if seen is not None:
                seen.update(k)
            return _Res()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


# --- ⑥ 스위치 ---

def test_sources_expose_interval_and_enabled():
    from app.scout import sources

    for cls in sources.ALL:
        s = cls()
        assert s.name in model.SOURCES
        assert isinstance(s.enabled(), bool)
        assert s.interval_sec() > 0


def test_every_source_is_registered_once():
    from app.scout import sources

    names = [c().name for c in sources.ALL]
    assert sorted(names) == sorted(model.SOURCES)
