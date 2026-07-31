"""개장 창(09:00~09:15) 관측 — 창을 열기 전에 재기부터 한다.

실측 2026-07-31: 09:03 진입 → 09:30 청산이 5거래일 평균 +0.367%(비용 차감)인데
날짜별로는 -1.553% ~ +1.932%, 승률 17.9% ~ 75.6%. **분산이 엣지의 4배**다.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app import settings
from app.data import store as bars_store
from app.scout import opening, store

KST = ZoneInfo("Asia/Seoul")
DAY = "2026-08-03"


def _bars(open_px, close_px, hi=None, lo=None, vol=100, rest_vol=50):
    """09:00~09:14 (15봉) + 09:15~09:29 (15봉) 하루치 축소판."""
    rows, idx = [], []
    for m in range(15):
        rows.append({"open": open_px, "high": hi or max(open_px, close_px),
                     "low": lo or min(open_px, close_px), "close": close_px,
                     "volume": vol})
        idx.append(datetime(2026, 8, 3, 9, m, tzinfo=KST))
    for m in range(15, 30):
        rows.append({"open": close_px, "high": close_px, "low": close_px,
                     "close": close_px, "volume": rest_vol})
        idx.append(datetime(2026, 8, 3, 9, m, tzinfo=KST))
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "scout.db")
    monkeypatch.setattr(opening, "_cfg", lambda: {"enabled": True, "min_symbols": 2})
    return monkeypatch


def _seed(monkeypatch, table: dict):
    monkeypatch.setattr(bars_store, "load_bars",
                        lambda code, tf="1m", limit=800: table.get(code, pd.DataFrame()))


def test_개장_폭과_급등_수를_센다(env):
    _seed(env, {
        "A": _bars(1000, 1040),        # +4.0%  → 급등(3%)
        "B": _bars(1000, 1060),        # +6.0%  → 급등(3%,5%)
        "C": _bars(1000, 990),         # -1.0%
        "D": _bars(1000, 1005),        # +0.5%
    })
    s = opening.window_stats(["A", "B", "C", "D"], DAY)
    assert s["n"] == 4
    assert s["up_ratio"] == 75.0           # 4종목 중 3종목 상승
    assert s["surge3"] == 2 and s["surge5"] == 1
    assert s["median_chg"] == pytest.approx(2.25)


def test_RVOL_은_그날_안에서_비교한다(env):
    """종목 규모에 무관해야 한다 — 창 평균 / 창 뒤 평균."""
    _seed(env, {"A": _bars(1000, 1010, vol=300, rest_vol=100),      # 3.0
                "B": _bars(1000, 1010, vol=50, rest_vol=50)})       # 1.0
    s = opening.window_stats(["A", "B"], DAY)
    assert s["mean_rvol"] == pytest.approx(2.0)


def test_표본_미달인_날은_남기지_않는다(env):
    """재시작·백필 실패로 몇 종목만 잡힌 날의 집계가 다른 날과 같은 무게로
    읽히면 4주 뒤 판정이 오염된다."""
    _seed(env, {"A": _bars(1000, 1010)})
    assert opening.window_stats(["A"], DAY) is None


def test_봉이_없거나_모자란_종목은_빠진다(env):
    _seed(env, {"A": _bars(1000, 1010), "B": _bars(1000, 1020),
                "C": pd.DataFrame()})
    s = opening.window_stats(["A", "B", "C"], DAY)
    assert s["n"] == 2


def test_창이_닫히기_전에는_재지_않는다(env, monkeypatch):
    """형성 중인 창을 재면 매 사이클 다른 값이 나온다."""
    _seed(monkeypatch, {"A": _bars(1000, 1010), "B": _bars(1000, 1020)})
    monkeypatch.setattr(settings, "WATCHLIST", {"A": {}, "B": {}}, raising=False)
    assert opening.collect_once(datetime(2026, 8, 3, 9, 10, tzinfo=KST)) is False
    assert opening.collect_once(datetime(2026, 8, 3, 9, 16, tzinfo=KST)) is True


def test_하루_한_행이고_덮어쓰지_않는다(env, monkeypatch):
    """09:15 한 시점의 상태가 이 행의 의미다. 재시작해 09:40 에 다시 쓰면
    같은 컬럼에 다른 시각의 값이 들어간다."""
    _seed(monkeypatch, {"A": _bars(1000, 1010), "B": _bars(1000, 1020)})
    monkeypatch.setattr(settings, "WATCHLIST", {"A": {}, "B": {}}, raising=False)
    assert opening.collect_once(datetime(2026, 8, 3, 9, 16, tzinfo=KST)) is True
    assert opening.collect_once(datetime(2026, 8, 3, 9, 40, tzinfo=KST)) is False
    assert len(store.opening_days()) == 1
