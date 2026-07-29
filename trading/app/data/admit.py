"""수동 편입 게이트 — 사람이 종목을 넣을 때 최소 조건을 확인한다.

## 왜 필요한가

`POST /api/watchlist` 는 코드 형식과 종목명 해석만 하고 아무것도 보지 않았다.
그래서 거래가 거의 없는 종목, 이름만 맞는 폐지 예정 종목, 1주도 못 사는 고가주가
매매 tier 로 그대로 들어갔다. 감시목록에 들어가면 분봉 백필·WS 구독·신호 평가가
따라붙으므로, **편입은 공짜가 아니다**.

## 막지 않고 알린다

기준 미달이라고 무조건 거부하지 않는다. 수동 편입은 사람이 근거를 갖고 하는
일이고, 시스템이 모르는 이유가 있을 수 있다. 기준을 못 넘으면 **무엇이 모자란지
숫자로 돌려주고**, 사용자가 그래도 넣겠다면 `force` 로 넣는다. 그 사실은
로그에 남는다 — 나중에 "왜 이게 들어왔나" 를 물을 수 있어야 한다.

거절이 아니라 확인 절차인 이유가 하나 더 있다. 장 시작 전·주말에는 당일
거래대금이 0이다. 그때 유동성 기준으로 막으면 아무것도 못 넣는다.

## 판정 재료는 ka10095 한 콜

거래대금·체결강도·현재가·상하한가·시총이 한 행에 온다. 종목이 실재하지 않으면
행 자체가 비어 있으므로 '존재 확인' 도 같은 콜로 끝난다.
"""
from __future__ import annotations

import logging

from .. import settings

log = logging.getLogger("trading.admit")

DEFAULTS = {
    # 당일 거래대금 하한(백만원). 실측(2026-07-29 감시목록 89종목)에서 100억
    # 이상이 60종목이었다 — 10억은 "거래가 있기는 한가" 수준의 최소선이다.
    "min_trde_prica": 1_000,
    # ETF·ETN·스팩·우선주는 매매 대상이 아니다. 이름으로 거른다(마스터에
    # 증권종류 필드가 없다 — discovery.is_excluded 와 같은 방식).
    "exclude_kinds": True,
}

# 이름으로 거르는 종류. discovery.is_excluded 가 쓰는 규칙과 어긋나지 않게
# 최소한만 둔다 — 두 곳이 다르면 어느 쪽이 맞는지 알 수 없다.
_KIND_KEYWORDS = ("KODEX", "TIGER", "KBSTAR", "ARIRANG", "HANARO", "SOL ",
                  "PLUS ", "ACE ", "RISE ", "KOACT", "ETN", "스팩", "선물")


def cfg() -> dict:
    return {**DEFAULTS, **(settings.CONFIG.get("admit") or {})}


def _num(v) -> float:
    """키움 값 → 숫자. 가격에 붙는 전일대비 방향 부호(+/-)는 크기가 아니다."""
    s = str(v or "").replace(",", "").strip()
    if not s:
        return 0.0
    try:
        return abs(float(s))
    except ValueError:
        return 0.0


def is_fund_like(name: str) -> bool:
    """ETF·ETN·스팩류인가 — 이름으로 판정."""
    n = (name or "").upper()
    return any(k.upper() in n for k in _KIND_KEYWORDS)


def evaluate(row: dict | None, code: str, conf: dict | None = None) -> dict:
    """ka10095 한 행 → 편입 판정.

    반환: {ok, reasons[], metrics{}} — ok=False 여도 호출자가 force 로 넘길 수
    있으므로 **이유를 전부** 모아 돌려준다(첫 번째에서 멈추지 않는다). 무엇이
    얼마나 모자란지 한 번에 보여줘야 사용자가 판단한다.
    """
    conf = conf or cfg()
    if not row or not str(row.get("stk_nm", "")).strip():
        return {"ok": False, "fatal": True, "reasons": [f"{code} 종목을 찾을 수 없습니다"],
                "metrics": {}}

    name = str(row.get("stk_nm", "")).strip()
    prica = _num(row.get("trde_prica"))          # 백만원
    metrics = {
        "name": name,
        "price": _num(row.get("cur_prc")),
        "trde_prica": prica,
        "trde_qty": _num(row.get("trde_qty")),
        "cntr_str": _num(row.get("cntr_str")),
        "mac": _num(row.get("mac")),
        "upl_pric": _num(row.get("upl_pric")),
        "lst_pric": _num(row.get("lst_pric")),
    }
    reasons: list[str] = []
    floor = float(conf.get("min_trde_prica") or 0)
    # 장 시작 전·휴장일은 거래대금이 0이다. 그때 유동성으로 막으면 아무것도
    # 못 넣는다 — 거래가 아예 없는 상태는 '미달' 이 아니라 '판정 불가' 다.
    if floor > 0 and 0 < prica < floor:
        reasons.append(f"거래대금 {prica:,.0f}백만 < 기준 {floor:,.0f}백만")
    if conf.get("exclude_kinds", True) and is_fund_like(name):
        reasons.append(f"ETF·ETN·스팩류로 보입니다 ({name})")
    return {"ok": not reasons, "fatal": False, "reasons": reasons, "metrics": metrics}


def row_of(payload: dict, code: str) -> dict | None:
    """ka10095 응답에서 해당 종목 행을 꺼낸다."""
    for r in payload.get("atn_stk_infr") or []:
        if str(r.get("stk_cd", "")).strip() == code:
            return r
    return None
