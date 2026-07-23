import pandas as pd

from app.backtest import report
from app.data import store


def test_aggregate_weights_by_trades():
    rows = [
        {"symbol": "A", "trades": 10, "win_rate": 60.0, "avg_pnl_pct": 1.0,
         "by_rule": {"orb": 1.0}},
        {"symbol": "B", "trades": 30, "win_rate": 40.0, "avg_pnl_pct": -0.5,
         "by_rule": {"orb": -0.5, "gap": 0.2}},
        {"symbol": "C", "trades": 0},   # 체결 없는 종목은 가중에서 제외
    ]
    agg = report.aggregate(rows)
    assert agg["symbols"] == 3
    assert agg["with_trades"] == 2
    assert agg["trades"] == 40
    # 승률 가중평균 = (60*10 + 40*30)/40 = 45.0
    assert agg["win_rate"] == 45.0
    # orb 규칙 평균 = (1.0 + -0.5)/2 = 0.25, gap = 0.2
    assert agg["by_rule"]["orb"] == 0.25
    assert agg["by_rule"]["gap"] == 0.2


def test_aggregate_no_trades():
    assert report.aggregate([{"symbol": "A", "trades": 0}])["trades"] == 0


def _seed_minutes(sym, days):
    """days 일치 09:00~09:10 분봉을 심는다."""
    for d in range(1, days + 1):
        idx = pd.date_range(f"2026-07-{d:02d} 09:00", periods=11, freq="1min")
        df = pd.DataFrame(
            {"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1000},
            index=idx,
        )
        store.upsert_bars(sym, "1m", df)


def test_minute_symbols_counts_distinct_days(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "market.db")
    _seed_minutes("005930", 4)
    _seed_minutes("000660", 2)
    got = dict(store.minute_symbols(min_days=3))
    assert got == {"005930": 4}          # 000660 은 2일 → 3일 미만 제외


def test_prune_minutes_drops_old_days(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "market.db")
    _seed_minutes("005930", 10)          # 07-01 ~ 07-10
    deleted = store.prune_minutes(keep_days=3)   # 최신 07-10 기준 07-07 이전 삭제
    assert deleted > 0
    remaining = store.minute_symbols(min_days=1)
    assert dict(remaining)["005930"] <= 4        # 07-07~07-10 정도만 남음
