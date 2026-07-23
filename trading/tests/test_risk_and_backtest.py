import pandas as pd

from app.backtest import runner
from app.trade import risk


def test_position_size():
    # 1천만 원, 0.5% 리스크(5만 원), 손절 거리 200원 → 250주
    assert risk.position_size(10_000_000, 0.5, 10_000, 9_800) == 250


def test_position_size_capped_by_equity():
    # 리스크상 수량이 매수 가능 수량을 넘으면 잔고로 캡
    assert risk.position_size(1_000_000, 5.0, 10_000, 9_990) == 100


def test_position_size_zero_dist():
    assert risk.position_size(1_000_000, 0.5, 10_000, 10_000) == 0


def test_daily_loss_limit():
    st = risk.DailyRiskState(equity=1_000_000, daily_loss_limit_pct=2.0)
    assert st.can_open()[0]
    st.record_pnl(-20_000)
    assert st.loss_limit_hit
    ok, why = st.can_open()
    assert not ok and "한도" in why


def test_backtest_orb_short_hits_target():
    rows = []
    # 09:00~09:14 범위 100~102
    for m in range(15):
        rows.append((f"09:{m:02d}", 101, 102, 100, 101))
    # 09:15 하단 이탈 → 숏 신호 (entry 99, stop 102, target 94.5)
    rows.append(("09:15", 100, 100.2, 98.9, 99.0))
    # 이후 하락, 09:20 에 target 94.5 터치
    for m, px in [(16, 98.5), (17, 97.5), (18, 96.5), (19, 95.5), (20, 94.0)]:
        rows.append((f"09:{m:02d}", px + 0.5, px + 0.8, px, px + 0.2))
    idx = [pd.Timestamp(f"2026-07-20 {t}:00") for t, *_ in rows]
    df = pd.DataFrame(
        [{"open": o, "high": h, "low": l, "close": c, "volume": 1000}
         for _, o, h, l, c in rows],
        index=pd.DatetimeIndex(idx),
    )
    cfg = {"orb": {"enabled": True, "range_start": "09:00",
                   "range_end": "09:15", "target_r": 1.5}}
    result = runner.run("TEST", df, cfg)
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.side == "short"
    assert trade.exit_reason == "target"
    stats = result.stats()
    assert stats["trades"] == 1
    assert stats["win_rate"] == 100.0
    assert stats["avg_pnl_pct"] > 3.0  # 비용 차감 후에도 양수
