"""감시목록 후보군(유니버스) 확대 — 자동 편입 경로와 매매 tier 판정.

배경(2026-07-24 실측): 매매 가능 후보 229종목 중 감시목록에 있던 건 19종목(8%).
일중변동 상위 20 중 감시 중이던 종목은 2개뿐이었다. 원인은 세 가지였고
여기서 그 회귀를 막는다.
"""
import pytest

from app import settings
from app.data import watchlist
from app.signals import scanner


@pytest.fixture(autouse=True)
def _reset_equity():
    """자산 전역은 테스트 간 격리한다(다른 테스트에 새지 않게)."""
    before = settings.EQUITY
    yield
    settings.set_equity(before)


def _item(code, name="종목", price=10_000, chg=5.0, tv=50_000, vol=500_000):
    return {"code": code, "name": name, "price": price, "change_pct": chg,
            "trade_value": tv, "volume": vol}


# --- ① 매매 tier 가격 상한이 자산·비중 상한에 연동된다 ---

def test_price_cap_follows_equity(monkeypatch):
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", 20)
    settings.set_equity(560_000)
    assert settings.tradable_price_cap(30_000) == 112_000     # 자산의 20%
    settings.set_equity(2_000_000)
    assert settings.tradable_price_cap(30_000) == 400_000     # 계좌가 커지면 함께
    settings.set_equity(0)
    assert settings.tradable_price_cap(30_000) == 30_000      # 미동기화 → 폴백


def test_price_cap_without_weight_limit(monkeypatch):
    """비중 상한이 없으면 자산 전액이 한 종목에 들어갈 수 있다."""
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", 0)
    settings.set_equity(560_000)
    assert settings.tradable_price_cap(30_000) == 560_000
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", "bad")
    assert settings.tradable_price_cap(30_000) == 560_000     # 잘못된 값도 안전


def test_gainers_tier_uses_dynamic_cap(monkeypatch):
    """고정 3만원이 아니라 실제 매수 가능액으로 매매/수집을 가른다.
    (회귀: gainer 15종목 중 11개가 3만원 상한에 걸려 수집전용이었다)"""
    monkeypatch.setattr(settings, "WATCHLIST", {})
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", 20)
    settings.set_equity(560_000)                              # 상한 112,000
    cfg = {"min_price": 1_000, "min_trade_value_krw": 0,
           "trade_max_price": 30_000, "top_n": 15}
    out = {x["code"]: x["collect_only"] for x in scanner.filter_gainers(
        [_item("000001", "저가", price=25_000),
         _item("000002", "중가", price=90_000),
         _item("000003", "고가", price=150_000)], cfg)}
    assert out == {"000001": False, "000002": False, "000003": True}


# --- ② 거래대금 상위가 감시목록으로 이어진다 ---

def test_filter_candidates_assigns_tier(monkeypatch):
    monkeypatch.setattr(settings, "WATCHLIST", {})
    monkeypatch.setitem(settings.RISK, "max_position_weight_pct", 20)
    settings.set_equity(560_000)
    cfg = {"min_change_pct": 3.0, "min_trade_value": 10_000,
           "min_price": 1_000, "top_n": 10, "trade_max_price": 30_000}
    out = scanner.filter_candidates(
        [_item("000001", price=20_000), _item("000002", price=300_000)], cfg)
    assert {x["code"]: x["collect_only"] for x in out} == {
        "000001": False, "000002": True}


def test_filter_candidates_excludes_etf(monkeypatch):
    """ETF·리츠·스팩은 자동 편입에서 뺀다(급등 자동편입 시 잡ETF 유입 방지)."""
    monkeypatch.setattr(settings, "WATCHLIST", {})
    cfg = {"min_change_pct": 3.0, "min_trade_value": 10_000, "min_price": 1_000,
           "top_n": 10}
    out = scanner.filter_candidates(
        [_item("000001", "KODEX 레버리지"), _item("000002", "삼성전자"),
         _item("000003", "○○리츠")], cfg)
    assert [x["code"] for x in out] == ["000002"]


def test_filter_candidates_keeps_existing_active(monkeypatch):
    """이미 감시 중이어도 active 소스면 후보로 유지 — 교체 시 탈락 방지."""
    monkeypatch.setattr(settings, "WATCHLIST", {"000001": "기존"})
    cfg = {"min_change_pct": 3.0, "min_trade_value": 10_000, "min_price": 1_000,
           "top_n": 10}
    assert scanner.filter_candidates([_item("000001")], cfg) == []
    kept = scanner.filter_candidates([_item("000001")], cfg,
                                     keep=frozenset({"000001"}))
    assert [x["code"] for x in kept] == ["000001"]


def test_replace_active_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(watchlist, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(settings, "WATCHLIST", {})
    monkeypatch.setattr(settings, "COLLECT_ONLY", set())
    watchlist.add("005930", "삼성전자", source="manual")
    watchlist.replace_active([
        {"code": "000660", "name": "A", "collect_only": False},
        {"code": "035420", "name": "B", "collect_only": True},
        {"code": "005930", "name": "중복", "collect_only": False},   # manual 유지
    ])
    rows = {e["code"]: e for e in watchlist.entries()}
    assert rows["000660"]["source"] == "active"
    assert rows["005930"]["source"] == "manual"       # 다른 소스는 건드리지 않는다
    assert settings.COLLECT_ONLY == {"035420"}
    # 다음 스캔에서 전량 교체 — 더 이상 상위가 아닌 종목은 빠진다
    watchlist.replace_active([{"code": "000660", "name": "A"}])
    codes = {e["code"] for e in watchlist.entries() if e["source"] == "active"}
    assert codes == {"000660"}
    assert "005930" in {e["code"] for e in watchlist.entries()}   # manual 은 그대로


def test_config_wires_active_auto_watch():
    """거래대금 상위 → 감시목록 배선이 살아 있는지(되돌아가면 잡는다)."""
    sc = settings.CONFIG["scanner"]
    assert sc.get("auto_watch") is True
    assert sc.get("market") == "000"          # 전체 시장(코스닥 포함)


def test_config_gainers_cover_both_markets():
    mk = settings.CONFIG["gainers"].get("markets")
    assert mk and "001" in mk and "101" in mk   # 코스피 + 코스닥


# --- ③ 수집전용은 장중 실시간 백필에서 제외, 마감 후 일괄 ---
# 매매하지 않는 종목을 30초마다 부를 이유가 없다. 분봉 API 가 1회에 900봉
# (≈2.3거래일)을 주므로 마감 후 한 번이면 그날치가 통째로 들어온다.

def test_realtime_targets_exclude_collect_only(monkeypatch):
    from app.signals.engine import SignalEngine

    monkeypatch.setattr(settings, "WATCHLIST",
                        {"000001": "매매", "000002": "매매", "000003": "수집"})
    monkeypatch.setattr(settings, "COLLECT_ONLY", {"000003"})
    monkeypatch.setitem(settings.CONFIG, "collection",
                        {"realtime_trading_only": True})
    assert SignalEngine._collect_targets() == ["000001", "000002"]

    # 끄면 종전대로 전체
    monkeypatch.setitem(settings.CONFIG, "collection",
                        {"realtime_trading_only": False})
    assert set(SignalEngine._collect_targets()) == {"000001", "000002", "000003"}


@pytest.mark.asyncio
async def test_eod_backfill_covers_everything(monkeypatch):
    """마감 백필은 수집전용을 포함한 감시목록 전체를 훑는다."""
    from app.signals import engine as engine_mod
    from app.signals.engine import SignalEngine

    monkeypatch.setattr(settings, "WATCHLIST",
                        {"000001": "매매", "000002": "수집"})
    monkeypatch.setattr(settings, "COLLECT_ONLY", {"000002"})
    called = []

    async def fake(sym):
        called.append(sym)

    monkeypatch.setattr(engine_mod.collector, "backfill_minutes", fake)
    assert await SignalEngine().eod_backfill_once() == 2
    assert set(called) == {"000001", "000002"}


@pytest.mark.asyncio
async def test_eod_backfill_survives_symbol_failure(monkeypatch):
    """한 종목이 실패해도 나머지는 계속 — 마감 확정이 통째로 날아가지 않게."""
    from app.signals import engine as engine_mod
    from app.signals.engine import SignalEngine

    monkeypatch.setattr(settings, "WATCHLIST", {"000001": "a", "000002": "b"})
    monkeypatch.setattr(settings, "COLLECT_ONLY", set())

    async def boom(sym):
        if sym == "000001":
            raise RuntimeError("조회 실패")

    monkeypatch.setattr(engine_mod.collector, "backfill_minutes", boom)
    assert await SignalEngine().eod_backfill_once() == 1


def test_config_eod_backfill_precedes_backtest_report():
    """마감 백필이 백테스트 리포트보다 앞서야 완결된 데이터로 평가된다."""
    c = settings.CONFIG
    assert c["collection"]["eod_backfill"] is True
    assert c["collection"]["eod_backfill_after"] < c["backtest"]["run_after"]
