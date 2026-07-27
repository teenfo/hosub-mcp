"""kt00018 계좌평가잔고 파싱 — 실응답 필드명 기준 (회귀: 보유목록 누락)."""
from app.kiwoom.account import parse_balance

# 2026-07-27 실응답 형태 (숫자는 0패딩 문자열, 종목코드는 A접두)
REAL = {
    "return_code": 0,
    "tot_pur_amt": "000000000562400",
    "tot_evlt_amt": "000000000559945",
    "tot_evlt_pl": "-00000000003663",
    "tot_prft_rt": "-0.65",
    "prsm_dpst_aset_amt": "000000000560755",
    "acnt_evlt_remn_indv_tot": [
        {"stk_cd": "A002810", "stk_nm": "삼영무역", "rmnd_qty": "000000000000002",
         "pur_pric": "000000000023600", "cur_prc": "000000023250",
         "evlt_amt": "000000000046500", "pur_amt": "000000000047200",
         "evltv_prft": "-00000000000792", "prft_rt": "-1.68",
         "trde_able_qty": "000000000000002"},
        {"stk_cd": "A011200", "stk_nm": "HMM", "rmnd_qty": "000000000000001",
         "pur_pric": "000000000021500", "cur_prc": "-000000021530",
         "evlt_amt": "000000000021530", "pur_amt": "000000000021500",
         "evltv_prft": "000000000000033", "prft_rt": "0.15",
         "trde_able_qty": "000000000000001"},
        {"stk_cd": "A000000", "stk_nm": "전량매도분", "rmnd_qty": "000000000000000",
         "pur_pric": "0", "cur_prc": "0", "evlt_amt": "0", "pur_amt": "0",
         "evltv_prft": "0", "prft_rt": "0", "trde_able_qty": "0"},
    ],
}


def test_parse_real_response():
    d = parse_balance(REAL)
    assert d["ok"] is True
    assert d["total_buy"] == 562400            # tot_pur_amt (구 tot_buy_amt 아님)
    assert d["total_eval"] == 559945
    assert d["total_pl"] == -3663              # tot_evlt_pl
    assert d["total_pl_rt"] == -0.65
    assert d["deposit_est"] == 560755
    assert len(d["holdings"]) == 2             # 잔량 0 행은 제외


def test_holding_fields():
    h = parse_balance(REAL)["holdings"][0]
    assert h["code"] == "002810" and h["name"] == "삼영무역"    # A접두 제거
    assert h["qty"] == 2 and h["avg_price"] == 23600
    assert h["cur_price"] == 23250
    assert h["pl_amt"] == -792 and h["pl_rt"] == -1.68
    assert h["buy_amt"] == 47200 and h["sellable_qty"] == 2


def test_negative_current_price_is_abs():
    """현재가는 부호로 등락을 표시 — 절대값으로 정규화."""
    assert parse_balance(REAL)["holdings"][1]["cur_price"] == 21530


def test_legacy_field_names_still_work():
    """문서상 구 필드명 응답도 수용 (폴백)."""
    legacy = {"return_code": 0, "tot_buy_amt": "1000", "tot_evlt_amt": "1100",
              "tot_pl_amt": "100", "tot_pl_rt": "10.0",
              "acnt_evlt_remn_prst": [
                  {"stk_cd": "A005930", "stk_nm": "삼성전자", "rmnd_qty": "1",
                   "avg_prc": "1000", "cur_prc": "1100", "evlt_amt": "1100",
                   "pl_amt": "100", "pl_rt": "10.0"}]}
    d = parse_balance(legacy)
    assert d["total_buy"] == 1000 and len(d["holdings"]) == 1
    assert d["holdings"][0]["avg_price"] == 1000


def test_error_response():
    d = parse_balance({"return_code": 3, "return_msg": "조회 실패"})
    assert d["ok"] is False and "조회" in d["error"]
