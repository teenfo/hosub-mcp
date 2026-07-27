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


def _pick(df, date=None):
    """api_bars 의 날짜 필터와 동일한 로직 (문자열 비교 — tz 무관)."""
    days = [str(d.date()) for d in df.index]
    want = date if (date and date in days) else max(days)
    return df[[d == want for d in days]]


def _sample(tz=None):
    idx = pd.to_datetime([
        "2026-07-24 09:00", "2026-07-24 09:01",
        "2026-07-27 09:00", "2026-07-27 09:01", "2026-07-27 09:02",
    ])
    if tz:
        idx = idx.tz_localize(tz)
    return pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                         "volume": 1}, index=idx)


def test_bars_day_filter():
    """분봉은 날짜 단위 — 지정 없으면 최근 거래일, 지정하면 그 날짜."""
    df = _sample()
    assert len(_pick(df)) == 3                                # 최근일(7/27)만
    assert len(_pick(df, "2026-07-24")) == 2                  # 과거 날짜 지정
    assert len(_pick(df, "2026-07-01")) == 3                  # 데이터 없는 날 → 최근일
    assert sorted({str(d.date()) for d in df.index}, reverse=True) == \
        ["2026-07-27", "2026-07-24"]                          # dates API 형식


def test_bars_day_filter_tz_aware():
    """tz-aware 인덱스(KST 저장분)에서도 과거 날짜 조회가 동작해야 한다.

    (회귀: tz-naive Timestamp 와 비교해 0봉이 반환되던 버그)
    """
    df = _sample(tz="Asia/Seoul")
    assert len(_pick(df)) == 3
    assert len(_pick(df, "2026-07-24")) == 2


# --- 감시 주기 설정 ---

def test_scan_interval_clamped(monkeypatch):
    """UI 값은 안전 범위로 클램프 — 키움 레이트리밋 보호."""
    eng = SignalEngine()
    for raw, want in [(30, 30), (5, 10), (9999, 300), ("bad", 60), (None, 60)]:
        monkeypatch.setattr(settings, "RISK", {"scan_interval_sec": raw})
        assert eng.scan_interval() == want, raw
    monkeypatch.setattr(settings, "RISK", {})            # 미설정 → 기본 60
    assert eng.scan_interval() == 60


def test_save_risk_persists_scan_interval(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RISK_FILE", tmp_path / "risk.json")
    monkeypatch.setattr(settings, "RISK", {})
    settings.save_risk(scan_interval_sec=20)
    assert settings.RISK["scan_interval_sec"] == 20
    settings.RISK.clear()
    settings._load_risk_overrides()
    assert settings.RISK["scan_interval_sec"] == 20      # 재기동 후에도 유지


def test_save_risk_rejects_out_of_range(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RISK_FILE", tmp_path / "risk.json")
    monkeypatch.setattr(settings, "RISK", {})
    for bad in (1, 5, 301, 1000):
        try:
            settings.save_risk(scan_interval_sec=bad)
            raised = False
        except ValueError:
            raised = True
        assert raised, bad


def test_market_status_reports_configured_interval(monkeypatch):
    eng = SignalEngine()
    monkeypatch.setattr(settings, "RISK", {"scan_interval_sec": 15})
    _at(monkeypatch, datetime(2026, 7, 27, 10, 30, tzinfo=KST))
    assert eng.market_status()["scan_interval_sec"] == 15
