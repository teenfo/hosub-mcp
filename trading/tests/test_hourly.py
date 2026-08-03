"""시간당 발굴 관측 — 합성 봉·신규 판정·창·원장. 관측 전용이라 주문 경로가 없다."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app import settings
from app.scout import hourly
from app.scout import store as scout_store

KST = ZoneInfo("Asia/Seoul")


@pytest.fixture(autouse=True)
def _tmp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(scout_store, "DB_PATH", tmp_path / "scout.db")
    monkeypatch.setattr(hourly, "DB_PATH_FN", lambda: tmp_path / "discovery.db")
    monkeypatch.setattr(settings, "KIWOOM_APP_KEY", "k")
    return tmp_path


def _at(hhmm: str, day: int = 3) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 8, day, h, m, tzinfo=KST)


def _bars(n=100, close=10_000.0, vol=100_000, last_day="2026-08-02"):
    end = pd.Timestamp(last_day)
    idx = pd.date_range(end=end, periods=n, freq="D")
    return pd.DataFrame({"open": close, "high": close * 1.01,
                         "low": close * 0.99, "close": close,
                         "volume": vol}, index=idx)


# --- 창 ---

@pytest.mark.parametrize("hhmm,day,ok", [
    ("09:29", 3, False), ("09:30", 3, True), ("12:00", 3, True),
    ("15:00", 3, True), ("15:01", 3, False), ("11:00", 1, False),   # 토요일
])
def test_장중_창에서만_돈다(hhmm, day, ok):
    assert hourly.in_window(_at(hhmm, day)) is ok


# --- 합성 봉 ---

def test_오늘_봉이_붙고_거래량은_일할_환산된다():
    df = _bars()
    now = _at("10:00")           # 세션 60분 경과 = 390분의 약 15.4%
    snap = {"open": 10_100.0, "high": 10_500.0, "low": 10_050.0,
            "price": 10_400.0, "volume": 30_000}
    out = hourly.synth_today_bar(df, snap, now)
    assert len(out) == len(df) + 1
    last = out.iloc[-1]
    assert last.close == 10_400.0 and last.high == 10_500.0
    assert last.volume == int(30_000 / (60 / 390))       # 하루치로 환산

def test_같은_날_봉이_이미_있으면_교체한다():
    df = _bars(last_day="2026-08-03")     # 마지막 행이 오늘
    snap = {"open": 1.0, "high": 2.0, "low": 0.5, "price": 1.5, "volume": 10}
    out = hourly.synth_today_bar(df, snap, _at("10:00"))
    assert len(out) == len(df)            # 두 개가 되지 않는다
    assert out.iloc[-1].close == 1.5


def test_스냅샷_재료가_모자라면_원본_그대로():
    df = _bars()
    out = hourly.synth_today_bar(df, {"price": 1.0}, _at("10:00"))   # OHL 없음
    assert len(out) == len(df)


# --- 신규 판정 ---

def _mk_universe(tmp_path, rows):
    import sqlite3

    with sqlite3.connect(tmp_path / "discovery.db") as conn:
        conn.execute("CREATE TABLE chart_obs (date TEXT, code TEXT, name TEXT,"
                     " score REAL, feats TEXT)")
        conn.executemany("INSERT INTO chart_obs VALUES ('2026-08-02',?,?,?,'{}')",
                         rows)


def test_전일에_이미_점수가_있던_종목은_픽이_아니다(_tmp_db, monkeypatch):
    _mk_universe(_tmp_db, [("000001", "가", 0), ("000002", "나", 3)])
    from app.data import store as bars_store

    # 두 종목 다 오늘 합성으로 만점이 나오게 만든다 — 급증+돌파 프레임
    base = _bars(close=10_000.0, vol=100_000)
    monkeypatch.setattr(bars_store, "load_bars", lambda c, tf, limit: base.copy())
    monkeypatch.setattr(hourly, "compute_features",
                        lambda df: {"score": 3, "close": float(df.iloc[-1].close),
                                    "reasons": ["돌파"]})
    snap = {"open": 1.0, "high": 2.0, "low": 0.5, "price": 1.5, "volume": 10}
    n, picks = hourly._evaluate(hourly.universe(),
                                {"000001": snap, "000002": snap},
                                _at("10:30"), min_score=2)
    assert n == 2
    assert [p["code"] for p in picks] == ["000001"]      # 신규 충족만
    assert picks[0]["prev_score"] == 0


def test_원장은_픽_0건이어도_평가_사실을_남긴다(_tmp_db):
    scout_store.record_hourly([], evaluated=700, now=_at("10:30"))
    (d,) = scout_store.hourly_days()
    assert d["runs"] == 1 and d["picks"] == 0
    assert scout_store.hourly_latest() == []             # 마커 행은 픽이 아니다


def test_원장_적재와_최신_조회(_tmp_db):
    scout_store.record_hourly(
        [{"code": "000001", "name": "가", "score": 3, "prev_score": 0,
          "close": 1_000.0, "reasons": ["돌파", "거래량급증"]}],
        evaluated=700, now=_at("10:30"))
    scout_store.record_hourly(
        [{"code": "000002", "name": "나", "score": 2, "prev_score": 1,
          "close": 2_000.0, "reasons": []}],
        evaluated=700, now=_at("11:30"))
    latest = scout_store.hourly_latest()
    assert [r["code"] for r in latest] == ["000002"]     # 마지막 시점만
    (d,) = scout_store.hourly_days()
    assert d["runs"] == 2 and d["picks"] == 2 and d["codes"] == 2


@pytest.mark.asyncio
async def test_유니버스가_비면_조용히_0건(_tmp_db):
    got = await hourly.evaluate_once(_at("10:30"))
    assert got == {"evaluated": 0, "picks": 0}


@pytest.mark.asyncio
async def test_창_밖에서는_호출_자체를_안_한다(_tmp_db, monkeypatch):
    called = []

    class _C:
        async def watch_info(self, codes):
            called.append(codes)
            return {}

    import app.kiwoom.client as mod
    monkeypatch.setattr(mod, "client", _C())
    _mk_universe(_tmp_db, [("000001", "가", 0)])
    assert (await hourly.evaluate_once(_at("15:30")))["evaluated"] == 0
    assert called == []
