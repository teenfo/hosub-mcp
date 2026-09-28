"""주간 기법 스윕 + rsi_dip 후보 규칙 테스트."""
import pandas as pd
import pytest

from app import settings
from app.backtest import sweep
from app.signals import rules


def _trend_df(n=400):
    """상승 추세 + 주기적 눌림이 있는 1분봉(스윕이 체결을 만들 수 있는 데이터)."""
    idx = pd.date_range("2026-07-20 09:00", periods=n, freq="1min")
    rows, px = [], 10_000.0
    for i in range(n):
        drift = 3.0 if (i // 30) % 2 == 0 else -1.5   # 상승·눌림 반복
        px += drift
        rows.append({"open": px - drift, "high": px + 5, "low": px - 8,
                     "close": px, "volume": 1000 + (i % 7) * 300})
    return pd.DataFrame(rows, index=idx)


def test_run_sweep_writes_scorecard(tmp_path, monkeypatch):
    monkeypatch.setattr(sweep, "OUT_FILE", tmp_path / "rule_sweep.json")
    monkeypatch.setattr(settings, "WATCHLIST", {"005930": "삼성전자"})
    df = _trend_df()
    monkeypatch.setattr("app.data.store.load_bars", lambda s, tf, limit=5000: df)

    out = sweep.run_sweep()
    assert out["symbols"] == 1 and out["side"] == "long"
    # 레지스트리 전 규칙이 성적표에 있다(체결 0이어도 항목은 존재)
    assert set(out["rules"]) == set(rules.REGISTRY)
    for st in out["rules"].values():
        assert "trades" in st
        # 실전 청산 공식 재생 결과가 나란히 실린다(2026-09-28) — 최상위는 legacy 유지
        assert "trades" in st["live"]
    # 파일 영속화 + latest 로 재조회
    assert sweep.latest()["run_ts"] == out["run_ts"]


def test_rsi_dip_signal_and_filters():
    cfg = {"rsi_period": 3, "oversold": 15, "min_bars": 40,
           "stop_lookback": 5, "atr_stop_mult": 0.5, "atr_period": 14, "target_r": 1.5}
    # 상승 유지 + 급락 딥(연속 음봉) 후 반등 양봉
    closes = ([100 + i * 0.3 for i in range(40)]        # 상승
              + [111.0, 110.2, 109.4, 108.8]            # 패닉 딥(RSI3 급락)
              + [109.6])                                 # 반등 양봉
    idx = pd.date_range("2026-07-20 09:00", periods=len(closes), freq="1min")
    rows, prev = [], closes[0]
    for c in closes:
        rows.append({"open": prev, "high": max(prev, c) + 0.1,
                     "low": min(prev, c) - 0.1, "close": c, "volume": 1000})
        prev = c
    df = pd.DataFrame(rows, index=idx)
    s = rules.rsi_dip(df, cfg)
    assert s is not None and s.rule == "rsi_dip" and s.side == "long"
    assert s.stop < s.entry < s.target
    # 하락 구조(세션 초반 대비 하락)에서는 딥 반등이어도 미발동(추세 필터)
    closes2 = [100 - i * 0.3 for i in range(44)] + [88.5]
    rows2, prev = [], closes2[0]
    for c in closes2:
        rows2.append({"open": prev, "high": max(prev, c) + 0.1,
                      "low": min(prev, c) - 0.1, "close": c, "volume": 1000})
        prev = c
    df2 = pd.DataFrame(rows2, index=pd.date_range("2026-07-20 09:00", periods=len(closes2), freq="1min"))
    assert rules.rsi_dip(df2, cfg) is None


def test_스윕은_롱_전용을_재생_단계에_넘긴다(tmp_path, monkeypatch):
    """사후 필터가 아니라 재생 단계에서 방향을 제한해야 한다.

    runner 는 '동시 1포지션' 이라, 양방향으로 재생한 뒤 롱만 고르면 숏이
    자리를 점유해 같은 날 롱 기회를 밀어낸다 — 집계에서만 빠질 뿐 실전
    (long_only=true)과 다른 세계를 측정한다(외부 리뷰 지적, 확인 2026-09-27).
    이 테스트는 runner 호출에 sides=("long",) 가 실리는지를 붙잡아 둔다.
    """
    monkeypatch.setattr(sweep, "OUT_FILE", tmp_path / "rule_sweep.json")
    monkeypatch.setattr(settings, "WATCHLIST", {"005930": "삼성전자"})
    monkeypatch.setattr("app.data.store.load_bars",
                        lambda s, tf, limit=5000: _trend_df())
    seen = []
    real_run = sweep.runner.run

    def _spy(sym, df, cfg, sides=None):
        seen.append(sides)
        return real_run(sym, df, cfg, sides=sides)

    monkeypatch.setattr(sweep.runner, "run", _spy)
    sweep.run_sweep()
    assert seen, "runner 가 호출되지 않았다"
    assert all(s == ("long",) for s in seen), f"sides 미전달 호출 존재: {set(seen)}"
