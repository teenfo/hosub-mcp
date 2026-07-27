"""랭킹 방식 비교 하네스 — "어떤 순서로 줄을 세워야 실제 R 이 나오는가".

여기서 지켜야 할 것도 결론이 아니라 **비교의 정직성**이다.
- 브래킷 근사가 보수적인가(손절·목표 동시 도달 시 손절 우선)
- walk-forward 가 미래를 보지 않는가 — 이게 무너지면 남는 건 순환 논리뿐이다
- 학습창이 모자란 구간을 성적에 넣지 않는가
- 대조군(random)이 재현되는가 — 흔들리면 방식 간 비교 자체가 성립하지 않는다
- 상위 N 이 실제로 살 수 있는 종목에서만 나오는가
"""
import numpy as np
import pandas as pd
import pytest

from app.research import ranking


# --- ① 브래킷 근사 — 순수 함수 ---
# stop 2.0% · target_r 1.5 → 목표는 +3.0%. 비용 0.28% → 0.14R.

def test_bracket_stop_only():
    assert ranking.bracket_r(up=1.0, dn=-3.0, close=-2.0, stop_pct=2.0,
                             target_r=1.5, cost_pct=0.28) == pytest.approx(-1.14)


def test_bracket_target_only():
    assert ranking.bracket_r(up=4.0, dn=-1.0, close=2.0, stop_pct=2.0,
                             target_r=1.5, cost_pct=0.28) == pytest.approx(1.36)


def test_bracket_both_hit_assumes_stop_first():
    """일봉으로는 순서를 알 수 없다 — 성적을 부풀리지 않는 쪽을 고른다."""
    both = ranking.bracket_r(up=4.0, dn=-3.0, close=1.0, stop_pct=2.0,
                             target_r=1.5, cost_pct=0.28)
    assert both == pytest.approx(-1.14)


def test_bracket_neither_hit_exits_at_close():
    assert ranking.bracket_r(up=1.0, dn=-1.0, close=1.0, stop_pct=2.0,
                             target_r=1.5, cost_pct=0.28) == pytest.approx(0.36)


def test_bracket_cost_is_scaled_to_stop_width():
    """손절이 좁을수록 같은 비용이 R 단위로는 더 크게 먹힌다."""
    tight = ranking.bracket_r(0.5, -0.5, 1.0, stop_pct=1.0, target_r=1.5, cost_pct=0.28)
    wide = ranking.bracket_r(0.5, -0.5, 1.0, stop_pct=4.0, target_r=1.5, cost_pct=0.28)
    assert (1.0 - tight) > (0.25 - wide)          # 0.28R 차감 vs 0.07R 차감
    assert tight == pytest.approx(1.0 - 0.28)
    assert wide == pytest.approx(0.25 - 0.07)


def test_bracket_guards_bad_input():
    nan = float("nan")
    assert np.isnan(ranking.bracket_r(1.0, -1.0, 1.0, 0.0, 1.5, 0.28))
    assert np.isnan(ranking.bracket_r(nan, -1.0, 1.0, 2.0, 1.5, 0.28))
    assert np.isnan(ranking.bracket_r(1.0, nan, 1.0, 2.0, 1.5, 0.28))
    assert np.isnan(ranking.bracket_r(1.0, -1.0, nan, 2.0, 1.5, 0.28))


# --- 가짜 패널 ---

def _dates(n):
    return [f"2026-{1 + d // 28:02d}-{1 + d % 28:02d}" for d in range(n)]


def _panel(n_days=30, n_sym=24, seed=5, flip_after=None):
    """IC_FEATURES 를 모두 갖춘 패널.

    `vol_ratio20` 만 결과와 단조 관계다. flip_after 를 주면 그 날짜 이후로
    관계가 뒤집힌다 — walk-forward 가 미래를 보는지 판별하는 데 쓴다.
    """
    rng = np.random.default_rng(seed)
    rows = []
    for d, day in enumerate(_dates(n_days)):
        sign = -1.0 if (flip_after is not None and d >= flip_after) else 1.0
        for k in range(n_sym):
            sig = k / (n_sym - 1)
            rows.append({
                "date": day, "symbol": f"S{k:03d}", "liquid": 1,
                "score": float(k % 4),
                "vol_ratio20": sig * 10 + rng.normal(0, 0.1),
                "near_high60_pct": 90 + rng.normal(0, 1),
                "rsi14": 50 + rng.normal(0, 5),
                "atr_pct": 1.0 + float(rng.random()) * 4,
                "ret_20d": float(rng.normal(0, 5)),
                "disparity20": 100 + float(rng.normal(0, 3)),
                "fwd_up_1": sign * sig * 6 + 1 + rng.normal(0, 0.2),
                "fwd_dn_1": -2.0 + rng.normal(0, 0.2),
                "fwd_1": sign * sig * 3 + rng.normal(0, 0.2),
            })
    return pd.DataFrame(rows)


# --- ② walk-forward 가 미래를 보지 않는다 ---

def test_wf_weights_ignore_future(monkeypatch):
    """미래를 크게 왜곡해도 그날 가중치는 변하지 않아야 한다."""
    monkeypatch.setattr(ranking, "TRAIN_DAYS", 10)
    df = _panel(n_days=30)
    dates = sorted(df["date"].unique())
    base = ranking.wf_weights(df, dates, 15)
    assert base, "가중치가 서지 않으면 판별이 무의미하다"

    tampered = df.copy()
    future = tampered["date"] >= dates[15]
    for col in ("fwd_up_1", "fwd_dn_1", "fwd_1", "vol_ratio20"):
        tampered.loc[future, col] *= -7.0
    assert ranking.wf_weights(tampered, dates, 15) == base


def test_wf_weights_match_truncated_history(monkeypatch):
    """그날까지 잘라낸 데이터로 계산한 값과 같아야 한다 — 재현 가능성의 정의."""
    monkeypatch.setattr(ranking, "TRAIN_DAYS", 10)
    df = _panel(n_days=30, flip_after=20)
    dates = sorted(df["date"].unique())
    truncated = df[df["date"] < dates[15]]
    assert ranking.wf_weights(df, dates, 15) == ranking.wf_weights(truncated, dates, 15)


def test_wf_weights_none_before_training_window(monkeypatch):
    monkeypatch.setattr(ranking, "TRAIN_DAYS", 10)
    df = _panel(n_days=20)
    dates = sorted(df["date"].unique())
    assert ranking.wf_weights(df, dates, 9) is None
    assert ranking.wf_weights(df, dates, 10) is not None


def test_wf_skips_days_without_training_window(monkeypatch):
    """학습창 미달 구간은 성적에 넣지 않는다."""
    monkeypatch.setattr(ranking, "TRAIN_DAYS", 10)
    monkeypatch.setattr(ranking, "MIN_EVAL_DAYS", 5)
    df = _panel(n_days=30)
    wf = ranking.evaluate(df, "composite_wf", 2.0, 1.5, 0.28, top_n=3)
    plain = ranking.evaluate(df, "score", 2.0, 1.5, 0.28, top_n=3)
    assert plain["days"] == 30
    assert wf["days"] == 20              # 앞 10일은 학습에만 쓰이고 평가되지 않는다


def test_wf_unreliable_when_too_few_eval_days(monkeypatch):
    """학습창을 빼고 나면 평가일이 모자란 경우 — 수치를 내놓지 않는다."""
    monkeypatch.setattr(ranking, "TRAIN_DAYS", 10)
    monkeypatch.setattr(ranking, "MIN_EVAL_DAYS", 20)
    out = ranking.evaluate(_panel(n_days=15), "composite_wf", 2.0, 1.5, 0.28, top_n=3)
    assert out["reliable"] is False
    assert "avg_r" not in out


def test_resid_weights_skip_control_feature():
    """대조축(atr_pct)은 자기 자신에 회귀시킬 수 없다 — 가중치에서 빠진다."""
    w = ranking.resid_weights(_panel(n_days=20))
    assert w and "atr_pct" not in w
    assert "vol_ratio20" in w


# --- ③ 대조군 재현성 ---

def test_random_is_reproducible(monkeypatch):
    monkeypatch.setattr(ranking, "MIN_EVAL_DAYS", 5)
    df = _panel(n_days=20)
    a = ranking.evaluate(df, "random", 2.0, 1.5, 0.28, top_n=3)
    b = ranking.evaluate(df, "random", 2.0, 1.5, 0.28, top_n=3)
    assert a == b


def test_random_differs_from_score(monkeypatch):
    """무작위가 점수 방식과 같은 결과를 내면 대조군 역할을 못 한다."""
    monkeypatch.setattr(ranking, "MIN_EVAL_DAYS", 5)
    df = _panel(n_days=20)
    rnd = ranking.evaluate(df, "random", 2.0, 1.5, 0.28, top_n=3)
    atr = ranking.evaluate(df, "atr", 2.0, 1.5, 0.28, top_n=3)
    assert rnd["avg_r"] != atr["avg_r"]


# --- ④ 상위 N 은 실제로 살 수 있는 종목에서만 ---

def _gate_panel(n_days=12, n_sym=20):
    """짝수 종목만 유동성 통과. **못 사는 쪽이 크게 이긴다** — 게이트가 새면 드러난다."""
    rows = []
    for day in _dates(n_days):
        for k in range(n_sym):
            liquid = k % 2 == 0
            rows.append({
                "date": day, "symbol": f"S{k:03d}", "liquid": int(liquid),
                "score": float(k), "atr_pct": float(k),
                "fwd_up_1": 1.0 if liquid else 20.0,
                "fwd_dn_1": -5.0 if liquid else -0.1,
                "fwd_1": -4.0 if liquid else 15.0,
            })
    return pd.DataFrame(rows)


@pytest.mark.parametrize("method", ["score", "atr", "liquid_only"])
def test_top_n_uses_only_tradable_symbols(monkeypatch, method):
    monkeypatch.setattr(ranking, "MIN_EVAL_DAYS", 5)
    out = ranking.evaluate(_gate_panel(), method, 2.0, 1.5, 0.28, top_n=3)
    # 유동성 통과분은 전부 손절(-1R - 0.14R). 못 사는 종목이 하나라도 섞이면 올라간다.
    assert out["avg_r"] == pytest.approx(-1.14)
    assert out["win_rate"] == 0.0


def test_random_control_uses_whole_universe(monkeypatch):
    """random 만 전 종목에서 뽑는다 — 유동성 게이트 자체의 값을 재기 위해서다."""
    monkeypatch.setattr(ranking, "MIN_EVAL_DAYS", 5)
    out = ranking.evaluate(_gate_panel(), "random", 2.0, 1.5, 0.28, top_n=3)
    assert out["avg_r"] > -1.14


def test_days_short_of_top_n_are_skipped(monkeypatch):
    """상위 N 을 채울 종목이 없는 날은 건너뛴다."""
    monkeypatch.setattr(ranking, "MIN_EVAL_DAYS", 1)
    df = _gate_panel(n_days=6, n_sym=4)      # 유동성 통과 2종목뿐
    out = ranking.evaluate(df, "score", 2.0, 1.5, 0.28, top_n=3)
    assert out["days"] == 0 and out["trades"] == 0


# --- ⑤ 집계 ---

def test_rank_series_rejects_unknown_method():
    with pytest.raises(ValueError):
        ranking.rank_series(_panel(n_days=1), "없는방식", None,
                            np.random.default_rng(0))


def test_z_of_constant_series_is_zero():
    z = ranking._z(pd.Series([3.0] * 5))
    assert (z == 0.0).all()


def test_analyze_covers_every_method_and_stop(monkeypatch):
    monkeypatch.setattr(ranking, "TRAIN_DAYS", 10)
    monkeypatch.setattr(ranking, "MIN_EVAL_DAYS", 5)
    out = ranking.analyze(_panel(n_days=30), [1.5, 2.0], 1.5, 0.28, top_n=3)
    assert len(out["rows"]) == len(ranking.METHODS) * 2
    assert out["best"]["avg_r"] == max(r["avg_r"] for r in out["rows"] if r["reliable"])


def test_analyze_reports_gap_versus_random(monkeypatch):
    """판정은 대조군 대비로 한다 — random 을 못 이기면 랭킹의 존재 이유가 없다."""
    monkeypatch.setattr(ranking, "TRAIN_DAYS", 10)
    monkeypatch.setattr(ranking, "MIN_EVAL_DAYS", 5)
    out = ranking.analyze(_panel(n_days=30), [2.0], 1.5, 0.28, top_n=3)
    rows = {r["method"]: r for r in out["rows"] if r["reliable"]}
    assert rows["random"]["vs_random"] == 0.0
    for m, r in rows.items():
        assert r["vs_random"] == pytest.approx(r["avg_r"] - rows["random"]["avg_r"])


def test_analyze_keeps_in_sample_and_walk_forward_side_by_side(monkeypatch):
    """둘의 격차가 곧 과적합의 크기다 — 하나만 내면 읽을 수 없다."""
    monkeypatch.setattr(ranking, "TRAIN_DAYS", 10)
    monkeypatch.setattr(ranking, "MIN_EVAL_DAYS", 5)
    out = ranking.analyze(_panel(n_days=30), [2.0], 1.5, 0.28, top_n=3)
    methods = {r["method"] for r in out["rows"]}
    assert {"composite_is", "composite_wf"} <= methods
    assert out["train_days"] == 10 and out["learn_target"] == "fwd_up_1"


def test_analyze_is_empty_safe():
    out = ranking.analyze(pd.DataFrame({"date": [], "liquid": []}), [2.0], 1.5, 0.28)
    assert out["best"] is None
    assert all(r["reliable"] is False for r in out["rows"])


def test_caveats_state_the_approximation():
    joined = " ".join(ranking.CAVEATS)
    assert "손절 우선" in joined and "in-sample" in joined
    assert "생존 편향" in joined


# --- ⑥ 배선 ---

def test_job_knows_rank():
    from app.backtest import job

    assert "rank" in job.JOBS


def test_run_once_without_data(monkeypatch, tmp_path):
    import sqlite3

    from app.research import eventstudy

    monkeypatch.setattr(eventstudy.store, "DB_PATH", tmp_path / "empty.db")
    with sqlite3.connect(tmp_path / "empty.db") as c:
        c.execute("CREATE TABLE bars (symbol TEXT, tf TEXT, ts TEXT, open REAL,"
                  " high REAL, low REAL, close REAL, volume INTEGER)")
    out = ranking.run_once()
    assert out["ok"] is False and "일봉" in out["error"]


def test_latest_before_first_run(monkeypatch, tmp_path):
    monkeypatch.setattr(ranking, "OUT_FILE", tmp_path / "none.json")
    assert ranking.latest()["run_ts"] is None


def test_save_and_latest_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr(ranking, "OUT_FILE", tmp_path / "rank.json")
    ranking.save({"ok": True, "run_ts": "2026-07-27T20:00:00+09:00", "top_n": 5})
    assert ranking.latest()["top_n"] == 5


def test_config_wires_stop_band():
    from app import settings

    rules = settings.CONFIG["rules"]
    assert rules["min_stop_pct"] < rules["max_stop_pct"]
