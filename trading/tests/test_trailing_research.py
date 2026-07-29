"""라인 추종 스윕 하네스 — 하네스 자신이 정직한가.

비교 결과를 믿으려면 **하네스가 변종을 실제로 다르게 재생하는지**부터 확인해야
한다. 값이 정해진 인공 봉으로 답을 미리 알고 맞춰본다.

여기서 검증하는 것은 규칙의 성적이 아니라 **측정 도구**다. 성적은 실 데이터로
`python -m app.backtest.job trailing` 을 돌려서 본다.
"""
from pathlib import Path

import pandas as pd
import pytest

from app import settings
from app.backtest import runner
from app.research import trailing
from app.trade import desk


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(settings, "COSTS", {"commission_pct": 0.015,
                                            "sell_tax_pct": 0.20,
                                            "slippage_bp": 5})
    monkeypatch.setattr(desk, "STATE_FILE", Path("/nonexistent/desk.json"))
    return settings


def _apply(variant):
    cfg = trailing._cfg_of(variant)
    settings.CONFIG.setdefault("execution", {})["desk"] = cfg or {"enabled": False}
    return cfg is not None


def _lines_at(variant, price, entry=10_000.0, stop=9_800.0, target=10_400.0):
    if not _apply(variant):
        return stop, target
    return trailing._lines(entry, stop, target, "long", stop, target, price)


# --------------------------------------------------------------------------
# 변종별 라인 계산 — 실제로 다른가
# --------------------------------------------------------------------------
def test_fixed_는_라인을_건드리지_않는다(env):
    assert _lines_at("fixed", 10_300) == (9_800.0, 10_400.0)


def test_trail_은_확정도_상한도_없다(env):
    """갭 추종만. 익절선까지 따라 올리는 옛 동작을 그대로 재현한다 —
    `trail_target` 을 켜야 비교 대상이 된다."""
    assert _lines_at("trail", 10_300) == (10_100.0, 10_700.0)


@pytest.mark.parametrize("n,want", [(30, 10_100.0), (50, 10_150.0),
                                    (90, 10_270.0)])
def test_lock_은_상승분의_N퍼센트를_확정한다(env, n, want):
    """진입 10,000 · 현재 10,300(상승 300) → 10,000 + 300×N%."""
    assert _lines_at(f"lock{n}", 10_300)[0] == want


def test_lock30_은_갭_추종보다_낮으면_갭_추종을_쓴다(env):
    """낮은 N 이 앞 단계보다 나빠지면 안 된다.

    10,150(상승 150): 확정선 10,045 vs 갭 추종 9,950 → 확정선이 높다.
    10,290(상승 290): 확정선 10,087 vs 갭 추종 10,090 → 갭 추종이 높다.
    """
    assert _lines_at("lock30", 10_290)[0] == 10_090.0


def test_격자가_서로_다른_값을_만든다(env):
    got = {v: _lines_at(v, 10_300)[0] for v in trailing.variants()}
    assert len(set(got.values())) >= 5, "같은 값이면 스윕이 무의미하다"


def test_스윕_격자는_30에서_시작한다(env):
    assert trailing.LOCK_GRID[0] == 30
    assert list(trailing.LOCK_GRID) == sorted(trailing.LOCK_GRID)


# --------------------------------------------------------------------------
# 재생 — 답을 아는 봉으로
# --------------------------------------------------------------------------
def _bars(closes, day="2026-07-20"):
    """분봉 프레임. 고가·저가를 종가와 같게 둬서 봉내 순서 문제를 없앤다."""
    idx = pd.date_range(f"{day} 09:00", periods=len(closes), freq="1min",
                        tz="Asia/Seoul")
    return pd.DataFrame({"open": closes, "high": closes, "low": closes,
                         "close": closes, "volume": [1000] * len(closes)},
                        index=idx)


def _replay_one(variant, closes, stop=9_800.0, target=10_400.0):
    """진입을 직접 심어 청산만 본다(신호 규칙을 타지 않는다)."""
    df = _bars(closes)
    bars = list(df.itertuples())
    update = _apply(variant)
    t = runner.Trade("005930", "orb", "long", bars[10].Index,
                     10_000.0, stop, target)
    cur_stop, cur_target = stop, target
    for bar in bars[11:]:
        if bar.low <= cur_stop:
            t.exit, t.exit_reason = cur_stop, "stop"
            break
        if bar.high >= cur_target:
            t.exit, t.exit_reason = cur_target, "target"
            break
        if update:
            cur_stop, cur_target = trailing._lines(
                10_000.0, stop, target, "long", cur_stop, cur_target,
                float(bar.close))
    if t.exit is None:                       # 장 마감 정리(replay 와 같은 규약)
        t.exit, t.exit_reason = float(bars[-1].close), "eod"
    return t


def test_오르다_되밀리면_추종이_고정보다_낫다(env):
    """추종의 존재 이유 — 이 경우가 아니면 추종은 값이 없다."""
    path = [10_000] * 11 + [10_100, 10_250, 10_290, 10_150, 9_900, 9_790]
    assert _replay_one("fixed", path).exit == 9_800.0
    got = _replay_one("lock30", path)
    assert got.exit_reason == "stop" and got.exit > 10_000


def test_곧장_손절이면_전부_같다(env):
    """반등 없이 떨어지면 추종은 할 일이 없다 — 다르면 하네스가 이상한 것이다."""
    path = [10_000] * 11 + [9_950, 9_900, 9_790]
    got = {v: _replay_one(v, path).exit for v in trailing.variants()}
    assert set(got.values()) == {9_800.0}


def test_높은_N_일수록_더_일찍_잘린다(env):
    """확정 비율이 높을수록 손절선이 가격에 붙는다 — 되밀림에 먼저 걸린다.

    이 단조성이 깨지면 재생이 잘못된 것이다.
    """
    path = [10_000] * 11 + [10_200, 10_260, 10_230, 10_200, 10_150, 10_100]
    exits = {n: _replay_one(f"lock{n}", path).exit for n in (30, 60, 90)}
    assert exits[90] >= exits[60] >= exits[30]


def test_상한_때문에_고정보다_적게_먹는_경우도_있다(env):
    """정직하게 남긴다 — 한 방향으로만 좋은 규칙은 없다."""
    path = [10_000] * 11 + [10_200, 10_350, 10_500]
    assert _replay_one("fixed", path).exit == 10_400.0
    assert _replay_one("lock30", path).exit == 10_300.0     # 상한 +3%


# --------------------------------------------------------------------------
# 스캔·집계
# --------------------------------------------------------------------------
def test_신호_스캔은_한_번만_한다(env, monkeypatch):
    """스윕 비용이 여기서 갈린다. 변종마다 다시 돌리면 장중 서버를 먹는다."""
    calls = []
    real = trailing.rules.evaluate_all
    monkeypatch.setattr(trailing.rules, "evaluate_all",
                        lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    closes = [10_000 + (i % 7) * 30 for i in range(120)]
    trailing.run_symbol("005930", _bars(closes))
    n = len(calls)
    calls.clear()
    trailing.run_symbol("005930", _bars(closes), names=["fixed"])
    assert n == len(calls), "변종 수와 무관하게 스캔 횟수가 같아야 한다"


def test_analyze_는_봉이_모자라면_건너뛴다(env, monkeypatch):
    monkeypatch.setattr(trailing.store, "load_bars",
                        lambda *a, **k: pd.DataFrame())
    got = trailing.analyze(["005930", "000660"])
    assert got["symbols_used"] == 0 and got["symbols_skipped"] == 2
    assert set(got["all"]) == set(trailing.variants())


def test_analyze_는_공통_진입만_짝짓는다(env, monkeypatch):
    closes = [10_000 + (i % 7) * 30 for i in range(200)]
    monkeypatch.setattr(trailing.store, "load_bars", lambda *a, **k: _bars(closes))
    got = trailing.analyze(["005930"], min_bars=50)
    assert got["symbols_used"] == 1
    assert got["paired_trades"] <= min(
        got["all"][v]["trades"] for v in trailing.variants())


def test_최고값과_이웃평균을_함께_낸다(env):
    """12거래일 표본에서 격자 최고를 그대로 믿으면 표본에 맞춘 것이다."""
    stats = {"lock30": {"avg_r": 0.1}, "lock40": {"avg_r": 0.9},
             "lock50": {"avg_r": 0.1}, "lock60": {"avg_r": 0.5},
             "lock70": {"avg_r": 0.6}, "lock80": {"avg_r": 0.5},
             "lock90": {"avg_r": 0.1}}
    got = trailing._pick(stats, list(stats))
    assert got["by_avg_r"] == "lock40", "뾰족한 최고점"
    assert got["robust"] == "lock70", "이웃이 고르게 좋은 구간"
    assert got["agree"] is False, "둘이 다르면 그 사실이 경고다"
