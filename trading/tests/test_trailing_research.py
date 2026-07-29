"""라인 추종 검증 하네스 — 하네스 자신이 정직한가.

비교 결과를 믿으려면 **하네스가 세 정책을 실제로 다르게 재생하는지**부터
확인해야 한다. 값이 정해진 인공 봉으로 답을 미리 알고 맞춰본다.

여기서 검증하는 것은 규칙의 성적이 아니라 **측정 도구**다. 성적은 실 데이터로
`python -m app.research.trailing` 을 돌려서 본다.
"""
import pandas as pd
import pytest

from app import settings
from app.backtest import runner
from app.research import trailing


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(settings, "COSTS", {"commission_pct": 0.015,
                                            "sell_tax_pct": 0.20,
                                            "slippage_bp": 5})
    monkeypatch.setitem(settings.RULES, "max_hold_min", 0)
    return settings


def _entry(price=10_000.0, stop=9_800.0, target=10_400.0, side="long"):
    return trailing.Entry(day=None, rule="orb", side=side, idx=0, ts=None,
                          price=price, stop=stop, target=target)


# --------------------------------------------------------------------------
# 정책별 라인 계산 — 셋이 실제로 다른가
# --------------------------------------------------------------------------
def _lines_at(policy, price, stop=9_800.0, target=10_400.0):
    e = _entry()
    old = (settings.CONFIG.get("execution", {}) or {}).get("desk")
    from pathlib import Path

    from app.trade import desk
    old_file, desk.STATE_FILE = desk.STATE_FILE, Path("/nonexistent/x.json")
    settings.CONFIG.setdefault("execution", {})["desk"] = trailing._desk_cfg(policy)
    try:
        return trailing._lines(policy, e, stop, target, price)
    finally:
        desk.STATE_FILE = old_file
        if old is None:
            settings.CONFIG.get("execution", {}).pop("desk", None)
        else:
            settings.CONFIG["execution"]["desk"] = old


def test_fixed_는_라인을_건드리지_않는다(env):
    assert _lines_at("fixed", 10_300) == (9_800.0, 10_400.0)


def test_trail_은_좁히기와_상한이_없다(env):
    """진입 10,000 · 갭 200/400. 10,300 이면 50% 를 넘었지만 좁히지 않는다."""
    assert _lines_at("trail", 10_300) == (10_100.0, 10_700.0)


def test_desk_는_상승분을_확정하고_상한을_건다(env):
    """10,300 → 손절 10,000+300×0.9 = 10,270 · 목표 상한 10,300."""
    assert _lines_at("desk", 10_300) == (10_270.0, 10_300.0)


def test_세_정책이_서로_다르다(env):
    got = {p: _lines_at(p, 10_300) for p in trailing.POLICIES}
    assert len(set(got.values())) == 3, "같으면 비교가 무의미하다"


# --------------------------------------------------------------------------
# 재생 — 답을 아는 봉으로 맞춰본다
# --------------------------------------------------------------------------
def _bars(closes, day="2026-07-20"):
    """분봉 프레임. 고가·저가는 종가와 같게 둬서 봉내 순서 문제를 없앤다."""
    idx = pd.date_range(f"{day} 09:00", periods=len(closes), freq="1min",
                        tz="Asia/Seoul")
    return pd.DataFrame({"open": closes, "high": closes, "low": closes,
                         "close": closes, "volume": [1000] * len(closes)},
                        index=idx)


def _replay(policy, closes, entry_i=10, stop=9_800.0, target=10_400.0):
    """진입을 직접 심어 청산만 본다(신호 규칙을 타지 않는다)."""
    from pathlib import Path

    from app.trade import desk
    df = _bars(closes)
    bars = list(df.itertuples())
    e = _entry(stop=stop, target=target)
    t = runner.Trade("005930", "orb", "long", bars[entry_i].Index,
                     e.price, stop, target)
    cur_stop, cur_target = stop, target

    old = (settings.CONFIG.get("execution", {}) or {}).get("desk")
    old_file, desk.STATE_FILE = desk.STATE_FILE, Path("/nonexistent/x.json")
    settings.CONFIG.setdefault("execution", {})["desk"] = trailing._desk_cfg(policy)
    try:
        for bar in bars[entry_i + 1:]:
            if bar.low <= cur_stop:
                t.exit, t.exit_reason = cur_stop, "stop"
                break
            if bar.high >= cur_target:
                t.exit, t.exit_reason = cur_target, "target"
                break
            cur_stop, cur_target = trailing._lines(
                policy, e, cur_stop, cur_target, float(bar.close))
    finally:
        desk.STATE_FILE = old_file
        if old is None:
            settings.CONFIG.get("execution", {}).pop("desk", None)
        else:
            settings.CONFIG["execution"]["desk"] = old
    return t


def test_오르다_되밀리면_추종이_고정보다_낫다(env):
    """추종의 존재 이유 — 이 경우가 아니면 추종은 값이 없다."""
    path = [10_000] * 11 + [10_100, 10_250, 10_290, 10_150, 9_900, 9_790]
    fixed = _replay("fixed", path)
    trail = _replay("desk", path)
    assert (fixed.exit_reason, fixed.exit) == ("stop", 9_800.0)
    assert trail.exit_reason == "stop" and trail.exit > 10_000, \
        "따라 올라간 손절선에서 이익을 남기고 나와야 한다"


def test_곧장_손절이면_셋이_같다(env):
    """반등 없이 떨어지면 추종은 할 일이 없다 — 차이가 나면 하네스가 이상한 것이다."""
    path = [10_000] * 11 + [9_950, 9_900, 9_790]
    got = {p: _replay(p, path).exit for p in trailing.POLICIES}
    assert set(got.values()) == {9_800.0}


def test_계속_오르면_고정은_익절_추종은_상한에서_익절(env):
    """`trail` 은 상한이 없어 목표가가 달아난다 — 그래서 익절이 안 난다."""
    path = [10_000] * 11 + [10_200, 10_350, 10_500, 10_800, 11_000]
    assert _replay("fixed", path).exit == 10_400.0
    assert _replay("desk", path).exit == 10_300.0        # 상한 +3%
    assert _replay("trail", path).exit_reason != "target"


def test_상한_때문에_desk_가_고정보다_적게_먹는_경우도_있다(env):
    """정직하게 남긴다 — 한 방향으로만 좋은 규칙은 없다.

    쭉 오르기만 하면 고정 목표(10,400)가 상한(10,300)보다 위다.
    """
    path = [10_000] * 11 + [10_200, 10_350, 10_500]
    assert _replay("fixed", path).exit == 10_400.0
    assert _replay("desk", path).exit == 10_300.0


# --------------------------------------------------------------------------
# 집계
# --------------------------------------------------------------------------
def test_analyze_는_봉이_모자라면_건너뛴다(env, monkeypatch):
    monkeypatch.setattr(trailing.store, "load_bars",
                        lambda *a, **k: pd.DataFrame())
    got = trailing.analyze(["005930", "000660"])
    assert got["symbols_used"] == 0 and got["symbols_skipped"] == 2
    assert set(got["all"]) == set(trailing.POLICIES)


def test_analyze_는_공통_진입만_짝짓는다(env, monkeypatch):
    """청산이 빨라지면 다음 신호를 잡아 진입 자체가 달라진다 —
    그 차이까지 섞이면 '청산 정책의 효과' 를 읽을 수 없다."""
    closes = [10_000 + (i % 7) * 30 for i in range(200)]
    monkeypatch.setattr(trailing.store, "load_bars", lambda *a, **k: _bars(closes))
    got = trailing.analyze(["005930"], min_bars=50)
    assert got["symbols_used"] == 1
    assert got["paired_trades"] <= min(
        got["all"][p]["trades"] for p in trailing.POLICIES)
