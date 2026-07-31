"""거래소 구분과 종목코드 접미사 — KRX / NXT / 통합.

## `stex_tp` 값을 실호출로 확정했다 (2026-07-31)

`client.py` 는 이 값을 "문서 미상 — 모의투자 호출로 검증 필요" 로 적어둔 채
`"1"` 을 쓰고 있었다. 같은 TR(ka10032)을 값만 바꿔 불러 응답의 종목코드로
확정했다.

    stex_tp=0  ->  000660          KRX
    stex_tp=1  ->  000660          KRX      <- 우리가 쓰던 값
    stex_tp=2  ->  005930_NX       NXT (넥스트레이드)
    stex_tp=3  ->  000660_AL       통합(ALL)

**즉 우리는 지금까지 KRX 만 보고 있었다.** 순위 3종·관측 2종·주문·잔고가 전부
KRX 다(주문은 `dmst_stex_tp: "KRX"`).

## 왜 정규화가 먼저인가

접미사를 안 벗기면 두 가지가 조용히 깨진다.

  `parse_stock_list` 는 `code.isdigit() and len(code) == 6` 이라 `_NX` 가 붙은
  코드를 **말없이 버린다** — 종목이 사라진 줄도 모른다.

  `parse_rank` 는 `lstrip("A")` 만 해서 `000660_AL` 이 **그대로 통과한다** —
  감시목록·분봉·주문에 존재하지 않는 코드가 흘러든다.

그래서 거래소를 늘리기 전에 코드 정규화가 있어야 한다. 여기서는 **벗기기만**
하고 아무 판단도 하지 않는다.
"""

KRX, NXT, ALL = "KRX", "NXT", "ALL"

# stex_tp 요청값 — 실호출로 확정
TP_KRX, TP_NXT, TP_ALL = "1", "2", "3"

_SUFFIX = {"_NX": NXT, "_AL": ALL}


def split_venue(code: str) -> tuple[str, str]:
    """`005930_NX` → `("005930", "NXT")` · `000660` → `("000660", "KRX")`.

    접미사가 없으면 KRX 로 읽는다. 응답이 접미사를 안 붙이는 경우가 KRX 뿐이기
    때문이다(stex_tp 0·1). 정규화만 하고 유효성 판정은 호출부에 맡긴다 —
    "6자리 숫자인가" 를 여기서 걸러 버리면 `parse_stock_list` 가 저지른 것과
    같은 침묵이 된다.
    """
    s = str(code or "").strip().lstrip("A")
    for suf, venue in _SUFFIX.items():
        if s.endswith(suf):
            return s[: -len(suf)], venue
    return s, KRX


def base_code(code: str) -> str:
    """거래소 접미사를 벗긴 6자리 코드."""
    return split_venue(code)[0]
