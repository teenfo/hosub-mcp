"""실현손익·체결내역 조회 응답 파싱 (ka10074 / ka10073 / ka10076).

## 왜 증권사 값을 받아오나

원장(`trade/ledger.py`)은 **비용을 모델로 근사**했다. 실측하니 세 군데가 틀렸다
(2026-07-28, 마키나락스 4주 3건 대조):

  수수료   모델 0.015%/편도(16원) vs 실제 **10원 고정**        → 과대
  거래세   모델 0.15%             vs 실제 **약 0.20%**         → 과소
  슬리피지 모델 0.05%×2 를 차감    vs **이미 체결가에 들어 있다** → 이중계상

셋이 서로 상쇄돼 합계는 비슷해 보였지만(3건 평균 67원 차이) 개별로는 전부
틀렸다. 백테스터에서는 맞는 공식이다 — 모델가로 시뮬레이션하니 슬리피지를
따로 빼야 한다. 실거래 원장에 그대로 쓴 것이 잘못이다.

## tdy_sel_pl 은 이미 순액이다

실측 확인: 마키나락스 26,850 → 27,750 × 4주.
  총액 (27,750 − 26,850) × 4 = 3,600
  수수료 20 + 거래세 222      =   242
  tdy_sel_pl                  = 3,358   ← 3,600 − 242

그래서 이 값에 비용을 **또 빼면 안 된다**. 기간 합계 `rlzt_pl` 도 일자별
`tdy_sel_pl` 의 단순 합이다(실측 −432 + −23,194 + 204,645 = 181,019).
"""


def _num(v, cast=int):
    """키움 숫자 문자열("000012345", "-00012", "+3.13") → 숫자. 실패 시 0."""
    try:
        return cast(float(str(v).strip().lstrip("+") or 0))
    except (TypeError, ValueError):
        return cast(0)


def _ok(raw: dict) -> bool:
    return raw.get("return_code") in (0, "0", None)


def parse_daily(raw: dict) -> dict:
    """ka10074 일자별실현손익 → 기간 합계 + 일자별.

    `realized` 는 수수료·거래세가 이미 차감된 **순 실현손익**이다.
    `commission`/`tax` 는 참고용으로 함께 싣는다 — 원장의 비용 모델이 얼마나
    어긋나는지 재는 데 쓴다.
    """
    if not _ok(raw):
        return {"ok": False, "error": raw.get("return_msg", "조회 실패")}
    days = [
        {
            "date": str(d.get("dt", "")),
            "buy_amt": _num(d.get("buy_amt")),
            "sell_amt": _num(d.get("sell_amt")),
            "realized": _num(d.get("tdy_sel_pl")),
            "commission": _num(d.get("tdy_trde_cmsn")),
            "tax": _num(d.get("tdy_trde_tax")),
        }
        for d in (raw.get("dt_rlzt_pl") or [])
    ]
    days.sort(key=lambda d: d["date"], reverse=True)
    return {
        "ok": True,
        "buy_amt": _num(raw.get("tot_buy_amt")),
        "sell_amt": _num(raw.get("tot_sell_amt")),
        "realized": _num(raw.get("rlzt_pl")),
        "commission": _num(raw.get("trde_cmsn")),
        "tax": _num(raw.get("trde_tax")),
        "days": days,
    }


def parse_symbol_realized(raw: dict) -> list[dict]:
    """ka10073 일자별종목별실현손익(기간) → 매도 건별.

    한 행이 **매도 한 건**이다. `buy_uv`(매입단가) → `cntr_pric`(체결단가) 로
    `cntr_qty` 주를 판 결과가 `realized`.
    """
    if not _ok(raw):
        return []
    out = []
    for d in (raw.get("dt_stk_rlzt_pl") or raw.get("dt_stk_div_rlzt_pl") or []):
        out.append({
            "date": str(d.get("dt", "")),
            "code": str(d.get("stk_cd", "")).lstrip("A"),
            "name": d.get("stk_nm", ""),
            "qty": _num(d.get("cntr_qty")),
            "buy_price": _num(d.get("buy_uv")),
            "sell_price": _num(d.get("cntr_pric")),
            "realized": _num(d.get("tdy_sel_pl")),
            "pl_rt": _num(d.get("pl_rt"), float),
            "commission": _num(d.get("tdy_trde_cmsn")),
            "tax": _num(d.get("tdy_trde_tax")),
        })
    return out


# 매수/매도 판별 — 실응답은 "+매수" / "-매도" 형태다. 부호가 아니라 글자를 본다
# (부호만 보면 표기가 바뀌었을 때 조용히 전부 매수로 읽힌다).
def _side(io_tp_nm: str) -> str:
    s = str(io_tp_nm or "")
    if "매도" in s:
        return "sell"
    if "매수" in s:
        return "buy"
    return ""


def parse_executions(raw: dict) -> list[dict]:
    """ka10076 체결내역 → 체결 건별.

    **`ord_no` 가 원장의 `positions.ord_no`/`exit_ord_no` 와 같은 키다.**
    조인 키가 이미 갖춰져 있어서 추정가를 실체결가로 갈아끼울 수 있다.

    미체결(`cntr_qty` 0)은 버린다 — 주문은 있었지만 손익에 기여하지 않는다.
    """
    if not _ok(raw):
        return []
    out = []
    for d in (raw.get("cntr") or []):
        qty = _num(d.get("cntr_qty"))
        if qty <= 0:
            continue
        out.append({
            "ord_no": str(d.get("ord_no", "")).strip(),
            "code": str(d.get("stk_cd", "")).lstrip("A")[:6],
            "name": d.get("stk_nm", ""),
            "side": _side(d.get("io_tp_nm")),
            "price": _num(d.get("cntr_pric")),
            "qty": qty,
            "commission": _num(d.get("tdy_trde_cmsn")),
            "tax": _num(d.get("tdy_trde_tax")),
            "time": str(d.get("ord_tm", "")).strip(),
        })
    return out
