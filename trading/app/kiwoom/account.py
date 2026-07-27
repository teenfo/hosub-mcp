"""계좌평가잔고(kt00018) 응답 파싱.

키움 실응답 필드명 기준(2026-07 실측):
- 보유목록: acnt_evlt_remn_indv_tot
- 종목: stk_cd(A접두)·stk_nm·rmnd_qty·pur_pric·cur_prc·evlt_amt·evltv_prft·prft_rt
- 합계: tot_pur_amt·tot_evlt_amt·tot_evlt_pl·tot_prft_rt·prsm_dpst_aset_amt
"""


def _num(v, cast=int):
    """키움 숫자 문자열("000012345", "-00012", "12.34") → 숫자. 실패 시 0."""
    try:
        return cast(float(str(v).strip() or 0))
    except (TypeError, ValueError):
        return cast(0)


def _first(raw: dict, *keys, default=None):
    """실응답/문서 필드명이 갈리는 항목을 순서대로 시도한다."""
    for k in keys:
        if raw.get(k) not in (None, ""):
            return raw[k]
    return default


def parse_balance(raw: dict) -> dict:
    """kt00018 응답 → 화면용 요약. return_code 0 이 아니면 ok=False."""
    if raw.get("return_code") not in (0, "0", None):
        return {"ok": False, "error": raw.get("return_msg", "조회 실패")}
    holdings = []
    items = _first(raw, "acnt_evlt_remn_indv_tot", "acnt_evlt_remn_prst", default=[]) or []
    for it in items:
        qty = _num(_first(it, "rmnd_qty", "trde_able_qty", default=0))
        if qty <= 0:
            continue                                   # 전량 매도된 잔여행 제외
        holdings.append(
            {
                "code": str(it.get("stk_cd", "")).lstrip("A"),
                "name": it.get("stk_nm", ""),
                "qty": qty,
                "avg_price": _num(_first(it, "pur_pric", "avg_prc", default=0)),
                "cur_price": abs(_num(it.get("cur_prc"))),  # 현재가는 부호로 등락 표시됨
                "eval_amt": _num(it.get("evlt_amt")),
                "buy_amt": _num(it.get("pur_amt")),
                "pl_amt": _num(_first(it, "evltv_prft", "pl_amt", default=0)),
                "pl_rt": _num(_first(it, "prft_rt", "pl_rt", default=0), float),
                "sellable_qty": _num(it.get("trde_able_qty", qty)),
            }
        )
    return {
        "ok": True,
        "account_name": raw.get("acnt_nm", ""),
        "total_buy": _num(_first(raw, "tot_pur_amt", "tot_buy_amt", default=0)),
        "total_eval": _num(raw.get("tot_evlt_amt")),
        "total_pl": _num(_first(raw, "tot_evlt_pl", "tot_pl_amt", default=0)),
        "total_pl_rt": _num(_first(raw, "tot_prft_rt", "tot_pl_rt", default=0), float),
        "deposit_est": _num(raw.get("prsm_dpst_aset_amt")),
        "holdings": holdings,
    }
