"""승격·강등 판정 — 감시목록을 **연속적으로** 갱신한다.

기존 방식은 스캔마다 source 전체를 갈아엎는 전량 교체다. 그래서 순위 경계의
종목이 60초마다 편입·이탈을 반복하고, 그때마다 WS 재구독이 일어난다.

## 매매 tier 승격은 점수로 하지 않는다

이 파일에서 가장 중요한 결정이다. 원안은 `promote_trade: 0.65` 점수 임계였다.
그런데 2026-07-27 실측에서 **점수 순 상위 N 은 매수가능 무작위보다 0.22R
나빴다**(191거래일 / 3,907종목). 점수로 매매를 여는 것은 근거가 없다.

그래서 매매 승격은 **게이트**로만 한다.

  ① 유동성 — 실제로 살 수 있는가
  ② 체결가능성 — `trade_tier_cap` 이하인가. **1주도 못 사는 종목을 매매
     대상에 올려 봐야 '잔고 부족' 만 쌓인다.** 지금 야간 발굴 편입 경로에는
     이 가드가 아예 없다(watchlist.replace_auto 의 INSERT 에 collect_only
     컬럼이 없어 DEFAULT 0 이 적용된다).
  ③ probation — 수집 tier 에서 최소 1세션 머물렀는가. 검증 안 된 소스가
     60초 만에 실거래로 이어지는 것을 막고, 부수적으로 **"감시했지만 매매하지
     않은" 대조군 표본**을 만든다.
  ④ 상한 — `max_trade` 를 넘지 않는가

점수는 상한을 넘었을 때 **누구를 먼저 뺄지**에만 쓴다.

## 강등에서 제외하는 것

  - 보유 포지션이 있는 종목. 감시목록에서 빠지면 WS 구독이 해제되고 청산
    감시가 최대 15분 묵은 가격으로 돈다(watchlist._held 참조 — 실거래 손실로
    이어졌던 결함이다).
  - seed/manual — 사용자 의도를 엔진이 덮어쓰지 않는다.
"""
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .. import settings
from .scoring import Candidate

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
# 장중 판정 — signals/engine.py·scanner.py 와 같은 조건이다. 그쪽은 실거래
# 경로라 이번 범위에서 건드리지 않고 여기 복제해 둔다(엔진이 그 루프들을
# 흡수하는 시점에 한 군데로 모은다).
SESSION = ("09:00", "15:30")


def in_session(now: datetime | None = None) -> bool:
    t = (now or datetime.now(UTC)).astimezone(KST)
    return t.weekday() < 5 and SESSION[0] <= t.strftime("%H:%M") <= SESSION[1]

# tier 이름 — decisions 에 그대로 들어간다
NONE, COLLECT, TRADE = "none", "collect", "trade"

DEFAULTS = {
    "promote_collect": 0.35,     # 이상 → 수집전용 후보
    "demote_below": 0.25,        # 미만 → 감시목록에서 제거 (승격 임계보다 낮다)
    "min_dwell_min": 15,         # 최소 체류 — 들락날락·WS 재구독 폭주 방지
    "probation_sessions": 1,     # 매매 승격 전 수집 tier 체류 세션 수
    "max_trade": 50,             # 매매 tier 상한
    "max_total": 120,            # 감시목록 전체 상한
}


def cfg() -> dict:
    return DEFAULTS | (settings.CONFIG.get("scout", {}) or {})


@dataclass
class Current:
    """지금 감시목록의 상태 — 투영기의 입력."""
    tier: dict[str, str]              # code → none|collect|trade
    since: dict[str, datetime]        # code → 현재 tier 진입 시각
    protected: frozenset              # 보유·seed/manual — 강등 금지
    names: dict[str, str]


def _tradable(c: Candidate, cap: float) -> tuple[bool, str]:
    """체결가능성 게이트. 가격을 모르면 **매매로 올리지 않는다**(보수적).

    '모른다' 에는 **낡은 값밖에 없는 경우**도 들어간다. nightly 는 발굴 배치일
    종가를, news 는 수집 시점 가격을 싣는다 — 실측 2026-07-28 펌텍코리아
    승격 결정의 '체결가능 48,600원' 이 어제 종가였고 당시 현재가는 47,050원
    (−3.19%)이었다. 상한 근처에서 갭이 크면 판정이 뒤집힌다.

    실측 시세를 못 받았으면 이번 사이클은 넘긴다. 30초 뒤 다시 온다 —
    틀린 값으로 매매를 여는 것보다 한 사이클 늦는 편이 싸다.

    **가격 상한은 더 이상 여기서 보지 않는다**(사용자 결정 2026-07-29).
    발굴엔진은 가격과 무관하게 매매 tier 로 올리고, 살 수 있는지는 매매 데스크가
    승인대기 단계에서 판정한다. 여기서 막으면 비싼 종목은 신호가 났다는 사실조차
    화면에 남지 않아 사람이 판단할 기회가 없다. `cap` 은 호출 규약 유지를 위해
    남겨 두고 `scout.price_cap: true` 로 예전 동작을 되살릴 수 있다.
    """
    price = c.live_price
    if price <= 0:
        return False, "가격 미확인"
    if not c.price_fresh:
        return False, "현재가 미확인 — 낡은 가격으로 매매 승격하지 않는다"
    if cap > 0 and price > cap and bool(cfg().get("price_cap", False)):
        return False, f"1주 {price:,.0f}원 > 한도 {cap:,.0f}원"
    return True, ""


def plan(cands: list[Candidate], cur: Current, now: datetime | None = None,
         conf: dict | None = None) -> list[dict]:
    """후보 + 현재 상태 → 결정 목록. **순수 함수** — 아무것도 쓰지 않는다.

    shadow 모드는 이 결과를 기록만 하고 적용하지 않는다. 그래서 "엔진이라면
    이렇게 했을 것" 을 실제 감시목록과 나란히 보여줄 수 있다.
    """
    now = now or datetime.now(UTC)
    conf = conf or cfg()
    cap = settings.tradable_price_cap(
        settings.CONFIG.get("scanner", {}).get("trade_max_price", 30_000))
    dwell = timedelta(minutes=float(conf["min_dwell_min"]))
    by_code = {c.code: c for c in cands}
    out: list[dict] = []

    def held_long_enough(code: str) -> bool:
        t = cur.since.get(code)
        return t is None or (now - t) >= dwell

    # --- 승격 ---
    trade_now = sum(1 for t in cur.tier.values() if t == TRADE)
    total_now = sum(1 for t in cur.tier.values() if t != NONE)
    for c in cands:
        tier = cur.tier.get(c.code, NONE)
        if tier == NONE:
            if c.score < conf["promote_collect"]:
                continue
            if total_now >= conf["max_total"]:
                continue                       # 상한 — 이번 사이클엔 못 들어온다
            total_now += 1
            out.append(_dec(c, "promote_collect", tier, COLLECT,
                            f"점수 {c.score:.2f} · 소스 {c.group_count}종"))
            continue
        if tier == COLLECT:
            # 매매 승격은 **게이트로만** 판정한다 — 점수는 보지 않는다
            if not held_long_enough(c.code):
                continue                       # probation — 최소 1세션 체류
            ok, why = _tradable(c, cap)
            if not ok:
                continue                       # 조용히 수집전용 유지
            if trade_now >= conf["max_trade"]:
                continue
            trade_now += 1
            out.append(_dec(c, "promote_trade", tier, TRADE,
                            f"체결가능 {c.live_price:,.0f}원 · 수집 체류 완료"))

    # --- 강등 ---
    # **장 마감 후에는 강등하지 않는다.** 장중 소스는 시장이 닫히면 보고할 수가
    # 없다. 그때의 '신호 없음' 은 "더는 후보가 아니다" 가 아니라 "확인할 수
    # 없다" 다 — TTL 을 넣은 이유와 같은 구분이다. 이걸 빼먹으면 매일 저녁
    # 거래대금·등락률 편입분이 전멸하고 다음 날 아침 다시 들어온다. 없애려던
    # 바로 그 회전이다.
    #
    # 2026-07-27 22:01 shadow 실측: 감시목록 72종목 중 22종목에 대해 강등
    # 결정이 나왔다(전부 장중 소스 편입분). 축소 폭 상한이 10건에서 끊었을 뿐이다.
    dropped: set[str] = set()
    if not in_session(now):
        return out
    for code, tier in cur.tier.items():
        if tier == NONE or code in cur.protected:
            continue
        c = by_code.get(code)
        score = c.score if c else 0.0
        if score >= conf["demote_below"]:
            continue                           # 히스테리시스 — 승격선보다 낮다
        if not held_long_enough(code):
            continue
        dropped.add(code)
        out.append(_drop(code, c, cur, score,
                         f"점수 {score:.2f} < 강등선 {conf['demote_below']}"))

    # --- 강등: 상한 초과 ---
    # 상한을 런타임에 낮추면(레이트리밋 재계산 등) 이미 초과 상태일 수 있다.
    # 그때는 **점수 하위부터** 뺀다. 점수의 유일한 매매 관련 용도가 이것이다 —
    # 누구를 올릴지가 아니라 자리가 모자랄 때 누구를 뺄지.
    live = [code for code, t in cur.tier.items()
            if t != NONE and code not in dropped and code not in cur.protected]
    live.sort(key=lambda code: (by_code[code].score if code in by_code else 0.0, code))
    over = len(live) + len(cur.protected) - int(conf["max_total"])
    for code in live[:max(0, over)]:
        dropped.add(code)
        c = by_code.get(code)
        out.append(_drop(code, c, cur, c.score if c else 0.0,
                         f"감시목록 상한 {conf['max_total']} 초과 — 점수 하위"))
    # 매매 tier 상한 초과분은 수집전용으로 내린다(감시는 유지)
    in_trade = [code for code in live
                if cur.tier.get(code) == TRADE and code not in dropped]
    in_trade.sort(key=lambda code: (by_code[code].score if code in by_code else 0.0,
                                    code))
    over_t = len(in_trade) - int(conf["max_trade"])
    for code in in_trade[:max(0, over_t)]:
        c = by_code.get(code)
        out.append({"code": code, "name": (c.name if c else None)
                    or cur.names.get(code, code),
                    "action": "demote", "from_tier": TRADE, "to_tier": COLLECT,
                    "score": c.score if c else 0.0,
                    "sources": c.sources if c else [],
                    "reason": f"매매 tier 상한 {conf['max_trade']} 초과 — 점수 하위"})
    return out


def _drop(code: str, c: Candidate | None, cur: Current, score: float,
          reason: str) -> dict:
    return {"code": code, "name": (c.name if c else None) or cur.names.get(code, code),
            "action": "drop", "from_tier": cur.tier.get(code, NONE), "to_tier": NONE,
            "score": score, "sources": list(c.sources) if c else [], "reason": reason}


def _dec(c: Candidate, action: str, frm: str, to: str, reason: str) -> dict:
    """결정 한 건. 실측 시세가 있으면 **기록만** 하고 판단에는 쓰지 않는다.

    등락률·체결강도를 여기 싣는 이유는 4주 뒤 측정 때문이다. 지금 승격 조건에
    넣으면 발굴 3규칙 점수가 저지른 일을 반복하는 것이다 — 그것도 '그럴듯해서'
    넣었고 1년 뒤 알파가 아니라 베타 프록시로 판명됐다. 체결강도는 어느 소스도
    쓰지 않는 새 정보라, 원장에 쌓여야 비로소 질문을 던질 수 있다.
    """
    d = {"code": c.code, "name": c.name, "action": action, "from_tier": frm,
         "to_tier": to, "score": c.score, "sources": list(c.sources),
         "reason": reason}
    if c.quote:
        d["change_pct"] = c.quote.get("change_pct")
        d["cntr_str"] = c.quote.get("cntr_str")
        # 호가 스프레드도 같은 규약 — 싣기만 하고 판단에 쓰지 않는다.
        # 유동성을 거래대금으로만 보는 지금 게이트가 못 보는 축이다.
        d["spread_pct"] = c.quote.get("spread_pct")
    return d
