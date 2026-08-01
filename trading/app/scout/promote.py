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
    감시가 최대 15분 묵은 가격으로 돈다(WS 구독·백필 대상에서 빠진다 — 실거래 손실로
    이어졌던 결함이다).
  - seed/manual — 사용자 의도를 엔진이 덮어쓰지 않는다.
"""
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .. import settings
from .model import DECISION_QUOTE_FIELDS
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


def session_open_before(now: datetime, sessions: int) -> datetime:
    """`sessions`번째 직전 **완료된** 세션의 개장 시각(KST). probation 기준선.

    '완료' 가 핵심이다(감사 2026-08-01 M1·M2). 진행 중인 오늘 세션을 세면
    기준선이 09:00 정각에 오늘로 점프해 — 08:59 편입이 09:00:00 에 통과하고,
    주말·금요일 저녁 유입분(야간 배치 픽 최대 45건)이 **월요일 개장 첫
    사이클에 전부 통과**한다. probation 의 목적("한 세션 관측된 종목만
    매매로")이 정확히 그 지점에서 무너진다.

    그래서 오늘은 마감(15:30) 후에만 센다. probation_sessions=1 의 실효:
    주말·야간 유입분은 월요일 하루를 수집 tier 로 관측된 뒤 화요일에 승격
    가능해진다. 휴장일 캘린더는 없다 — 공휴일이 끼면 길어지는(보수적인)
    쪽으로 틀린다.
    """
    t = now.astimezone(KST)
    opens = 0
    day = t.date()
    if t.weekday() >= 5 or t.strftime("%H:%M") < SESSION[1]:
        day -= timedelta(days=1)     # 오늘 세션은 아직 완료가 아니다
    while True:
        if day.weekday() < 5:
            opens += 1
            if opens >= sessions:
                break
        day -= timedelta(days=1)
    return datetime.combine(day, datetime.min.time(),
                            tzinfo=KST).replace(hour=9)

# tier 이름 — decisions 에 그대로 들어간다
NONE, COLLECT, TRADE = "none", "collect", "trade"

DEFAULTS = {
    "promote_collect": 0.35,     # 이상 → 수집전용 후보
    "demote_below": 0.25,        # 미만 → 감시목록에서 제거 (승격 임계보다 낮다)
    "min_dwell_min": 15,         # 최소 체류 — 들락날락·WS 재구독 폭주 방지
    # 매매 승격 전 수집 tier 체류 **세션** 수. 15분 dwell 만으로는 신규 종목이
    # 편입 15분 뒤 매매 tier 까지 갈 수 있었다 — 설계 검토 7번("probation 은
    # '감시했지만 매매하지 않은 표본' 을 만든다")이 주석으로만 있었고 코드가
    # 없었다(감사 2026-08-01 확인). 1 = 오늘 개장 전부터 감시하던 종목만 승격.
    # 0 이면 종전 동작(15분 dwell 만).
    "probation_sessions": 1,
    "max_trade": 40,             # 매매 tier 상한 — config 와 동일값(드리프트 금지)
    "max_total": 120,            # 감시목록 전체 상한
    # --- 기회비용 회전 ---
    # 자리를 받고 이만큼 지나도록 신호가 한 건도 없으면 대기 후보에게 넘긴다.
    # **성과가 아니라 신호 유무로 판정한다** — 종목의 과거 손익은 다음 거래를
    # 예측하지 않는다(실측 2026-07-31: 상관 −0.050, n=57, t=−0.37. 손실 이력
    # 종목의 다음 거래 −0.611% vs 이익 이력 −0.607%, 차이 0.004%p).
    # 반면 "이 종목에 우리 규칙이 걸리지 않는다" 는 자리를 비울 근거가 된다 —
    # 자리의 목적이 신호 탐지이기 때문이다.
    "silent_dwell_min": 390,     # 1거래일. 0 이면 회전 끔
    "silent_cooldown_min": 390,  # 밀려난 뒤 재승격 금지 기간
    "max_rotate": 3,             # 사이클당 회전 상한 — MAX_SHRINK 와 같은 서킷브레이커
    # 상한 초과 강등의 사이클당 상한 — max_trade 급락·held 오산으로 매매 tier 를
    # 한 사이클에 통째로 비우지 못하게 한다(진단 2026-08-01 M6)
    "max_demote": 10,
}


def cfg() -> dict:
    return DEFAULTS | (settings.CONFIG.get("scout", {}) or {})


@dataclass
class Current:
    """지금 감시목록의 상태 — 투영기의 입력."""
    tier: dict[str, str]              # code → none|collect|trade
    since: dict[str, datetime]        # code → 현재 tier 진입 시각
    # 감시목록에서 **빼지 않는다**(drop 금지). 보유 + seed/manual.
    protected: frozenset
    # 그중 **매매 tier 에서도 내리지 않는** 것 — 보유 종목만.
    #
    # 둘을 나눈 이유(사용자 결정 2026-07-31): 수동 편입은 단순 관심 종목일 수
    # 있으므로 매매 자리를 예약하지 않는다. 감시목록에는 남아 정보가 계속
    # 붙지만, 상한이 모자라면 수집전용으로 내려간다.
    #
    # 보유는 다르다 — 매매 tier 에서 빠지면 장중 분봉 백필 대상에서 제외되고
    # (`_collect_targets`), 청산 감시의 가격 폴백이 낡는다.
    held: frozenset = frozenset()
    names: dict[str, str] = field(default_factory=dict)
    # --- 기회비용 회전 (2026-07-31) ---
    # 자리를 받은 시각 / 마지막 신호 시각 / 마지막으로 밀려난 시각.
    # 감시목록에는 tier 변경 시각이 없어(`set_mode` 가 `added` 를 안 건드린다)
    # `decisions`·`signal_log` 원장에서 읽어 온다.
    trade_since: dict[str, datetime] = field(default_factory=dict)
    last_signal: dict[str, datetime] = field(default_factory=dict)
    demoted_at: dict[str, datetime] = field(default_factory=dict)
    # 회전 재료(위 세 dict)를 실제로 읽어 왔는가. False 면 회전을 멈춘다 —
    # 빈 dict 를 '전부 침묵' 으로 읽는 사고를 막는다(감사 2026-08-01 H5).
    rotation_ready: bool = True


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

    prob = int(conf.get("probation_sessions", 0) or 0)
    prob_line = session_open_before(now, prob) if prob > 0 else None

    def probation_done(code: str) -> bool:
        """수집 tier 를 `probation_sessions`세션 거쳤는가 — 매매 승격의 게이트.

        기준은 감시목록 편입 시각(`since`)이 기준선(가장 최근 개장 등)보다
        이전인가다. 부수 효과가 목적의 절반이다: probation 을 거치는 종목은
        '감시했지만 매매하지 않은 표본' 이 되어 사후 측정의 대조군을 만든다.
        시각을 모르는 항목(엔진 이전 편입)은 통과 — 이미 오래 있었다.
        """
        if prob_line is None:
            return True
        t = cur.since.get(code)
        return t is None or t <= prob_line

    # 동점 축출 tie-break — 일자 시드 셔플. NIGHTLY_STRENGTH 단일화로 야간
    # 팔 45건이 전부 동점이 됐는데, 종전 tie-break(코드 오름차순)면 상한 축출이
    # **종목코드 순으로 결정론**이 되어 무작위 대조군 표본의 생존 여부가
    # 무작위가 아니게 된다(감사 2026-08-01 M5). 일자 시드라 같은 날 안에서는
    # 안정적(진동 없음)이고 날이 바뀌면 순서가 섞인다.
    _day = now.astimezone(KST).date().isoformat()

    def shuffle_key(code: str) -> str:
        return hashlib.md5(f"{_day}:{code}".encode()).hexdigest()

    # --- 기회비용 회전 ---
    silent_min = float(conf.get("silent_dwell_min", 0) or 0)
    cool = timedelta(minutes=float(conf.get("silent_cooldown_min", 0) or 0))

    def silent(code: str) -> bool:
        """자리를 받은 뒤 `silent_dwell_min` 이 지나도록 신호가 없었는가.

        기준점은 **자리를 받은 시각**이다(`trade_since`). 감시목록 편입 시각을
        쓰면 어제 편입돼 오늘 자리를 받은 종목이 즉시 '오래 앉았다' 가 된다.
        원장에 없으면 편입 시각으로 폴백한다 — 엔진 이전에 들어온 종목이다.
        """
        if silent_min <= 0 or not cur.rotation_ready:
            return False                   # 재료가 없으면 침묵 판정 자체를 안 한다
        got = cur.trade_since.get(code) or cur.since.get(code)
        if got is None or (now - got) < timedelta(minutes=silent_min):
            return False                   # 아직 기회를 다 쓰지 않았다
        last = cur.last_signal.get(code)
        return last is None or last < got

    def cooling(code: str) -> bool:
        """밀려난 지 얼마 안 됐는가. 없으면 다음 사이클에 곧장 복귀해 무의미하다."""
        t = cur.demoted_at.get(code)
        return bool(cool) and t is not None and (now - t) < cool

    # --- 승격 ---
    promoted_now: set[str] = set()     # 이번 사이클 승격분 — 회전 계산이 쓴다
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
                continue                       # 최소 체류(분) — 진동 방지
            if not probation_done(c.code):
                continue                       # probation — 개장 전부터 감시하던 것만
            if cooling(c.code):
                continue                       # 침묵으로 밀려난 직후 — 쿨다운
            ok, why = _tradable(c, cap)
            if not ok:
                continue                       # 조용히 수집전용 유지
            if trade_now >= conf["max_trade"]:
                continue
            trade_now += 1
            promoted_now.add(c.code)
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
    live.sort(key=lambda code: (by_code[code].score if code in by_code else 0.0,
                                shuffle_key(code)))
    # 보호 종목은 **감시목록에 있는 것만** 센다. `protected` 에는 보유 종목이
    # 통째로 들어오는데(ledger.open_symbols) 감시목록 밖 보유(수동 매수 등)가
    # 섞이면 합계가 실제 감시목록보다 커져 비보호 종목을 과잉 drop 한다.
    prot_in_watch = sum(1 for c in cur.protected if cur.tier.get(c, NONE) != NONE)
    over = len(live) + prot_in_watch - int(conf["max_total"])
    for code in live[:max(0, over)]:
        dropped.add(code)
        c = by_code.get(code)
        out.append(_drop(code, c, cur, c.score if c else 0.0,
                         f"감시목록 상한 {conf['max_total']} 초과 — 점수 하위"))
    # 매매 tier 상한 초과분은 수집전용으로 내린다(감시는 유지)
    #
    # **보호 종목도 상한을 차지한다.** `live` 는 보호를 빼고 만들어졌으므로
    # 그대로 세면 상한 계산에서 보호분이 통째로 사라진다 — 바로 위 `max_total`
    # 은 `len(live) + len(cur.protected)` 로 더해 주는데 여기만 빠져 있었다.
    #
    # 그 비대칭이 교착을 만들었다(실측 2026-07-31):
    #   매매 tier 28 · 보호 22 · 비보호 6 · max_trade 25
    #   승격 판정: trade_now(28) >= 25  → 차단        ← 전체를 센다
    #   강등 판정: over_t = 6 - 25 = -19 → 0건        ← 비보호만 센다
    # 들어올 수도 나갈 수도 없어 `promote_trade` 결정이 7/29 이후 0건이었고,
    # 같은 종목만 며칠씩 반복 거래됐다(5일 106거래 / 고유 50종목).
    #
    # 보호 종목은 강등할 수 없으므로, 그것이 상한을 채우면 비보호가 0까지
    # 밀려난다. 그게 정직한 결말이다 — 상한을 넘긴 채 잠기는 것보다 낫다.
    # **수동/seed 는 매매 자리를 예약하지 않는다**(사용자 결정 2026-07-31).
    # `live` 대신 tier 를 직접 훑되 `held`(보유)만 뺀다 — 보유는 매매 tier 에서
    # 내리면 분봉 백필 대상에서 빠져 청산 감시의 가격 폴백이 낡는다.
    # 수동은 감시목록에는 남으므로(drop 보호는 유지) 정보 수집이 끊기지 않는다.
    in_trade = [code for code, t in cur.tier.items()
                if t == TRADE and code not in dropped and code not in cur.held]
    # **침묵한 자리를 먼저 뺀다.** 종전 정렬은 점수 하위부터였는데, 점수는
    # "여전히 눈에 띄는가" 이지 "우리에게 값어치가 있는가" 가 아니다 — 거래대금
    # 상위에 계속 있는 종목은 계속 눈에 띈다. 자리의 목적은 신호 탐지다.
    in_trade.sort(key=lambda code: (not silent(code),
                                    by_code[code].score if code in by_code else 0.0,
                                    shuffle_key(code)))
    held_trade = sum(1 for code, t in cur.tier.items()
                     if t == TRADE and code in cur.held)
    over_t = len(in_trade) + held_trade - int(conf["max_trade"])
    demoted: set[str] = set()
    # 사이클당 상한 — MAX_SHRINK(drop)와 같은 서킷브레이커가 여기엔 없었다
    # (감사 2026-08-01 M6). max_trade 를 런타임에 확 낮추거나 held 계산이
    # 튀어도 한 사이클에 매매 tier 를 통째로 비우지 못하게 한다.
    over_t = min(over_t, int(conf.get("max_demote", 10)))
    for code in in_trade[:max(0, over_t)]:
        demoted.add(code)
        c = by_code.get(code)
        out.append(_demote(code, c, cur,
                           f"매매 tier 상한 {conf['max_trade']} 초과 — "
                           + ("침묵" if silent(code) else "점수 하위")))

    # --- 회전: 자리는 찼는데 침묵한 종목이 있고, 대기 후보가 있을 때 ---
    #
    # **경쟁이 있을 때만 돈다.** 자리가 남는데 내리면 순손실이다. 대기 후보가
    # 없어도 마찬가지 — 비워 봐야 채울 것이 없다.
    #
    # 실측 2026-07-31 근거: 매매 tier 40종목 중 16종목(40%)이 그날 신호 0건이었고,
    # 매매 tier **밖에서 나온 신호는 0건**이었다. 자리가 모자라 놓친 기회는 없고,
    # 앉아 있는 종목이 신호를 안 낸다. 발굴 소스는 5일간 고유 1,000종목 이상을
    # 지목했는데 실제로 거래된 것은 50종목이다.
    rotate = int(conf.get("max_rotate", 0) or 0)
    if rotate > 0 and over_t <= 0:
        # 대기자는 **승격의 네 게이트를 전부 통과한 종목**만 센다. probation 이
        # 빠져 있던 종전에는 "채울 수 없는 자리를 비우는" 회전이 가능했다 —
        # 오늘 장중 편입분이 대기자를 부풀리고, 밀려난 종목은 쿨다운 390분에
        # 잠기는데 그 자리는 오늘 중 채워질 수 없었다(감사 2026-08-01 H1).
        # 이번 사이클 승격분(promoted_now)도 뺀다 — cur.tier 스냅샷에는 아직
        # COLLECT 라 대기자이면서 동시에 빈자리로 이중 계상됐다(H2).
        waiting = [c.code for c in cands
                   if cur.tier.get(c.code) == COLLECT
                   and c.code not in dropped
                   and c.code not in promoted_now
                   and not cooling(c.code)
                   and held_long_enough(c.code)
                   and probation_done(c.code)
                   and _tradable(c, cap)[0]]
        room = int(conf["max_trade"]) - (len(in_trade) + held_trade
                                         + len(promoted_now))
        need = min(len(waiting) - max(0, room), rotate)
        if need > 0:
            for code in [x for x in in_trade if silent(x) and x not in demoted][:need]:
                c = by_code.get(code)
                out.append(_demote(code, c, cur,
                                   f"침묵 {int(silent_min)}분 — 대기 후보에 자리 양보"))
    return out


def _demote(code: str, c: Candidate | None, cur: Current, reason: str) -> dict:
    return {"code": code, "name": (c.name if c else None) or cur.names.get(code, code),
            "action": "demote", "from_tier": TRADE, "to_tier": COLLECT,
            "score": c.score if c else 0.0,
            "sources": c.sources if c else [], "reason": reason}


def _drop(code: str, c: Candidate | None, cur: Current, score: float,
          reason: str) -> dict:
    return {"code": code, "name": (c.name if c else None) or cur.names.get(code, code),
            "action": "drop", "from_tier": cur.tier.get(code, NONE), "to_tier": NONE,
            "score": score, "sources": list(c.sources) if c else [], "reason": reason}


def _dec(c: Candidate, action: str, frm: str, to: str, reason: str) -> dict:
    """결정 한 건. 실측 시세가 있으면 **기록만** 하고 판단에는 쓰지 않는다.

    등락률·체결강도·스프레드·호가잔량·당일 위치를 여기 싣는 이유는 4주 뒤 측정
    때문이다. 지금 승격 조건에 넣으면 발굴 3규칙 점수가 저지른 일을 반복하는
    것이다 — 그것도 '그럴듯해서' 넣었고 1년 뒤 알파가 아니라 베타 프록시로
    판명됐다. 원장에 쌓여야 비로소 질문을 던질 수 있다.

    **`quote_obs` 가 있는데도 여기 또 싣는 이유**는 시점이다. 그쪽은 종목·날짜당
    한 행으로 접히므로 남는 것은 그날 마지막 값인데, 물어야 할 것은 "이 종목을
    올린 **그 순간** 어땠나" 다. 이번 실측이 정확히 그 순간의 값을 가리켰다 —
    진입가가 직전 30분 범위의 0.67 지점이었고 위치가 높을수록 이후 상승 여력이
    단조로 줄었다(하단 1.52% → 상단 1.02%). 접힌 평균으로는 그 질문을 못 한다.

    실을 필드는 `model.DECISION_QUOTE_FIELDS` 가 유일한 목록이다. 여기서 손으로
    나열하면 스키마와 갈리고, 갈린 컬럼은 NULL 로만 조용히 드러난다.
    """
    d = {"code": c.code, "name": c.name, "action": action, "from_tier": frm,
         "to_tier": to, "score": c.score, "sources": list(c.sources),
         "reason": reason}
    if c.quote:
        # 없는 키는 담지 않는다 — `extra_fields` 가 못 잰 값의 키를 아예 빼므로
        # 여기서 0 으로 채우면 '측정했는데 0' 과 '못 쟀다' 가 섞인다.
        for f in DECISION_QUOTE_FIELDS:
            if f in c.quote:
                d[f] = c.quote[f]
    return d
