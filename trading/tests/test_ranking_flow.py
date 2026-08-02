"""외국인/STV 랭킹 방식 — 하네스 편입(IC 통과 후 최종 관문)."""
import sqlite3
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from app.data import store as bars_store
from app.research import ranking
from app.scout import store as scout_store


def test_frgn_stv_는_그_컬럼_순서로_뽑는다():
    g = pd.DataFrame({"frgn_stv": [0.1, 0.5, np.nan, -0.2]})
    rng = np.random.default_rng(0)
    r = ranking.rank_series(g, "frgn_stv", None, rng)
    assert r.idxmax() == 1
    assert r.isna().sum() == 1          # NaN 은 보존 — evaluate 가 dropna 로 거른다


def test_유효_후보가_top_n_미만인_날은_건너뛴다():
    # 3일 × 6종목, frgn_stv 는 첫날만 값이 있고 top_n=5 미만(3개) → 그 날 스킵
    days = [f"2026-07-{d:02d}" for d in (21, 22, 23)]
    rows = []
    for di, d in enumerate(days):
        for c in range(6):
            rows.append({
                "date": d, "symbol": f"{c:06d}", "liquid": 1, "score": 1.0,
                "atr_pct": 3.0, "fwd_up_1": 1.0, "fwd_dn_1": -1.0, "fwd_1": 0.0,
                "frgn_stv": (0.1 * c if di == 0 and c < 3 else np.nan),
            })
    df = pd.DataFrame(rows)
    out = ranking.evaluate(df, "frgn_stv", stop_pct=2.0, target_r=1.5,
                           cost_pct=0.0, top_n=5)
    assert out["days"] == 0 and out["trades"] == 0


def test_attach_flow_는_수량_나누기_거래량이다(tmp_path, monkeypatch):
    monkeypatch.setattr(scout_store, "DB_PATH", tmp_path / "scout.db")
    monkeypatch.setattr(bars_store, "DB_PATH", tmp_path / "market.db")
    with sqlite3.connect(tmp_path / "scout.db") as conn:
        conn.execute("CREATE TABLE flow_obs (d TEXT, code TEXT, foreign_ REAL)")
        conn.execute("INSERT INTO flow_obs VALUES ('2026-07-21', '005930', 500)")
    with sqlite3.connect(tmp_path / "market.db") as conn:
        conn.execute("CREATE TABLE bars (symbol TEXT, tf TEXT, ts TEXT,"
                     " open REAL, high REAL, low REAL, close REAL, volume INTEGER)")
        conn.execute("INSERT INTO bars VALUES ('005930','1d','2026-07-21',"
                     "1,1,1,1,10000)")
    df = pd.DataFrame({"date": ["2026-07-21", "2026-07-21"],
                       "symbol": ["005930", "000660"]})
    n = ranking.attach_flow(df)
    assert n == 1
    assert df.loc[0, "frgn_stv"] == pytest.approx(0.05)   # 500 / 10000
    assert np.isnan(df.loc[1, "frgn_stv"])                # 수급 없는 종목은 NaN


def test_수급_DB_가_없으면_flow_섹션은_조용히_빠진다(tmp_path, monkeypatch):
    monkeypatch.setattr(scout_store, "DB_PATH", tmp_path / "없는.db")
    df = pd.DataFrame({"date": ["2026-07-21"], "symbol": ["005930"]})
    assert ranking.attach_flow(df) == 0
    assert "frgn_stv" not in df.columns
