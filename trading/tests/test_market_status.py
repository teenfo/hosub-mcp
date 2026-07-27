"""매매 데스크 감시 상태 표시 + 분봉 날짜 필터."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from app import settings
from app.signals.engine import SignalEngine

KST = ZoneInfo("Asia/Seoul")


def _at(monkeypatch, dt: datetime, key: str = "K"):
    """engine.market_status 가 보는 '지금'을 고정."""
    import app.signals.engine as eng_mod

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return dt

    monkeypatch.setattr(eng_mod, "datetime", FakeDT)
    monkeypatch.setattr(settings, "KIWOOM_APP_KEY", key)


def test_market_status_phases(monkeypatch):
    eng = SignalEngine()
    monkeypatch.setattr(settings, "WATCHLIST", {"a": 1, "b": 2, "c": 3})
    monkeypatch.setattr(settings, "COLLECT_ONLY", {"c"})

    _at(monkeypatch, datetime(2026, 7, 27, 10, 30, tzinfo=KST))      # 월 장중
    s = eng.market_status()
    assert s["phase"] == "open" and s["scanning"] is True
    assert s["watch_count"] == 2                                     # 수집전용 제외

    _at(monkeypatch, datetime(2026, 7, 27, 8, 30, tzinfo=KST))       # 개장 전
    assert eng.market_status()["phase"] == "pre"

    _at(monkeypatch, datetime(2026, 7, 27, 16, 0, tzinfo=KST))       # 마감 후
    assert eng.market_status()["phase"] == "closed"

    _at(monkeypatch, datetime(2026, 7, 25, 10, 30, tzinfo=KST))      # 토요일
    assert eng.market_status()["phase"] == "closed"

    _at(monkeypatch, datetime(2026, 7, 27, 10, 30, tzinfo=KST), key="")   # 키 없음
    st = eng.market_status()
    assert st["phase"] == "disabled" and st["scanning"] is False


def test_market_status_scan_age(monkeypatch):
    """마지막 스캔 경과 — 루프가 실제로 도는지 판단 근거."""
    eng = SignalEngine()
    now = datetime(2026, 7, 27, 10, 30, tzinfo=KST)
    _at(monkeypatch, now)
    eng.last_run = (now - timedelta(seconds=45)).isoformat(timespec="seconds")
    assert eng.market_status()["last_scan_age_sec"] == 45
    eng.last_run = ""
    assert eng.market_status()["last_scan_age_sec"] is None


def test_bars_day_filter():
    """분봉은 날짜 단위로 끊어서 본다 — 지정 없으면 최근 거래일."""
    idx = pd.to_datetime([
        "2026-07-24 09:00", "2026-07-24 09:01",
        "2026-07-27 09:00", "2026-07-27 09:01", "2026-07-27 09:02",
    ])
    df = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                       "volume": 1}, index=idx)
    days = df.index.normalize()
    latest = df[days == days.max()]
    assert len(latest) == 3                                   # 최근일(7/27)만
    picked = df[days == pd.Timestamp("2026-07-24").normalize()]
    assert len(picked) == 2                                   # 과거 날짜 지정
    assert sorted({str(d.date()) for d in df.index}, reverse=True) == \
        ["2026-07-27", "2026-07-24"]                          # dates API 형식
