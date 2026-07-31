"""ka10095 잔여 필드 — 63필드 중 9개만 읽고 나머지를 버리고 있었다.

추가 호출 0이다(승격 게이트가 이미 이 TR 을 부른다). 전부 관측이고 판단에 쓰지
않는다. 값을 못 만들면 키를 **빼고** 돌려준다 — 0 으로 채우면 '측정했는데 0' 과
'못 쟀다' 가 섞여 4주 뒤 평균이 조용히 아래로 끌린다.
"""
import json

from app.kiwoom.quote import extra_fields, parse_watch_info


ROW = {
    "stk_cd": "005930", "cur_prc": "-70000", "flu_rt": "-1.5",
    "cntr_str": "98.5", "trde_qty": "1000", "trde_prica": "70000000",
    "buy_1th_bid": "69900", "sel_1th_bid": "70000",
    "pri_buy_req": "34600", "pri_sel_req": "10000",
    "tot_buy_req": "500000", "tot_sel_req": "250000",
    "open_pric": "-69000", "high_pric": "72000", "low_pric": "-68000",
    "pred_trde_qty_pre": "180.5",
    "exp_cntr_pric": "70100", "exp_cntr_qty": "3000",
    "upl_pric": "91000",
}


def test_호가_불균형비():
    e = extra_fields(ROW)
    assert e["req_imbalance"] == 3.46          # 실측에서 본 값
    assert e["tot_imbalance"] == 2.0


def test_당일_위치():
    """이번 실측이 가리킨 값 — 결정 시점에 남기지 않으면 사후 재계산만 가능하다."""
    e = extra_fields(ROW)
    # 저 68,000 · 고 72,000 · 현재 70,000 → (70000-68000)/4000 = 0.5
    assert e["day_pos"] == 0.5
    assert e["day_range_pct"] == round(4000 / 68000 * 100, 4)
    assert e["from_open_pct"] == round(1000 / 69000 * 100, 4)


def test_예상체결과_상한가_거리():
    e = extra_fields(ROW)
    assert e["exp_price"] == 70100 and e["exp_qty"] == 3000
    assert e["limit_pos"] == round(21000 / 70000 * 100, 4)
    assert e["vol_vs_prev"] == 180.5


def test_부호는_전일대비_방향이라_절댓값으로_읽는다():
    """가격에 붙는 -는 하락 표시다. 그대로 쓰면 저가가 음수가 된다."""
    e = extra_fields(ROW)
    assert 0 <= e["day_pos"] <= 1
    assert e["day_range_pct"] > 0


def test_못_잰_값은_키가_아예_없다():
    e = extra_fields({"cur_prc": "1000"})
    for k in ("req_imbalance", "tot_imbalance", "day_pos", "exp_price",
              "limit_pos", "vol_vs_prev", "from_open_pct"):
        assert k not in e


def test_고가와_저가가_같으면_위치를_내지_않는다():
    """상한가 직행처럼 범위가 0인 날 — 0으로 나누지 않고 조용히 뺀다."""
    e = extra_fields({**ROW, "high_pric": "70000", "low_pric": "70000"})
    assert "day_pos" not in e and "day_range_pct" not in e


def test_묶음_파서에_그대로_실린다():
    out = parse_watch_info({"return_code": 0, "atn_stk_infr": [ROW]})
    q = out["005930"]
    assert q["price"] == 70000 and q["spread_pct"] is not None
    assert q["day_pos"] == 0.5 and q["req_imbalance"] == 3.46


def test_원장에_접혀_쌓인다(tmp_path, monkeypatch):
    from app.scout import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "scout.db")
    quotes = parse_watch_info({"return_code": 0, "atn_stk_infr": [ROW]})
    assert store.record_quotes(quotes, {"005930": "삼성전자"}) == 1
    assert store.record_quotes(quotes, {"005930": "삼성전자"}) == 1

    row = store.quote_stats()[0]
    assert row["n"] == 2
    assert row["day_pos_n"] == 2 and row["day_pos_avg"] == 0.5
    assert json.loads(row["extra_last"])["req_imbalance"] == 3.46


def test_못_잰_관측은_평균을_끌어내리지_않는다(tmp_path, monkeypatch):
    from app.scout import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "scout.db")
    store.record_quotes(parse_watch_info({"return_code": 0, "atn_stk_infr": [ROW]}))
    # 고=저 인 관측 — day_pos 를 못 낸다
    store.record_quotes(parse_watch_info(
        {"return_code": 0,
         "atn_stk_infr": [{**ROW, "high_pric": "70000", "low_pric": "70000"}]}))

    row = store.quote_stats()[0]
    assert row["n"] == 2
    assert row["day_pos_n"] == 1 and row["day_pos_avg"] == 0.5   # 0 으로 안 센다
