import json

import numpy as np
import pandas as pd

from app import export, settings
from app.features import compute_features

CFG = {"vol_surge_ratio": 3.0, "near_high_ratio": 0.97, "min_price": 1_000,
       "min_trade_value_krw": 1_000_000_000, "min_score": 2}


def _daily(closes, volumes):
    idx = pd.date_range("2026-04-01", periods=len(closes), freq="B")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame(
        {"open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
         "volume": volumes}, index=idx,
    )


def test_compute_features_shape_and_values():
    closes = list(np.linspace(9000, 10000, 69)) + [10500]
    volumes = [200_000] * 69 + [1_000_000]
    f = compute_features(_daily(closes, volumes), CFG)
    assert f is not None
    assert f["close"] == 10_500
    assert f["vol_ratio20"] > 3.0
    assert f["ma_aligned"] == 1
    assert f["liquid"] == 1
    assert f["score"] >= 2
    assert f["change_pct"] > 0
    assert 0 <= f["rsi14"] <= 100


def test_compute_features_none_when_short():
    assert compute_features(_daily([10_000] * 30, [500_000] * 30), CFG) is None


def test_illiquid_still_has_features_but_not_liquid():
    # 저가·저거래대금이라도 피처는 나오고 liquid=0 (분석기가 자유 필터)
    f = compute_features(_daily([500] * 70, [100_000] * 70), CFG)
    assert f is not None
    assert f["liquid"] == 0


def test_screen_daily_matches_features(tmp_path):
    from app import discovery
    closes = list(np.linspace(9000, 10000, 69)) + [10500]
    volumes = [200_000] * 69 + [1_000_000]
    df = _daily(closes, volumes)
    score, reasons = discovery.screen_daily(df, CFG)
    f = compute_features(df, CFG)
    assert score == f["score"] and reasons == f["reasons"]


def test_write_dataset_creates_csv_and_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(export, "DATASET_DIR", tmp_path / "datasets")
    monkeypatch.setattr(settings, "CONFIG", {"export": {"keep_days": 30}})
    rows = [
        {"code": "005930", "name": "삼성전자", "close": 70000, "trade_value": 500_000,
         "change_pct": 1.2, "volume": 10, "vol_ratio20": 1.1, "near_high20_pct": 99.0,
         "near_high60_pct": 98.0, "off_low60_pct": 10.0, "ma5": 1, "ma20": 1, "ma60": 1,
         "ma_aligned": 1, "ma_aligned_new": 0, "ret_5d": 2.0, "ret_20d": 5.0,
         "ret_60d": 8.0, "rsi14": 60.0, "liquid": 1, "score": 2.0,
         "reasons": ["거래량 급증"]},
        {"code": "000660", "name": "SK하이닉스", "close": 130000, "trade_value": 900_000,
         "change_pct": -0.5, "volume": 7, "vol_ratio20": 0.9, "near_high20_pct": 95.0,
         "near_high60_pct": 90.0, "off_low60_pct": 5.0, "ma5": 1, "ma20": 1, "ma60": 1,
         "ma_aligned": 0, "ma_aligned_new": 0, "ret_5d": -1.0, "ret_20d": 2.0,
         "ret_60d": 3.0, "rsi14": 45.0, "liquid": 1, "score": 0.0, "reasons": []},
    ]
    manifest = export.write_dataset("2026-07-23", rows)
    assert manifest["symbol_count"] == 2
    csv_path = tmp_path / "datasets" / "2026-07-23" / "features.csv"
    df = pd.read_csv(csv_path)
    assert "reasons" not in df.columns          # 텍스트 목록은 CSV 에서 제외
    assert list(df["code"].astype(str)) == ["660", "5930"]  # 거래대금 내림차순
    # 매니페스트 재조회
    latest = export.latest_manifest()
    assert latest["date"] == "2026-07-23"
    assert "code" in latest["columns"]


def test_prune_keeps_only_recent(tmp_path, monkeypatch):
    monkeypatch.setattr(export, "DATASET_DIR", tmp_path / "datasets")
    monkeypatch.setattr(settings, "CONFIG", {"export": {"keep_days": 2}})
    for d in ["2026-07-20", "2026-07-21", "2026-07-22", "2026-07-23"]:
        export.write_dataset(d, [{"code": "005930", "name": "s", "close": 1, "trade_value": 1}])
    remaining = sorted(p.name for p in (tmp_path / "datasets").iterdir() if p.is_dir())
    assert remaining == ["2026-07-22", "2026-07-23"]
