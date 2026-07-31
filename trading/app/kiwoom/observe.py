"""관측 전용 TR — 장중 투자자별 매매(ka10063) · VI 발동종목(ka10054).

## 이 두 개는 신호를 내지 않는다

점수·승격 경로에 넣지 않고 원장에만 쌓는다. 체결강도·스프레드와 같은 규약이다.
1.5단계가 폐기한 실수는 "그럴듯해서 넣었다" 였다 — 발굴 3규칙 점수도 그렇게
들어갔고 1년 뒤 이벤트 스터디에서 알파가 아니라 베타 프록시로 판명됐다.
여기서 무엇이 유의한지는 **4주 뒤에** 잔차 IC 로 묻는다.

## 실호출로 확정한 것 (2026-07-31)

경로와 파라미터가 문서에서 짐작한 것과 달랐다. 둘 다 여러 경로에서 1504
(`해당 URI에서는 지원하는 API ID가 아닙니다`)를 받고서야 찾았다.

  ka10063  /api/dostk/mrkcond   배열 `opmr_invsr_trde`  **800행**
           필수 파라미터가 `invsr` 다 — `invsr_tp` 로 보내면 1511
           (`필수 입력 값에 값이 존재하지 않습니다`)
  ka10054  /api/dostk/stkinfo   배열 `motn_stk`         **100행**

## 부호가 두 번 붙어 온다

`sell_amt: "--1"` 이 실제 응답에 있다. 키움은 값에 전일대비 방향을 부호로
얹는데, 원래 음수인 필드에는 그것이 **겹친다**. `float("--1")` 은 예외라
기존 `_num` 은 조용히 0 을 돌려준다 — 순매도 1억이 '거래 없음' 이 된다.
가격 필드와 달리 여기서는 **부호가 정보**이므로 절댓값으로 뭉갤 수도 없다.
"""

from .quote import _num


def signed(v) -> float:
    """`--239` → -239 · `+430` → 430. 부호가 정보인 필드 전용.

    가격 필드는 `abs()` 로 읽지만(부호가 전일대비 방향이라 값과 무관),
    순매수 금액은 부호 자체가 사려는 쪽인지 팔려는 쪽인지를 말한다.

    **부호를 토글로 읽으면 안 된다.** 겹친 `--` 는 "음수의 음수" 가 아니라
    방향 표시와 값 자체의 부호가 같은 자리에 붙은 것이다. 실응답으로 검산된다 —
    `buy_amt "+430"` · `sell_amt "--239"` · `netprps_amt "+191"` 에서
    430 - 239 = 191 이다. 토글로 읽으면 매도가 +239 가 되어 순매수가 669 로
    부풀고, 부호가 곧 방향인 이 필드에서 그 오류는 결론을 뒤집는다.
    """
    s = str(v if v is not None else "").strip()
    if not s:
        return 0.0
    n = len(s) - len(s.lstrip("+-"))
    neg = "-" in s[:n]
    return -_num(s[n:]) if neg else _num(s[n:])


def parse_investor(raw: dict) -> list[dict]:
    """ka10063 → 종목별 순매수. 실패하면 빈 목록.

    `netprps_amt` 단위는 백만원이고, `_irds` 접미사는 직전 조회 대비 증감이다.
    증감을 함께 남기는 이유: 누적 순매수는 하루가 갈수록 커지기만 해서 "지금
    사고 있나" 를 말해 주지 않는다. 진입 시점 관측에 필요한 것은 증감 쪽이다.
    """
    if raw.get("return_code") not in (0, "0", None):
        return []
    out = []
    for row in raw.get("opmr_invsr_trde") or []:
        code = str(row.get("stk_cd", "")).strip()
        if not code:
            continue
        out.append({
            "code": code,
            "name": str(row.get("stk_nm", "")).strip(),
            "price": abs(_num(row.get("cur_prc"))),
            "change_pct": signed(row.get("flu_rt")),
            "net_amt": signed(row.get("netprps_amt")),
            "net_amt_delta": signed(row.get("netprps_amt_irds")),
            "net_qty": signed(row.get("netprps_qty")),
            "buy_amt": signed(row.get("buy_amt")),
            "sell_amt": signed(row.get("sell_amt")),
        })
    return out


def parse_vi(raw: dict) -> list[dict]:
    """ka10054 → VI 발동종목. 실패하면 빈 목록.

    VI(변동성완화장치)는 급변할 때 거래소가 2분간 단일가로 묶는 장치다.
    발동 중에 시장가를 던지면 우리가 생각한 가격에 체결되지 않는다.

    지금은 **겹치는지만 센다.** 5거래일 실측에서 우리 거래와 VI 발동종목의
    교집합은 0이었다 — 표본 하나로는 결론이 아니라 계속 재야 할 이유다.
    """
    if raw.get("return_code") not in (0, "0", None):
        return []
    out = []
    for row in raw.get("motn_stk") or []:
        code = str(row.get("stk_cd", "")).strip()
        if not code:
            continue
        out.append({
            "code": code,
            "name": str(row.get("stk_nm", "")).strip(),
            "motn_pric": abs(_num(row.get("motn_pric"))),
            "kind": str(row.get("viaplc_tp", "")).strip(),      # 동적/정적
            "dispty_rt": signed(row.get("dynm_dispty_rt")),
            "motn_time": str(row.get("trde_cntr_proc_time", "")).strip(),
            "release_time": str(row.get("virelis_time", "")).strip(),
            "motn_cnt": int(_num(row.get("vimotn_cnt"), int)),
        })
    return out
