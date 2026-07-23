from app.kiwoom.account import parse_balance

SAMPLE = {
    "return_code": 0,
    "return_msg": "정상적으로 처리되었습니다",
    "acnt_nm": "위탁종합",
    "tot_buy_amt": "000010000000",
    "tot_evlt_amt": "000010500000",
    "tot_pl_amt": "000000500000",
    "tot_pl_rt": "5.00",
    "prsm_dpst_aset_amt": "000020000000",
    "acnt_evlt_remn_prst": [
        {
            "stk_cd": "A005930",
            "stk_nm": "삼성전자",
            "rmnd_qty": "000000000010",
            "avg_prc": "000000250000",
            "cur_prc": "-000000268500",  # 부호는 등락 표시
            "evlt_amt": "000002685000",
            "pl_amt": "000000185000",
            "pl_rt": "7.40",
        }
    ],
}


def test_parse_balance_summary():
    out = parse_balance(SAMPLE)
    assert out["ok"] is True
    assert out["account_name"] == "위탁종합"
    assert out["total_eval"] == 10_500_000
    assert out["total_pl"] == 500_000
    assert out["total_pl_rt"] == 5.0
    assert out["deposit_est"] == 20_000_000


def test_parse_balance_holding_fields():
    h = parse_balance(SAMPLE)["holdings"][0]
    assert h["code"] == "005930"          # A 접두 제거
    assert h["qty"] == 10
    assert h["cur_price"] == 268_500      # 등락 부호 제거
    assert h["pl_amt"] == 185_000
    assert h["pl_rt"] == 7.4


def test_parse_balance_negative_pl():
    raw = dict(SAMPLE, tot_pl_amt="-000000300000", tot_pl_rt="-3.00")
    out = parse_balance(raw)
    assert out["total_pl"] == -300_000    # 손실 부호 유지
    assert out["total_pl_rt"] == -3.0


def test_parse_balance_error_response():
    out = parse_balance({"return_code": 8005, "return_msg": "계좌 없음"})
    assert out["ok"] is False and "계좌" in out["error"]


def test_parse_balance_empty_holdings():
    out = parse_balance(dict(SAMPLE, acnt_evlt_remn_prst=[]))
    assert out["ok"] is True and out["holdings"] == []
