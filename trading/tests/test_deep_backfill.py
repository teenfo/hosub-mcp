"""신규 편입 종목 심층 백필(연속조회).

분봉 API(ka10080)는 호출 시각 기준 최근 900봉(≈2.4거래일)만 준다. 장 마감 후에
불러도 '최근' 900봉이라 과거가 늘지 않는다. 과거를 확장하는 유일한 경로가 응답
헤더의 cont-yn/next-key 를 물고 되부르는 연속조회다. 그 배선이 살아 있는지,
그리고 종목당 1회로 끝나는지를 여기서 지킨다.
"""
import pandas as pd
import pytest

from app import settings
from app.data import collector, store
from app.signals import engine as engine_mod
from app.signals.engine import SignalEngine


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    """실 DB 를 건드리지 않는다."""
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "market.db")


def _df(start: str, n: int = 3) -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq="1min")
    return pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 10},
        index=idx,
    )


class FakePager:
    """연속조회 응답 흉내 — 페이지마다 다른 봉과 next-key 를 준다."""

    def __init__(self, pages: int, fail_on: int | None = None):
        self.pages, self.fail_on = pages, fail_on
        self.calls: list[str] = []

    async def __call__(self, symbol, interval=1, next_key=""):
        i = len(self.calls)
        self.calls.append(next_key)
        if self.fail_on is not None and i == self.fail_on:
            raise RuntimeError("조회 실패")
        last = i >= self.pages - 1
        return {"_page": i}, ("" if last else f"key{i + 1}")


def _parse_by_page(monkeypatch):
    """페이지 번호로 서로 겹치지 않는 봉을 만들어 낸다."""
    monkeypatch.setattr(
        collector, "parse_chart_response",
        lambda data: _df(f"2026-07-{20 - data['_page']} 09:00"),
    )


# --- ① 연속조회로 여러 페이지를 가져온다 ---

@pytest.mark.asyncio
async def test_deep_backfill_follows_next_key(monkeypatch):
    pager = FakePager(pages=5)
    monkeypatch.setattr(collector.client, "minute_chart_page", pager)
    _parse_by_page(monkeypatch)

    assert await collector.deep_backfill("000001", pages=5) == 15   # 5페이지 × 3봉
    # 첫 호출은 키 없이, 이후엔 직전 응답의 키를 물고 간다
    assert pager.calls == ["", "key1", "key2", "key3", "key4"]


@pytest.mark.asyncio
async def test_deep_backfill_stops_when_no_more_history(monkeypatch):
    """상장 초기 등 과거가 없으면 pages 를 다 쓰지 않고 멈춘다."""
    pager = FakePager(pages=2)
    monkeypatch.setattr(collector.client, "minute_chart_page", pager)
    _parse_by_page(monkeypatch)

    assert await collector.deep_backfill("000001", pages=5) == 6
    assert len(pager.calls) == 2


@pytest.mark.asyncio
async def test_deep_backfill_stops_on_empty_page(monkeypatch):
    pager = FakePager(pages=5)
    monkeypatch.setattr(collector.client, "minute_chart_page", pager)
    monkeypatch.setattr(collector, "parse_chart_response", lambda data: pd.DataFrame())

    assert await collector.deep_backfill("000001", pages=5) == 0
    assert len(pager.calls) == 1


@pytest.mark.asyncio
async def test_deep_backfill_keeps_partial_on_late_failure(monkeypatch):
    """2페이지 이후 실패는 확보한 만큼 남긴다 — 전부 날리지 않는다."""
    pager = FakePager(pages=5, fail_on=2)
    monkeypatch.setattr(collector.client, "minute_chart_page", pager)
    _parse_by_page(monkeypatch)

    assert await collector.deep_backfill("000001", pages=5) == 6   # 2페이지분


@pytest.mark.asyncio
async def test_deep_backfill_raises_on_first_page_failure(monkeypatch):
    """첫 페이지 실패는 올려 보낸다 — 완료로 기록돼 영영 안 받는 일을 막는다."""
    pager = FakePager(pages=5, fail_on=0)
    monkeypatch.setattr(collector.client, "minute_chart_page", pager)

    with pytest.raises(RuntimeError):
        await collector.deep_backfill("000001", pages=5)


# --- ② 종목당 1회 — 완료 기록이 재편입에도 남는다 ---

@pytest.mark.asyncio
async def test_pending_runs_once_per_symbol(monkeypatch):
    monkeypatch.setattr(settings, "WATCHLIST", {"000001": "가", "000002": "나"})
    monkeypatch.setitem(settings.CONFIG, "collection",
                        {"deep_backfill": True, "deep_backfill_pages": 5,
                         "deep_backfill_per_cycle": 10})
    seen = []

    async def fake(sym, pages):
        seen.append((sym, pages))
        return 900 * pages

    monkeypatch.setattr(engine_mod.collector, "deep_backfill", fake)
    eng = SignalEngine()
    assert await eng.deep_backfill_pending() == 2
    assert seen == [("000001", 5), ("000002", 5)]
    # 두 번째 사이클엔 대상이 없다
    assert await eng.deep_backfill_pending() == 0
    assert len(seen) == 2
    # 감시목록에서 빠졌다 재편입돼도 다시 부르지 않는다
    monkeypatch.setattr(settings, "WATCHLIST", {"000002": "나", "000003": "다"})
    assert await eng.deep_backfill_pending() == 1
    assert seen[-1][0] == "000003"


@pytest.mark.asyncio
async def test_pending_retries_failed_symbol(monkeypatch):
    """실패한 종목은 완료로 기록하지 않아 다음 주기에 다시 시도한다."""
    monkeypatch.setattr(settings, "WATCHLIST", {"000001": "가"})
    monkeypatch.setitem(settings.CONFIG, "collection", {"deep_backfill": True})
    calls = []

    async def flaky(sym, pages):
        calls.append(sym)
        if len(calls) == 1:
            raise RuntimeError("조회 실패")
        return 4500

    monkeypatch.setattr(engine_mod.collector, "deep_backfill", flaky)
    eng = SignalEngine()
    assert await eng.deep_backfill_pending() == 0
    assert await eng.deep_backfill_pending() == 1
    assert calls == ["000001", "000001"]
    assert await eng.deep_backfill_pending() == 0        # 성공 후엔 끝


@pytest.mark.asyncio
async def test_pending_marks_zero_bar_symbol(monkeypatch):
    """0봉(거래정지·상장폐지)도 완료로 남겨 매 사이클 재조회를 막는다."""
    monkeypatch.setattr(settings, "WATCHLIST", {"000001": "가"})
    monkeypatch.setitem(settings.CONFIG, "collection", {"deep_backfill": True})
    calls = []

    async def empty(sym, pages):
        calls.append(sym)
        return 0

    monkeypatch.setattr(engine_mod.collector, "deep_backfill", empty)
    eng = SignalEngine()
    assert await eng.deep_backfill_pending() == 1
    assert await eng.deep_backfill_pending() == 0
    assert calls == ["000001"]


# --- ③ 부하 방어 ---

@pytest.mark.asyncio
async def test_pending_respects_per_cycle_cap(monkeypatch):
    """최초 배포 시 감시목록 전체가 대상이 된다 — 한 사이클에 몰아 부르지 않는다."""
    monkeypatch.setattr(settings, "WATCHLIST", {f"00000{i}": "x" for i in range(9)})
    monkeypatch.setitem(settings.CONFIG, "collection",
                        {"deep_backfill": True, "deep_backfill_per_cycle": 3})

    async def ok(sym, pages):
        return 4500

    monkeypatch.setattr(engine_mod.collector, "deep_backfill", ok)
    eng = SignalEngine()
    assert await eng.deep_backfill_pending() == 3
    assert await eng.deep_backfill_pending() == 3
    assert await eng.deep_backfill_pending() == 3
    assert await eng.deep_backfill_pending() == 0
    # 마감 후에는 상한을 넉넉히 줄 수 있다
    monkeypatch.setattr(settings, "WATCHLIST", {f"1000{i:02d}": "y" for i in range(9)})
    assert await eng.deep_backfill_pending(limit=30) == 9


@pytest.mark.asyncio
async def test_pending_can_be_disabled(monkeypatch):
    monkeypatch.setattr(settings, "WATCHLIST", {"000001": "가"})
    monkeypatch.setitem(settings.CONFIG, "collection", {"deep_backfill": False})

    async def boom(sym, pages):
        raise AssertionError("꺼져 있으면 부르지 않는다")

    monkeypatch.setattr(engine_mod.collector, "deep_backfill", boom)
    assert await SignalEngine().deep_backfill_pending() == 0


@pytest.mark.asyncio
async def test_pending_includes_collect_only(monkeypatch):
    """수집전용도 대상 — 매매는 안 해도 백테스트가 과거를 쓴다(1회 비용)."""
    monkeypatch.setattr(settings, "WATCHLIST", {"000001": "매매", "000002": "수집"})
    monkeypatch.setattr(settings, "COLLECT_ONLY", {"000002"})
    monkeypatch.setitem(settings.CONFIG, "collection",
                        {"deep_backfill": True, "deep_backfill_per_cycle": 10})
    seen = []

    async def fake(sym, pages):
        seen.append(sym)
        return 4500

    monkeypatch.setattr(engine_mod.collector, "deep_backfill", fake)
    assert await SignalEngine().deep_backfill_pending() == 2
    assert set(seen) == {"000001", "000002"}


@pytest.mark.asyncio
async def test_eod_recovers_symbols_missed_intraday(monkeypatch):
    """장중 사이클 상한에 밀린 신규 편입분을 마감 백필이 회수한다."""
    monkeypatch.setattr(settings, "WATCHLIST", {f"00000{i}": "x" for i in range(5)})
    monkeypatch.setattr(settings, "COLLECT_ONLY", set())
    monkeypatch.setitem(settings.CONFIG, "collection",
                        {"deep_backfill": True, "deep_backfill_per_cycle": 2,
                         "deep_backfill_eod_max": 30})
    from app.data import roster
    monkeypatch.setattr(roster, "active", lambda days: {})
    deep = []

    async def fake_deep(sym, pages):
        deep.append(sym)
        return 4500

    async def fake_flat(sym):
        return 900

    monkeypatch.setattr(engine_mod.collector, "deep_backfill", fake_deep)
    monkeypatch.setattr(engine_mod.collector, "backfill_minutes", fake_flat)
    eng = SignalEngine()
    assert await eng.deep_backfill_pending() == 2       # 장중엔 2종목까지
    assert await eng.eod_backfill_once() == 5
    assert len(deep) == 5                               # 마감 후 나머지 3종목 회수


# --- ④ 헤더 배선 (cont-yn / next-key) ---

@pytest.mark.asyncio
async def test_minute_chart_page_header_round_trip(monkeypatch):
    """요청에 next-key 를 실어 보내고, 응답 헤더에서 다음 키를 꺼낸다."""
    import httpx

    from app.kiwoom import client as client_mod

    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(dict(request.headers))
        n = len(sent)
        hdr = ({"cont-yn": "Y", "next-key": f"k{n}"} if n < 3
               else {"cont-yn": "N", "next-key": "k3"})
        return httpx.Response(200, json={"stk_min_pole_chart_qry": []}, headers=hdr)

    kc = client_mod.KiwoomClient()
    kc._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(kc._limiter, "max_rps", 1000)
    monkeypatch.setattr(kc._limiter, "effective_rps", 1000)

    async def token():
        return "T"

    monkeypatch.setattr(client_mod.token_manager, "get", token)

    _, nxt = await kc.minute_chart_page("000001")
    assert nxt == "k1"
    assert sent[0]["cont-yn"] == "N" and "next-key" not in sent[0]

    _, nxt = await kc.minute_chart_page("000001", next_key=nxt)
    assert nxt == "k2"
    assert sent[1]["cont-yn"] == "Y" and sent[1]["next-key"] == "k1"

    # cont-yn 이 N 이면 next-key 가 와도 무시한다 — 무한 루프 방지
    _, nxt = await kc.minute_chart_page("000001", next_key=nxt)
    assert nxt == ""
    await kc._http.aclose()


# --- ⑤ 설정 배선 ---

def test_config_wires_deep_backfill():
    c = settings.CONFIG["collection"]
    assert c["deep_backfill"] is True
    # 5페이지 × 900봉 ÷ 하루 381봉 ≈ 11.8거래일 — backtest.min_days 를 넘겨야 한다
    days = c["deep_backfill_pages"] * 900 / 381
    assert days > settings.CONFIG["backtest"]["min_days"]
