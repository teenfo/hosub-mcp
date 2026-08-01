"""발굴 점수 이벤트 스터디 — 규칙이 실제로 익일 수익률과 상관이 있는가.

여기서 지켜야 할 것은 결론이 아니라 **측정의 정직성**이다.
- 벡터화 패널이 실제 발굴 로직(compute_features)과 갈라지지 않을 것
- 미래를 참조하지 않을 것
- 잡을 수 없는 갭을 수익으로 세지 않을 것
- 시장이 오른 날을 규칙의 공으로 돌리지 않을 것
결론이 좋게 나오도록 만드는 코드가 아니라, 틀린 결론을 막는 코드다.
"""
import numpy as np
import pandas as pd
import pytest

from app import settings
from app.features import compute_features
from app.research import eventstudy, panel


def _bars(n=140, seed=7):
    """재현 가능한 가짜 일봉(오름차순)."""
    rng = np.random.default_rng(seed)
    close = 10_000 + np.cumsum(rng.normal(0, 120, n))
    high = close + rng.uniform(20, 200, n)
    low = close - rng.uniform(20, 200, n)
    op = close + rng.normal(0, 60, n)
    vol = rng.integers(50_000, 900_000, n)
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.DataFrame({"open": op, "high": high, "low": low,
                         "close": close, "volume": vol}, index=idx)


# --- ① 벡터화 패널이 실제 발굴 로직과 같은 값을 낸다 ---
# 두 경로가 갈라지면 연구 결과가 실제 매매와 다른 것을 재게 된다.

@pytest.mark.parametrize("seed", [1, 2, 3, 11, 42])
def test_panel_last_row_matches_compute_features(seed):
    cfg = settings.CONFIG["nightly"]
    df = _bars(seed=seed)
    p = panel.panel(df, cfg)
    f = compute_features(df, cfg)
    last = p.iloc[-1]
    assert int(last["score"]) == int(f["score"])
    assert int(last["liquid"]) == int(f["liquid"])
    assert int(last["ma_aligned_new"]) == int(f["ma_aligned_new"])
    assert last["near_high60_pct"] == pytest.approx(f["near_high60_pct"], abs=0.05)
    assert last["rsi14"] == pytest.approx(f["rsi14"], abs=0.05)
    assert last["atr_pct"] == pytest.approx(f["atr_pct"], abs=0.05)
    assert last["ret_20d"] == pytest.approx(f["ret_20d"], abs=0.05)


@pytest.mark.parametrize("cut", [80, 100, 125])
def test_panel_matches_at_every_past_date(cut):
    """과거 어느 날을 잘라도 그날 값이 같아야 한다 — 그래야 과거 재현이 성립한다."""
    cfg = settings.CONFIG["nightly"]
    df = _bars(seed=5)
    p = panel.panel(df, cfg)
    f = compute_features(df.iloc[:cut], cfg)
    row = p.loc[df.index[cut - 1]]
    assert int(row["score"]) == int(f["score"])
    assert int(row["liquid"]) == int(f["liquid"])


def test_panel_needs_60_rows():
    assert panel.panel(_bars(n=59), {}).empty
    assert not panel.panel(_bars(n=140), {}).empty


# --- ② 미래를 참조하지 않는다 ---

def test_panel_does_not_peek_forward():
    """뒤쪽 데이터를 바꿔도 앞쪽 피처는 변하지 않아야 한다."""
    cfg = settings.CONFIG["nightly"]
    df = _bars(seed=9)
    base = panel.panel(df, cfg)
    tampered = df.copy()
    tampered.iloc[-20:, tampered.columns.get_loc("close")] *= 3   # 미래를 크게 왜곡
    tampered.iloc[-20:, tampered.columns.get_loc("volume")] *= 10
    after = panel.panel(tampered, cfg)
    common = base.index[:-20]
    pd.testing.assert_frame_equal(base.loc[common], after.loc[common])


def test_forward_uses_next_open_not_today_close():
    """수익률 기준은 t+1 시가다 — t 종가 진입은 배치 시각상 불가능하다."""
    p = pd.DataFrame({
        "close": [100.0, 110.0, 121.0],
        "open": [99.0, 105.0, 115.0],
    }, index=pd.date_range("2026-01-01", periods=3))
    out = panel.forward(p, horizons=(1,))
    # t0: 익일 시가 105 → 익일 종가 110 = +4.762%
    assert out["fwd_1"].iloc[0] == pytest.approx((110 / 105 - 1) * 100)
    # 잡을 수 없는 갭(100 → 105)은 수익이 아니라 gap_pct 로 따로 잡힌다
    assert out["gap_pct"].iloc[0] == pytest.approx(5.0)
    assert np.isnan(out["fwd_1"].iloc[-1])          # 마지막 날은 미래가 없다


def test_forward_horizons_share_entry_price():
    """3일·5일 보유도 진입가는 같은 t+1 시가여야 비교가 성립한다."""
    p = pd.DataFrame({"close": [100.0, 110.0, 120.0, 130.0],
                      "open": [99.0, 100.0, 111.0, 121.0]},
                     index=pd.date_range("2026-01-01", periods=4))
    out = panel.forward(p, horizons=(1, 3))
    assert out["fwd_1"].iloc[0] == pytest.approx(10.0)     # 100 → 110
    assert out["fwd_3"].iloc[0] == pytest.approx(30.0)     # 100 → 130 (같은 진입가)


# --- ③ 시장이 오른 날을 규칙의 공으로 돌리지 않는다 ---

def _long(rows):
    return pd.DataFrame(rows)


def test_excess_removes_market_move():
    """전 종목이 똑같이 +5% 오른 날이면 초과수익은 0이어야 한다."""
    df = _long([
        {"date": "2026-07-01", "symbol": "A", "score": 3, "liquid": 1,
         "fwd_1": 5.0, "fwd_3": 5.0, "fwd_5": 5.0, "gap_pct": 0.0},
        {"date": "2026-07-01", "symbol": "B", "score": 0, "liquid": 1,
         "fwd_1": 5.0, "fwd_3": 5.0, "fwd_5": 5.0, "gap_pct": 0.0},
    ])
    out = eventstudy.analyze(df, cost_pct=0.28)
    for b in out["buckets"]:
        assert b["excess_pct"] == 0.0        # 규칙의 공이 아니다
        assert b["mean_pct"] == 5.0          # 원시 수익률은 그대로 보고한다


def test_excess_detects_real_edge():
    """점수 높은 쪽만 더 올랐으면 초과수익으로 잡혀야 한다."""
    rows = []
    for d in range(10):
        rows += [{"date": f"2026-07-{d + 1:02d}", "symbol": "A", "score": 3,
                  "liquid": 1, "fwd_1": 3.0, "fwd_3": 3.0, "fwd_5": 3.0, "gap_pct": 0.0},
                 {"date": f"2026-07-{d + 1:02d}", "symbol": "B", "score": 0,
                  "liquid": 1, "fwd_1": -1.0, "fwd_3": -1.0, "fwd_5": -1.0, "gap_pct": 0.0}]
    out = eventstudy.analyze(_long(rows), cost_pct=0.28)
    hi = next(b for b in out["buckets"] if b["score"] == 3)
    lo = next(b for b in out["buckets"] if b["score"] == 0)
    assert hi["excess_pct"] > 0 > lo["excess_pct"]
    assert hi["excess_pct"] == pytest.approx(2.0)


# --- ④ 비용과 표본 신뢰도를 숨기지 않는다 ---

def test_net_return_subtracts_cost():
    df = _long([{"date": "2026-07-01", "symbol": "A", "score": 2, "liquid": 1,
                 "fwd_1": 0.20, "fwd_3": 0.2, "fwd_5": 0.2, "gap_pct": 0.0}])
    b = eventstudy.analyze(df, cost_pct=0.28)["buckets"][0]
    assert b["mean_pct"] == 0.2
    assert b["net_pct"] == pytest.approx(-0.08)      # 비용 반영 시 적자

def test_small_bucket_flagged_unreliable():
    rows = [{"date": f"2026-07-{i + 1:02d}", "symbol": "A", "score": 3, "liquid": 1,
             "fwd_1": 1.0, "fwd_3": 1.0, "fwd_5": 1.0, "gap_pct": 0.0} for i in range(5)]
    b = eventstudy.analyze(_long(rows), cost_pct=0.28)["buckets"][0]
    assert b["n"] == 5 and b["reliable"] is False


def test_gap_is_reported_separately():
    """엣지가 못 먹는 갭에 있었는지 드러나야 한다."""
    rows = [{"date": f"2026-07-{i + 1:02d}", "symbol": "A", "score": 3, "liquid": 1,
             "fwd_1": 0.1, "fwd_3": 0.1, "fwd_5": 0.1, "gap_pct": 2.5} for i in range(40)]
    b = eventstudy.analyze(_long(rows), cost_pct=0.28)["buckets"][0]
    assert b["gap_pct"] == 2.5 and b["mean_pct"] == 0.1


def test_liquid_split_reported():
    """유동성 게이트를 통과한 표본만 따로 봐야 실제 매매 가능성이 보인다."""
    rows = [{"date": "2026-07-01", "symbol": "A", "score": 3, "liquid": 1,
             "fwd_1": 1.0, "fwd_3": 1.0, "fwd_5": 1.0, "gap_pct": 0.0},
            {"date": "2026-07-01", "symbol": "B", "score": 3, "liquid": 0,
             "fwd_1": 9.0, "fwd_3": 9.0, "fwd_5": 9.0, "gap_pct": 0.0}]
    out = eventstudy.analyze(_long(rows), cost_pct=0.28)
    assert out["liquid_rows"] == 1
    assert out["buckets_liquid"][0]["mean_pct"] == 1.0      # 못 사는 종목이 섞이지 않는다


def test_caveats_include_survivorship():
    assert any("생존 편향" in c for c in eventstudy.CAVEATS)


# --- ⑤ IC(순위상관) ---

def test_ic_detects_monotonic_relation():
    """피처 순위와 수익률 순위가 같으면 IC 가 1 에 가까워야 한다."""
    rows = []
    for d in range(12):
        for k in range(25):
            rows.append({"date": f"2026-07-{d + 1:02d}", "symbol": f"S{k}",
                         "score": k % 4, "liquid": 1, "gap_pct": 0.0,
                         "vol_ratio20": float(k), "fwd_1": float(k),
                         "fwd_3": float(k), "fwd_5": float(k)})
    out = eventstudy.analyze(_long(rows), cost_pct=0.28)
    ic = {r["feature"]: r for r in out["ic"]}
    assert ic["vol_ratio20"]["mean_ic"] == pytest.approx(1.0, abs=0.01)
    assert ic["vol_ratio20"]["hit_rate"] == 100.0
    assert ic["vol_ratio20"]["days"] == 12


def test_ic_near_zero_for_noise():
    rng = np.random.default_rng(3)
    rows = []
    for d in range(30):
        for k in range(40):
            rows.append({"date": f"2026-07-{d + 1:02d}", "symbol": f"S{k}",
                         "score": int(rng.integers(0, 4)), "liquid": 1, "gap_pct": 0.0,
                         "vol_ratio20": float(rng.normal()), "fwd_1": float(rng.normal()),
                         "fwd_3": 0.0, "fwd_5": 0.0})
    out = eventstudy.analyze(_long(rows), cost_pct=0.28)
    ic = {r["feature"]: r for r in out["ic"]}
    assert abs(ic["vol_ratio20"]["mean_ic"]) < 0.1
    assert abs(ic["vol_ratio20"]["t_stat"]) < 2.5      # 우연과 구분되지 않는다


def test_empty_input_is_safe():
    assert eventstudy.analyze(pd.DataFrame(), cost_pct=0.28) == {"rows": 0}


# --- ⑥ 국면 분리 — 알파인가 저베타 효과인가 ---
# 하락장 한 국면만 담긴 표본에서 '덜 움직인 종목이 이겼다' 는 자동으로 성립한다.
# 그건 예측력이 아니라 베타 노출이 낮았던 것이다. 시장이 오르는 날 부호가
# 뒤집히는지 보면 둘을 가를 수 있다.

def _regime_rows(n_days=60, beta_only=True, seed=1):
    """저베타 피처를 심어 둔 가짜 패널.

    beta_only=True  : 저변동성 종목이 시장 등락의 절반만 따라간다(순수 베타)
    beta_only=False : 시장과 무관하게 항상 +2%p 더 번다(순수 알파)
    """
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(n_days):
        mkt = 2.0 if d % 2 == 0 else -2.0          # 상승·하락 날이 번갈아
        for k in range(30):
            low_vol = k < 15
            if beta_only:
                fwd = mkt * (0.5 if low_vol else 1.5)
            else:
                fwd = mkt + (2.0 if low_vol else 0.0)
            atr = 1.0 if low_vol else 5.0
            rows.append({
                "date": f"2026-{1 + d // 28:02d}-{1 + d % 28:02d}",
                "symbol": f"S{k}", "score": 1, "liquid": 1, "gap_pct": 0.0,
                "atr_pct": atr,
                # 변동성과 거의 같이 움직이는 피처 — 잔차화가 죽여야 하는 대상.
                # 완전히 같으면 잔차가 상수(0)라 상관이 정의되지 않으므로 잡음을 섞는다.
                "vol_ratio20": atr + rng.normal(0, 0.3),
                "ret_1d": mkt + rng.normal(0, 0.01),
                "fwd_1": fwd + rng.normal(0, 0.05),
                "fwd_3": fwd, "fwd_5": fwd,
            })
    return pd.DataFrame(rows)


def _ic_of(section, label, feature):
    return next(r for r in section[label]["ic"] if r["feature"] == feature)["mean_ic"]


def test_regime_split_exposes_beta():
    """저베타 피처는 익일 시장 방향에 따라 IC 부호가 뒤집혀야 한다."""
    out = eventstudy.analyze(_regime_rows(beta_only=True), cost_pct=0.28)
    up = _ic_of(out["by_fwd_dir"], "익일 상승", "atr_pct")
    down = _ic_of(out["by_fwd_dir"], "익일 하락", "atr_pct")
    assert up > 0 > down          # 오르는 날엔 고변동성이 이긴다 = 알파가 아니다


def test_regime_split_keeps_alpha_stable():
    """진짜 알파는 국면이 바뀌어도 부호가 유지된다."""
    out = eventstudy.analyze(_regime_rows(beta_only=False), cost_pct=0.28)
    up = _ic_of(out["by_fwd_dir"], "익일 상승", "atr_pct")
    down = _ic_of(out["by_fwd_dir"], "익일 하락", "atr_pct")
    assert up < 0 and down < 0    # 저변동성이 양쪽 다 이긴다 = 알파


def test_trend_label_uses_only_past():
    """사전 국면(trend)은 그날 이미 알 수 있는 정보로만 매겨져야 한다."""
    df = _regime_rows(n_days=60)
    labeled = eventstudy.label_regimes(df)
    # 앞 TREND_DAYS 일은 추세를 셀 수 없어 라벨이 비어야 한다
    dates = sorted(df["date"].unique())
    early = labeled[labeled["date"] == dates[0]]
    assert early["trend"].isna().all()
    assert labeled[labeled["date"] == dates[-1]]["trend"].notna().all()


def test_trend_split_is_reported():
    out = eventstudy.analyze(_regime_rows(n_days=90), cost_pct=0.28)
    assert out["by_trend"], "사전 국면 분리 결과가 있어야 한다"
    for sec in out["by_trend"].values():
        assert sec["days"] >= eventstudy.MIN_REGIME_DAYS


def test_short_regime_dropped():
    """표본이 짧은 국면은 비교 대상에서 뺀다 — 우연을 국면 차이로 읽지 않게."""
    df = _regime_rows(n_days=30)
    out = eventstudy.analyze(df, cost_pct=0.28)
    for sec in out["by_fwd_dir"].values():
        assert sec["days"] >= eventstudy.MIN_REGIME_DAYS


def test_regime_split_uses_liquid_sample():
    """국면 분리는 매수 가능 표본으로 — 못 사는 종목 성적은 판단에 안 쓴다."""
    df = _regime_rows(n_days=60)
    df.loc[df["symbol"] == "S0", "liquid"] = 0
    df.loc[df["symbol"] == "S0", "fwd_1"] = 99.0      # 못 사는 종목에 큰 값
    out = eventstudy.analyze(df, cost_pct=0.28)
    for sec in out["by_fwd_dir"].values():
        assert sec["market_pct"] < 10          # 99% 짜리가 섞이지 않았다


def test_caveat_warns_fwd_dir_not_tradable():
    assert any("사전에 알 수 없" in c for c in eventstudy.CAVEATS)


def test_market_daily_is_cross_sectional_mean():
    df = pd.DataFrame([
        {"date": "2026-07-01", "ret_1d": 1.0}, {"date": "2026-07-01", "ret_1d": 3.0},
        {"date": "2026-07-02", "ret_1d": -2.0},
    ])
    m = eventstudy.market_daily(df)
    assert m["2026-07-01"] == 2.0 and m["2026-07-02"] == -2.0


# --- ⑦ 목적어 교정 — 발굴은 방향이 아니라 '움직임'을 공급해야 한다 ---
# 발굴이 먹이는 것은 orb·momentum 같은 장중 규칙이다. 익일 종가 방향만 재면
# 잘못된 것을 최적화하게 된다. 1년치 하네스가 그 상태였다.

def test_panel_keeps_high_low():
    """새 목적어의 재료. 종전엔 close/open 만 실어 계산 자체가 불가능했다."""
    p = panel.panel(_bars(seed=4), settings.CONFIG["nightly"])
    assert {"high", "low", "open", "close"} <= set(p.columns)


def test_forward_targets_share_the_same_entry():
    """셋 다 t+1 시가 진입 기준이어야 서로 비교된다."""
    p = pd.DataFrame({
        "close": [100.0, 110.0, 120.0], "open": [99.0, 100.0, 111.0],
        "high": [101.0, 115.0, 121.0], "low": [98.0, 95.0, 110.0],
    }, index=pd.date_range("2026-01-01", periods=3))
    out = panel.forward(p, horizons=(1,))
    # t0 진입가 = t1 시가 100
    assert out["fwd_1"].iloc[0] == pytest.approx(10.0)        # → t1 종가 110
    assert out["fwd_up_1"].iloc[0] == pytest.approx(15.0)     # → t1 고가 115
    assert out["fwd_range_1"].iloc[0] == pytest.approx(20.0)  # (115−95)/100


def test_forward_range_is_never_negative():
    """고가−저가는 정의상 음수가 될 수 없다 — 부호가 뒤집히면 계산 오류다."""
    p = panel.forward(panel.panel(_bars(seed=13), settings.CONFIG["nightly"]))
    r = p["fwd_range_1"].dropna()
    assert len(r) and (r >= 0).all()


def test_forward_up_never_below_direction():
    """고가는 종가 이상이므로 최대상승 ≥ 방향수익률이어야 한다."""
    p = panel.forward(panel.panel(_bars(seed=21), settings.CONFIG["nightly"]))
    both = p[["fwd_up_1", "fwd_1"]].dropna()
    assert len(both) and (both["fwd_up_1"] >= both["fwd_1"] - 1e-9).all()


def test_forward_last_row_has_no_future():
    p = panel.forward(panel.panel(_bars(seed=6), settings.CONFIG["nightly"]))
    assert np.isnan(p["fwd_up_1"].iloc[-1]) and np.isnan(p["fwd_range_1"].iloc[-1])


def test_forward_without_high_low_is_safe():
    """high/low 가 없는 프레임(구버전 호출)이 와도 터지지 않는다."""
    p = pd.DataFrame({"close": [100.0, 110.0], "open": [99.0, 100.0]},
                     index=pd.date_range("2026-01-01", periods=2))
    out = panel.forward(p, horizons=(1,))
    assert "fwd_1" in out and "fwd_up_1" not in out


def test_ic_is_computed_per_target():
    rows = []
    for d in range(12):
        for k in range(25):
            rows.append({"date": f"2026-07-{d + 1:02d}", "symbol": f"S{k}",
                         "score": k % 4, "liquid": 1, "gap_pct": 0.0,
                         "atr_pct": float(k), "vol_ratio20": float(k),
                         "fwd_1": float(k), "fwd_3": 0.0, "fwd_5": 0.0,
                         "fwd_up_1": float(-k), "fwd_range_1": float(k)})
    out = eventstudy.analyze(_long(rows), cost_pct=0.28)
    by = out["ic_by_target"]
    assert set(by) == set(panel.TARGETS)
    ic_of = lambda t, f: next(r["mean_ic"] for r in by[t] if r["feature"] == f)  # noqa: E731
    assert ic_of("fwd_1", "vol_ratio20") == pytest.approx(1.0, abs=0.01)
    assert ic_of("fwd_up_1", "vol_ratio20") == pytest.approx(-1.0, abs=0.01)


def test_targets_and_threshold_reported():
    out = eventstudy.analyze(_regime_rows(n_days=60), cost_pct=0.28)
    assert out["beta_control"] == "atr_pct"
    assert out["bonferroni_t"] == eventstudy.bonferroni_t(len(panel.IC_FEATURES))


# --- ⑧ 베타 잔차화 — 횡단면 demeaning 은 베타를 지우지 못한다 ---

def test_bonferroni_threshold_values():
    assert eventstudy.bonferroni_t(1) == 1.96
    assert eventstudy.bonferroni_t(7) == 2.69
    assert eventstudy.bonferroni_t(7) > eventstudy.bonferroni_t(3)


def _resid_of(out, feature, key="resid_ic"):
    return next((r.get(key) for r in out["ic"] if r["feature"] == feature), None)


def _down_skewed_beta(n_days=80, seed=3):
    """**하락 편중** 표본에 순수 베타 피처를 심는다.

    실제 표본이 이 모양이었다(190일 중 하락 107 / 상승 83, 시장 평균 −0.22%).
    상승·하락이 균형을 이루면 베타 효과가 상쇄돼 평균 IC 가 0으로 나온다 —
    그래서 원시 IC 만 봐서는 판별이 안 되고, 하락 편중일 때 강하게 보인다.
    바로 그 상황에서 잔차화가 베타를 걸러 내는지를 확인한다.
    """
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(n_days):
        mkt = -2.0 if d % 5 else 1.0          # 5일 중 4일 하락
        for k in range(30):
            atr = 1.0 if k < 15 else 5.0       # 저변동성 / 고변동성
            rows.append({
                "date": f"2026-{1 + d // 28:02d}-{1 + d % 28:02d}",
                "symbol": f"S{k}", "score": 1, "liquid": 1, "gap_pct": 0.0,
                "atr_pct": atr,
                "vol_ratio20": atr + rng.normal(0, 0.3),
                "ret_1d": mkt,
                "fwd_1": mkt * (0.5 if atr == 1.0 else 1.5) + rng.normal(0, 0.05),
                "fwd_3": 0.0, "fwd_5": 0.0,
            })
    return pd.DataFrame(rows)


def test_residual_kills_pure_beta():
    """저변동성이 시장을 덜 따라간 것뿐이라면 잔차 IC 는 0 으로 죽어야 한다.

    이게 이번 변경의 핵심 판별력이다 — 원시 IC 는 강하게 나오지만 그 강도가
    변동성으로 전부 설명되면 알파가 아니다.
    """
    out = eventstudy.analyze(_down_skewed_beta(), cost_pct=0.28)
    raw = abs(_resid_of(out, "vol_ratio20", "mean_ic"))
    resid = abs(_resid_of(out, "vol_ratio20"))
    assert raw > 0.3, f"원시 IC 가 약해 판별이 무의미하다({raw})"
    assert resid < raw / 3, f"잔차화가 베타를 못 걸렀다(원시 {raw} → 잔차 {resid})"


def test_residual_keeps_real_alpha():
    """변동성과 무관한 신호는 잔차화 후에도 살아남아야 한다."""
    rng = np.random.default_rng(11)
    rows = []
    for d in range(40):
        for k in range(30):
            # atr 은 신호와 무관하게 섞고, 신호(vol_ratio20)만 수익률과 단조 관계.
            # 일별 잡음이 없으면 IC 표준편차가 0이라 t 값이 정의되지 않는다.
            rows.append({"date": f"2026-{1 + d // 28:02d}-{1 + d % 28:02d}",
                         "symbol": f"S{k}", "score": 1, "liquid": 1, "gap_pct": 0.0,
                         "atr_pct": float((k * 7) % 30), "vol_ratio20": float(k),
                         "ret_1d": 0.1, "fwd_1": float(k) + rng.normal(0, 1.5),
                         "fwd_3": 0.0, "fwd_5": 0.0})
    out = eventstudy.analyze(_long(rows), cost_pct=0.28)
    assert _resid_of(out, "vol_ratio20") > 0.7
    assert abs(_resid_of(out, "vol_ratio20", "resid_t")) > 2


def test_control_feature_is_flagged_not_residualized():
    """대조축을 자기 자신에 회귀시키면 잔차가 0이다 — 계산하지 않고 표시만."""
    out = eventstudy.analyze(_regime_rows(n_days=60), cost_pct=0.28)
    ctrl = next(r for r in out["ic"] if r["feature"] == "atr_pct")
    assert ctrl["is_control"] is True
    assert "resid_ic" not in ctrl
    other = next(r for r in out["ic"] if r["feature"] == "vol_ratio20")
    assert other["is_control"] is False


def test_resid_spearman_guards():
    g = pd.DataFrame({"a": [1.0] * 30, "atr_pct": [2.0] * 30, "fwd_1": [3.0] * 30})
    assert np.isnan(eventstudy._resid_spearman(g, "a"))          # 분산 0
    small = pd.DataFrame({"a": [1.0, 2.0], "atr_pct": [1.0, 2.0], "fwd_1": [1.0, 2.0]})
    assert np.isnan(eventstudy._resid_spearman(small, "a"))      # 표본 부족
    assert np.isnan(eventstudy._resid_spearman(g, "atr_pct"))    # 자기 자신


def test_spearman_target_parameterised():
    g = pd.DataFrame({"a": range(30), "fwd_1": range(30),
                      "fwd_up_1": range(29, -1, -1)})
    assert eventstudy._spearman(g, "a", "fwd_1") == pytest.approx(1.0)
    assert eventstudy._spearman(g, "a", "fwd_up_1") == pytest.approx(-1.0)
    assert np.isnan(eventstudy._spearman(g, "a", "없는목적어"))


# --- ⑨ 배선 ---

def test_job_knows_study():
    from app.backtest import job

    assert "study" in job.JOBS


def test_run_once_without_data(monkeypatch, tmp_path):
    """일봉이 없으면 예외가 아니라 사유를 돌려준다."""
    monkeypatch.setattr(eventstudy.store, "DB_PATH", tmp_path / "empty.db")
    import sqlite3

    with sqlite3.connect(tmp_path / "empty.db") as c:
        c.execute("CREATE TABLE bars (symbol TEXT, tf TEXT, ts TEXT, open REAL,"
                  " high REAL, low REAL, close REAL, volume INTEGER)")
    out = eventstudy.run_once()
    assert out["ok"] is False and "일봉" in out["error"]


def test_latest_before_first_run(monkeypatch, tmp_path):
    monkeypatch.setattr(eventstudy, "OUT_FILE", tmp_path / "none.json")
    assert eventstudy.latest()["run_ts"] is None


def test_save_and_latest_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr(eventstudy, "OUT_FILE", tmp_path / "es.json")
    eventstudy.save({"ok": True, "run_ts": "2026-07-27T18:00:00+09:00", "rows": 7})
    assert eventstudy.latest()["rows"] == 7


def test_build_panel_end_to_end():
    """일봉 긴 표 → 피처 패널 + 익일 수익률까지 실제로 이어지는지."""
    frames = []
    for code in ("000001", "000002"):
        b = _bars(seed=hash(code) % 100).reset_index(names="ts")
        b["symbol"] = code
        frames.append(b)
    daily = pd.concat(frames)
    out = eventstudy.build_panel(daily, settings.CONFIG["nightly"])
    assert set(out["symbol"]) == {"000001", "000002"}
    assert {"date", "score", "liquid", "fwd_1", "gap_pct"} <= set(out.columns)
    assert out["fwd_1"].notna().all()      # 미래가 없는 마지막 행은 떨어져 나간다


def test_config_wires_research_cost():
    assert settings.CONFIG["research"]["cost_pct"] > 0
