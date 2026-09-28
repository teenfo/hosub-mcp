"""포트폴리오 재생기 — 자리·자금·우선순위 경쟁을 재현하는가."""
import pandas as pd

from app.backtest import portfolio as pf
from app.backtest import runner
from app.trade import exit_policy as ep

CFG = {"orb": {"enabled": True, "range_start": "09:00", "range_end": "09:15",
               "target_r": 1.5, "priority": 1.0}}
FLAT = ep.Params(trailing=False, max_gain_pct=0.0, refit=False, eod_flat_at="15:20")


def _orb(day="2026-07-20", base=100.0, up=True, until=60):
    """09:00~09:14 범위 → 09:15 상단 돌파(롱 ORB) → 이후 추세."""
    rows = [(f"09:{m:02d}", base + 1, base + 2, base, base + 1) for m in range(15)]
    rows.append(("09:15", base + 1.5, base + 3.2, base + 1.4, base + 3.0))
    px = base + 3.0
    for m in range(16, until):
        px += 0.1 if up else -0.1
        rows.append((f"09:{m:02d}", px - 0.05, px + 0.1, px - 0.1, px))
    idx = [pd.Timestamp(f"{day} {t}:00") for t, *_ in rows]
    return pd.DataFrame([{"open": o, "high": h, "low": lo, "close": c, "volume": 1000}
                         for _, o, h, lo, c in rows], index=pd.DatetimeIndex(idx))


def _pp(**kw):
    base = dict(equity=10_000_000, risk_pct=0.5, max_positions=3,
                max_weight_pct=30.0, daily_loss_limit_pct=0.0)
    return pf.PortfolioParams(**(base | kw))


def test_한_종목이면_종목별_재생기와_같은_청산이다():
    """자리 경쟁이 없으면 runner(live) 와 결과가 같아야 한다 — 공식 공유의 증거."""
    df = _orb()
    one = runner.run("A", df, CFG, sides=("long",), exit_model="live", exit_params=FLAT)
    res = pf.replay({"A": df}, CFG, _pp(), FLAT)
    assert [(t.exit_reason, round(t.exit, 4)) for t in res.trades] == \
        [(t.exit_reason, round(t.exit, 4)) for t in one.trades]


def test_동시_포지션_한도가_세번째_신호를_막는다():
    """같은 분에 3종목이 동시에 돌파 — 한도 2면 하나는 자리를 못 얻는다."""
    dfs = {"A": _orb(base=100), "B": _orb(base=200), "C": _orb(base=50)}
    res = pf.replay(dfs, CFG, _pp(max_positions=2), FLAT)
    assert sorted(t.symbol for t in res.trades) == ["A", "B"]   # 동점 → 종목코드 순
    assert res.blocked["max_positions"] == 1, "하루 한 번만 센다(매 분 재차단 중복 없음)"


def test_현금이_모자라면_뒤_신호는_수량_0이다():
    dfs = {"A": _orb(base=100), "B": _orb(base=200)}
    # 앞 신호가 비중 100%·리스크 50% 로 현금을 거의 다 쓰게 만든다
    res = pf.replay(dfs, CFG, _pp(max_weight_pct=100.0, risk_pct=50.0), FLAT)
    assert [t.symbol for t in res.trades] == ["A"]
    assert res.blocked["cash_or_size"] == 1


def test_수량은_리스크와_비중_상한으로_정해진다():
    res = pf.replay({"A": _orb()}, CFG, _pp(max_weight_pct=10.0), FLAT)
    [t] = res.trades
    assert t.qty * t.model_entry <= 10_000_000 * 0.10 + 1


def _flat(base, day="2026-07-20", n=120, drop_at=None):
    """평탄한 봉. drop_at 분부터 급락(손절 유도)."""
    idx = pd.date_range(f"{day} 09:00", periods=n, freq="1min")
    rows = []
    for i, _ in enumerate(idx):
        px = base * (0.9 if drop_at is not None and i >= drop_at else 1.0)
        rows.append({"open": px, "high": px * 1.001, "low": px * 0.999,
                     "close": px, "volume": 1000})
    return pd.DataFrame(rows, index=idx)


def test_일일_손실_가드가_그날_신규_진입을_끊는다(monkeypatch):
    """A 가 09:20 진입 → 09:30 급락 손절로 한도 초과 → B 의 09:40 신호는 차단."""
    from app.signals.rules import Signal

    def _eval(window, cfg, prev_close=None, now=None):
        last = window.index[-1].strftime("%H:%M")
        px = float(window["close"].iloc[-1])
        if (px > 150 and last == "09:40") or (px < 150 and last == "09:20"):
            return [Signal("orb", "long", px, px * 0.98, px * 1.03, "stub")]
        return []

    monkeypatch.setattr(pf.rules, "evaluate_all", _eval)
    dfs = {"A": _flat(100, drop_at=30), "B": _flat(200)}
    res = pf.replay(dfs, CFG, _pp(daily_loss_limit_pct=0.3), FLAT)
    assert [t.symbol for t in res.trades] == ["A"]
    assert res.trades[0].exit_reason == "stop"
    assert res.blocked.get("daily_loss_guard") == 1
    # 가드가 없으면 B 도 산다(대조)
    free = pf.replay(dfs, CFG, _pp(daily_loss_limit_pct=0.0), FLAT)
    assert sorted(t.symbol for t in free.trades) == ["A", "B"]


def test_롱_전용이면_숏_신호는_발주하지_않는다():
    # 하단 이탈을 만들어 숏 ORB 신호 유도
    rows = [(f"09:{m:02d}", 101, 102, 100, 101) for m in range(15)]
    rows.append(("09:15", 100, 100.2, 98.9, 99.0))
    for m, px in [(16, 98.5), (17, 97.5), (18, 96.5), (19, 95.5), (20, 94.0)]:
        rows.append((f"09:{m:02d}", px + 0.5, px + 0.8, px, px + 0.2))
    idx = [pd.Timestamp(f"2026-07-21 {t}:00") for t, *_ in rows]
    short_df = pd.DataFrame([{"open": o, "high": h, "low": lo, "close": c,
                              "volume": 1000} for _, o, h, lo, c in rows],
                            index=pd.DatetimeIndex(idx))
    res = pf.replay({"S": short_df}, CFG, _pp(), FLAT)
    assert res.trades == [] and res.blocked.get("long_only") == 1


def test_통계는_계좌_기준_손익을_낸다():
    res = pf.replay({"A": _orb()}, CFG, _pp(), FLAT)
    st = res.stats()
    assert st["trades"] == 1 and st["pnl_krw"] == round(res.end_equity - res.start_equity)
    assert st["exits"] == {"eod": 1}
