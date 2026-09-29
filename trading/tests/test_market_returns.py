"""시장수익률 날짜 키 — 2026-09-29 결함의 회귀 방지.

일봉 ts 가 `…T00:00:00+09:00` 로 저장되는데 SQLite `date(ts)` 가 UTC 로 환산해
**하루 앞당긴 날짜** 를 키로 썼다. 국면 적중률이 '오늘 판정 vs 다음 거래일' 로
채점됐고 금요일이 통째로 빠졌다.
"""
from zoneinfo import ZoneInfo

import pandas as pd

from app.data import store

KST = ZoneInfo("Asia/Seoul")


def _daily(closes: dict[str, float]) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz=KST) for d in closes])
    return pd.DataFrame({"open": list(closes.values()), "high": list(closes.values()),
                         "low": list(closes.values()), "close": list(closes.values()),
                         "volume": [1] * len(closes)}, index=idx)


def test_시장수익률_키는_KST_거래일이다(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "market.db")
    # 목(9/24) 100 → 금(9/25) 110 → 월(9/28) 99
    store.upsert_bars("A", "1d", _daily({"2026-09-24": 100.0, "2026-09-25": 110.0,
                                         "2026-09-28": 99.0}))
    got = store.market_returns()
    assert set(got) == {"2026-09-25", "2026-09-28"}, "금요일이 빠지거나 일요일이 생기면 안 된다"
    assert round(got["2026-09-25"], 6) == 10.0      # 금요일의 수익률은 금요일 키에
    assert round(got["2026-09-28"], 6) == -10.0


def test_since_는_KST_날짜로_자른다(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "market.db")
    store.upsert_bars("A", "1d", _daily({"2026-09-24": 100.0, "2026-09-25": 110.0,
                                         "2026-09-28": 99.0}))
    assert set(store.market_returns("2026-09-28")) == {"2026-09-28"}
