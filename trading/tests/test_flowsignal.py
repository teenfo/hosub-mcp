"""정규화 수급 재현 측정 — 잔차 상관·심은 신호 검출·시총 적재."""
import sqlite3
from datetime import date, timedelta

import pandas as pd

from app import settings
from app.data import store as bars_store
from app.research import flowsignal
from app.scout import store as scout_store


def test_잔차상관은_베타만인_신호를_죽인다():
    # x 가 z(변동성)와 같으면 잔차에 남는 게 없다 — 상관은 0 근처여야 한다
    z = pd.Series([1.0, 2, 3, 4, 5, 6, 7, 8])
    y = pd.Series([2.0, 1, 4, 3, 6, 5, 8, 7])
    c = flowsignal._resid_corr(z.copy(), y, z)
    assert c is None or abs(c) < 0.15


def test_시총_적재(tmp_path, monkeypatch):
    monkeypatch.setattr(scout_store, "DB_PATH", tmp_path / "scout.db")
    n = flowsignal.record_caps([
        {"stk_cd": "005930", "stk_nm": "삼성전자", "mac": "3500000", "cur_prc": "-70000"},
        {"stk_cd": "", "mac": "1", "cur_prc": "1"},            # 코드 없음 — 버린다
        {"stk_cd": "000660", "stk_nm": "하이닉스", "mac": "0", "cur_prc": "100"},  # 시총 0
    ])
    assert n == 1
    with sqlite3.connect(tmp_path / "scout.db") as conn:
        row = conn.execute("SELECT code, mac, price FROM cap_obs").fetchone()
    assert row == ("005930", 3500000.0, 70000.0)


def _seed(tmp_path, monkeypatch, n_days=40, n_codes=25):
    """심은 신호: 외국인 순매수 = 익일 수익 × STV / 종가 → frgn_stv ∝ fwd_oc."""
    scout_db, market_db = tmp_path / "scout.db", tmp_path / "market.db"
    monkeypatch.setattr(scout_store, "DB_PATH", scout_db)
    monkeypatch.setattr(bars_store, "DB_PATH", market_db)
    monkeypatch.setitem(settings.CONFIG, "research",
                        {"flowsignal": {"days": 90, "min_cross_n": 10}})
    d0 = date.today() - timedelta(days=n_days + 2)
    days = [(d0 + timedelta(days=i)).isoformat() for i in range(n_days)]
    closes = {c: [100.0 + c] for c in range(n_codes)}
    rets = {}          # rets[(t, c)] = t일에 심는 '익일' 시가→종가 수익
    for t in range(n_days - 1):
        for c in range(n_codes):
            rets[(t, c)] = (((c * 7 + t * 3) % 9) - 4) / 100.0
    for c in range(n_codes):
        for t in range(n_days - 1):
            closes[c].append(closes[c][-1] * (1 + rets[(t, c)]))
    with sqlite3.connect(market_db) as conn:
        conn.execute("CREATE TABLE bars (symbol TEXT, tf TEXT, ts TEXT,"
                     " open REAL, high REAL, low REAL, close REAL, volume INTEGER,"
                     " PRIMARY KEY (symbol, tf, ts))")
        for c in range(n_codes):
            code = f"{c:06d}"
            for t, d in enumerate(days):
                cl = closes[c][t]
                op = closes[c][t - 1] if t else cl        # 익일 시가 = 전일 종가
                band = 0.01 + (c % 5) / 100               # 종목별 변동성 분산
                conn.execute("INSERT INTO bars VALUES (?,?,?,?,?,?,?,?)",
                             (code, "1d", d, op, cl * (1 + band), cl * (1 - band),
                              cl, 1000 + ((c * 5 + t) % 50) * 10))
    with sqlite3.connect(scout_db) as conn:
        conn.execute("CREATE TABLE flow_obs (d TEXT, code TEXT, name TEXT,"
                     " orgn REAL, foreign_ REAL, updated_at TEXT,"
                     " PRIMARY KEY (d, code))")
        conn.execute("CREATE TABLE cap_obs (code TEXT PRIMARY KEY, name TEXT,"
                     " mac REAL, price REAL, updated_at TEXT)")
        for c in range(n_codes):
            code = f"{c:06d}"
            shares = 1000.0 + c * 37
            conn.execute("INSERT INTO cap_obs VALUES (?,?,?,?,?)",
                         (code, f"종목{c}", shares * closes[c][-1],
                          closes[c][-1], "t"))
            for t, d in enumerate(days[:-1]):
                cl = closes[c][t]
                stv = cl * (1000 + ((c * 5 + t) % 50) * 10)
                frgn = rets[(t, c)] * stv / cl * 1000     # 심은 신호
                orgn = float(((c + t) % 5) - 2)           # 잡음
                conn.execute(
                    "INSERT INTO flow_obs VALUES (?,?,?,?,?,?)",
                    (d, code, f"종목{c}", orgn, frgn, "t"))


def test_심은_신호를_잡고_잡음은_잡지_않는다(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = flowsignal.analyze()
    assert out["ok"] and out["n_symbols"] == 25
    sig = out["signals"]
    # frgn_stv 는 fwd_oc 에 비례해 심었다 — 잔차 IC 가 강하게 양수
    assert sig["frgn_stv"]["resid_ic"] > 0.5
    assert out["verdict"]["frgn_stv_significant"] is True
    # 기관은 잡음이다 — 유의하지 않아야 한다
    assert abs(sig["inst_smc"]["resid_ic"] or 0) < 0.3
    assert out["verdict"]["inst_smc_significant"] is False


def test_ETF_는_유니버스에서_빠진다(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, n_codes=12)
    # 이름을 ETF 로 바꾸면 표본에서 빠져야 한다 (측정 원칙 5 — 유니버스 오염)
    with sqlite3.connect(tmp_path / "scout.db") as conn:
        conn.execute("UPDATE flow_obs SET name='KODEX 인버스' WHERE code='000001'")
    out = flowsignal.analyze()
    assert out["ok"] and out["n_symbols"] == 11


def test_표본이_없으면_실패를_말한다(tmp_path, monkeypatch):
    monkeypatch.setattr(scout_store, "DB_PATH", tmp_path / "빈.db")
    monkeypatch.setattr(bars_store, "DB_PATH", tmp_path / "빈2.db")
    out = flowsignal.analyze()
    assert out["ok"] is False
