import asyncio

from app.data import collector


def test_aggregator_builds_and_flushes_bars(monkeypatch):
    flushed = []
    monkeypatch.setattr(
        collector.store, "upsert_bars",
        lambda symbol, tf, df: flushed.append((symbol, tf, df)) or len(df),
    )
    agg = collector.BarAggregator()

    async def run():
        # 10:00 분봉: 100 → 고가 102 / 저가 99 → 종가 101, 거래량 30
        await agg.on_tick("005930", 100.0, 10, "100001")
        await agg.on_tick("005930", 102.0, 10, "100030")
        await agg.on_tick("005930", 99.0, 5, "100045")
        await agg.on_tick("005930", 101.0, 5, "100059")
        # 형성 중 봉 스냅샷
        snap = agg.snapshot("005930")
        assert snap["open"] == 100.0 and snap["high"] == 102.0
        assert snap["low"] == 99.0 and snap["close"] == 101.0 and snap["volume"] == 30
        # 다음 분 진입 → 이전 봉 flush
        await agg.on_tick("005930", 101.5, 7, "100101")
        return agg.snapshot("005930")

    snap2 = asyncio.run(run())
    assert len(flushed) == 1
    symbol, tf, df = flushed[0]
    assert symbol == "005930" and tf == "1m" and len(df) == 1
    row = df.iloc[0]
    assert (row.open, row.high, row.low, row.close, row.volume) == (100.0, 102.0, 99.0, 101.0, 30)
    assert snap2["open"] == 101.5 and snap2["volume"] == 7


def test_snapshot_none_without_ticks():
    assert collector.BarAggregator().snapshot("000000") is None
