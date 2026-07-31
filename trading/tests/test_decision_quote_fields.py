"""결정 시점 실측 시세를 `decisions` 에 시점 고정으로 싣는다.

`quote_obs` 는 종목·날짜당 한 행으로 접히므로 남는 것이 그날 마지막 값이다.
물어야 할 것은 "이 종목을 올린 **그 순간** 당일 위치가 얼마였나" 이고, 이번
실측이 정확히 그 순간의 값을 가리켰다(진입가가 직전 30분 범위의 0.67 지점).
"""
import pytest

from app.kiwoom.quote import parse_watch_info
from app.scout import promote, store
from app.scout.model import DECISION_QUOTE_FIELDS
from app.scout.scoring import Candidate


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "scout.db")


ROW = {
    "stk_cd": "005930", "cur_prc": "-70000", "flu_rt": "-1.5", "cntr_str": "98.5",
    "trde_qty": "1000", "trde_prica": "70000000",
    "buy_1th_bid": "69900", "sel_1th_bid": "70000",
    "pri_buy_req": "34600", "pri_sel_req": "10000",
    "tot_buy_req": "500000", "tot_sel_req": "250000",
    "open_pric": "-69000", "high_pric": "72000", "low_pric": "-68000",
    "pred_trde_qty_pre": "180.5", "upl_pric": "91000",
}


def _cand(quote=None):
    c = Candidate(code="005930", name="삼성전자")
    c.score = 1.0
    c.sources = ["volume"]
    c.quote = quote
    return c


def test_스키마와_INSERT_가_같은_목록을_쓴다(db):
    """따로 나열하면 ALTER 는 되는데 INSERT 를 빼먹어 컬럼이 조용히 NULL 로
    남는다. 그 침묵은 4주 뒤 측정할 때야 드러난다."""
    with store._conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(decisions)")}
    assert set(DECISION_QUOTE_FIELDS) <= cols


def test_결정에_실측이_시점_고정으로_실린다(db):
    q = parse_watch_info({"return_code": 0, "atn_stk_infr": [ROW]})["005930"]
    d = promote._dec(_cand(q), "promote_collect", "none", "collect", "테스트")
    assert store.log_decisions([d], "full", True) == 1

    with store._conn() as conn:
        row = dict(conn.execute("SELECT * FROM decisions").fetchone())
    assert row["day_pos"] == 0.5                 # (70000-68000)/(72000-68000)
    assert row["req_imbalance"] == 3.46
    assert row["tot_imbalance"] == 2.0
    assert row["vol_vs_prev"] == 180.5
    assert row["cntr_str"] == 98.5
    assert row["spread_pct"] is not None
    assert row["limit_pos"] == round(21000 / 70000 * 100, 4)


def test_못_잰_값은_NULL_이지_0_이_아니다(db):
    """`extra_fields` 가 키를 아예 빼므로 여기서 0 으로 채우면
    '측정했는데 0' 과 '못 쟀다' 가 섞인다."""
    q = parse_watch_info({"return_code": 0, "atn_stk_infr": [
        {**ROW, "high_pric": "70000", "low_pric": "70000",   # 범위 0 → 위치 못 냄
         "pri_buy_req": "0", "pri_sel_req": "0"}]})["005930"]
    d = promote._dec(_cand(q), "promote_collect", "none", "collect", "테스트")
    store.log_decisions([d], "full", True)

    with store._conn() as conn:
        row = dict(conn.execute("SELECT * FROM decisions").fetchone())
    assert row["day_pos"] is None and row["req_imbalance"] is None
    assert row["cntr_str"] == 98.5               # 잰 값은 그대로 실린다


def test_시세가_아예_없어도_결정은_남는다(db):
    d = promote._dec(_cand(None), "demote", "trade", "collect", "테스트")
    assert store.log_decisions([d], "shadow", False) == 1
    with store._conn() as conn:
        row = dict(conn.execute("SELECT * FROM decisions").fetchone())
    assert all(row[f] is None for f in DECISION_QUOTE_FIELDS)
    assert row["action"] == "demote" and row["applied"] == 0


def test_예상체결은_싣지_않는다():
    """승격 판정은 in_session 안에서만 일어나 예상체결은 사실상 늘 비어 있다.
    늘 NULL 인 컬럼은 원장을 읽기 어렵게만 한다."""
    assert "exp_price" not in DECISION_QUOTE_FIELDS
    assert "exp_qty" not in DECISION_QUOTE_FIELDS
