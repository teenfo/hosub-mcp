"""매물대·VPIN — 저장 분봉 기반 마감 관측 (연구 문서 §3.2·§6 반영).

둘 다 관측 전용이다: 신호·점수·승격·주문 경로에서 읽지 않는다.
값을 못 내면 ok=False — 0 으로 채우면 '계산했는데 0' 과 '못 쟀다' 가 섞인다.
"""
import numpy as np
import pandas as pd
import pytest

from app.research import profile as profile_mod
from app.research import vpin as vpin_mod


def _bars(prices, volumes, start="2026-08-03 09:00"):
    idx = pd.date_range(start, periods=len(prices), freq="min")
    c = pd.Series(prices, index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c * 1.001, "low": c * 0.999,
                         "close": c, "volume": volumes}, index=idx)


# --- 매물대 -------------------------------------------------------------------

def test_매물대_POC_는_거래가_몰린_가격대다():
    """10,000원 근처에서 거래량 대부분, 11,000원 근처는 소량."""
    prices = [10_000] * 80 + [11_000] * 20
    volumes = [50_000] * 80 + [1_000] * 20
    p = profile_mod.volume_profile(_bars(prices, volumes), bins=10)
    assert p["ok"]
    assert abs(p["poc"] - 10_000) < 200          # POC 가 두꺼운 쪽
    assert p["va_low"] <= 10_000 <= p["va_high"]  # 밸류에어리어가 그곳을 덮는다


def test_매물대_현재가_위치_플래그():
    prices = [10_000] * 80 + [11_000] * 20
    volumes = [50_000] * 80 + [1_000] * 20
    p = profile_mod.volume_profile(_bars(prices, volumes), bins=10)
    # 마지막 가격 11,000 은 두꺼운 매물대(10,000) 위다
    assert p["above_poc"] == 1
    assert p["close"] == pytest.approx(11_000)


def test_매물대_상위_레벨은_비중_내림차순():
    prices = list(np.linspace(10_000, 12_000, 100))
    volumes = [10_000] * 100
    p = profile_mod.volume_profile(_bars(prices, volumes), bins=8)
    shares = [lv["share"] for lv in p["levels"]]
    assert shares == sorted(shares, reverse=True)
    assert sum(shares) <= 1.0 + 1e-9


def test_매물대_표본_부족이면_ok_False():
    assert profile_mod.volume_profile(_bars([10_000] * 5, [1] * 5)) == {"ok": False}
    assert profile_mod.volume_profile(None) == {"ok": False}


def test_매물대_거래량_0_이면_ok_False():
    p = profile_mod.volume_profile(_bars([10_000] * 50, [0] * 50))
    assert p == {"ok": False}


# --- VPIN ---------------------------------------------------------------------

def test_vpin_한방향_체결은_높고_균형은_낮다():
    """단조 상승(전부 매수 분류) vs 지그재그(반반) — 문서의 '독성' 정의 그대로."""
    rng = np.random.default_rng(7)
    up = list(np.cumsum(np.abs(rng.normal(5, 1, 200))) + 10_000)   # 단조 상승
    zig = [10_000 + (5 if i % 2 == 0 else -5) for i in range(200)]  # 균형
    v = [10_000] * 200
    toxic = vpin_mod.vpin(_bars(up, v))
    calm = vpin_mod.vpin(_bars(zig, v))
    assert toxic["ok"] and calm["ok"]
    assert toxic["vpin"] > 0.8            # 전부 한 방향 → 1 에 가깝다
    assert calm["vpin"] < 0.4
    assert toxic["vpin"] > calm["vpin"]


def test_vpin_은_0과_1_사이다():
    rng = np.random.default_rng(11)
    prices = list(10_000 + np.cumsum(rng.normal(0, 3, 300)))
    vp = vpin_mod.vpin(_bars(prices, [5_000] * 300))
    assert vp["ok"] and 0.0 <= vp["vpin"] <= 1.0


def test_vpin_가격_불변이면_ok_False():
    """σ=0 — 나눗셈이 성립하지 않는다. 0 이 아니라 '못 쟀다' 다."""
    assert vpin_mod.vpin(_bars([10_000] * 100, [1_000] * 100)) == {"ok": False}


def test_vpin_표본_부족이면_ok_False():
    assert vpin_mod.vpin(_bars([10_000, 10_010], [1, 1])) == {"ok": False}


# --- 수집기 -------------------------------------------------------------------

def test_수집기는_원장에_쓰고_실패를_격리한다(tmp_path, monkeypatch):
    import asyncio

    from app import settings
    from app.scout import bars_obs, store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "scout.db")
    monkeypatch.setattr(settings, "WATCHLIST", {"000001": "정상", "000002": "빈봉"})
    monkeypatch.setattr(settings, "CONFIG", {"scout": {"observe": {"bars": {}}}})

    rng = np.random.default_rng(3)
    good = _bars(list(10_000 + np.cumsum(rng.normal(0, 5, 390))),
                 [10_000] * 390, start="2026-08-03 09:00")
    good = good.rename_axis("ts")

    def fake_load(symbol, tf="1m", limit=2000):
        return good if symbol == "000001" else pd.DataFrame()

    from app.data import store as bars_store
    monkeypatch.setattr(bars_store, "load_bars", fake_load)

    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime(2026, 8, 3, 16, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    got = asyncio.run(bars_obs.collect_once(now))
    assert got["symbols"] == 2
    assert got["profile"] == 1 and got["vpin"] == 1   # 빈 봉 종목은 못 재고 넘어간다
    rows = store.profile_rows("2026-08-03")
    assert len(rows) == 1 and rows[0]["code"] == "000001"
    assert rows[0]["poc"] is not None
    vrows = store.vpin_rows(days=3650)
    assert len(vrows) == 1 and 0 <= vrows[0]["vpin"] <= 1
