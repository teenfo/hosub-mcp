"""계좌평가잔고(kt00018) 응답 파싱."""


def _num(v, cast=int):
    """키움 숫자 문자열("000012345", "-00012", "12.34") → 숫자. 실패 시 0."""
    try:
        return cast(float(str(v).strip() or 0))
    except (TypeError, ValueError):
        return cast(0)


def parse_balance(raw: dict) -> dict:
    """kt00018 응답 → 화면용 요약. return_code 0 이 아니면 ok=False."""
    if raw.get("return_code") not in (0, "0", None):
        return {"ok": False, "error": raw.get("return_msg", "조회 실패")}
    holdings = []
    for it in raw.get("acnt_evlt_remn_prst") or []:
        holdings.append(
            {
                "code": str(it.get("stk_cd", "")).lstrip("A"),
                "name": it.get("stk_nm", ""),
                "qty": _num(it.get("rmnd_qty")),
                "avg_price": _num(it.get("avg_prc")),
                "cur_price": abs(_num(it.get("cur_prc"))),  # 현재가는 부호로 등락 표시됨
                "eval_amt": _num(it.get("evlt_amt")),
                "pl_amt": _num(it.get("pl_amt")),
                "pl_rt": _num(it.get("pl_rt"), float),
            }
        )
    return {
        "ok": True,
        "account_name": raw.get("acnt_nm", ""),
        "total_buy": _num(raw.get("tot_buy_amt")),
        "total_eval": _num(raw.get("tot_evlt_amt")),
        "total_pl": _num(raw.get("tot_pl_amt")),
        "total_pl_rt": _num(raw.get("tot_pl_rt"), float),
        "deposit_est": _num(raw.get("prsm_dpst_aset_amt")),
        "holdings": holdings,
    }
