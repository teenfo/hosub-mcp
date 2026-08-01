"""신호 모델 — 여섯 갈래 발굴 소스가 같은 언어로 말하게 하는 규약.

지금 각 소스는 서로 다른 단위로 말한다. 거래대금 상위는 순위(1~N), 급등 조짐은
급증률(%), 야간 발굴은 3규칙 점수(0~3), 뉴스는 0~100 점수다. 이 상태로는
"거래대금 상위 + 급등 조짐 + 뉴스 호재가 겹친 종목이 단일 소스 종목보다 나은가"
를 물을 수조차 없다. `strength` 0.0~1.0 정규화가 그 질문을 가능하게 한다.

## strength 는 예측력이 아니다

**중요**: `strength` 는 "이 종목이 오를 확률" 이 아니라 **"이 소스 안에서 얼마나
강하게 지목됐는가"** 다. 2026-07-27 실측에서 발굴 점수 순 상위 N 은 매수가능
무작위보다 0.22R 나빴다 — 소스가 가리키는 순서에 예측력이 있다는 증거가 없다.

그래서 이 값의 용도는 셋뿐이다.
  1. 여러 소스가 같은 종목을 가리켰는지 세는 것(취합)
  2. 상한 초과 시 **누구를 먼저 뺄지**
  3. 화면에 근거를 보이는 것

**매매 tier 승격에는 쓰지 않는다.** 승격은 게이트(유동성·체결가능성·probation)로만
결정한다. 점수로 매매를 여는 것은 근거가 없다.

## TTL 이 필요한 이유

지금은 API 1회 실패 → 필터가 빈 리스트 → `replace_*` 가 tier 전체를 지운다.
신호에 유효기간을 주면 **한 번의 조회 실패가 목록을 지우지 못한다.**
"신호가 없다" 와 "아직 확인 못 했다" 는 다른 상태다.
"""
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime

# 소스 이름 — DB 에 그대로 들어가므로 바꾸면 이력이 끊긴다
VOLUME = "volume"        # 거래대금 상위
GAINERS = "gainers"      # 등락률 상위
PRESURGE = "presurge"    # 거래량 급증 (지금은 편입 경로가 없어 측정된 적이 없다)
NIGHTLY = "nightly"      # 야간 전종목 발굴
NEWS = "news"            # 뉴스·공시 (TNM)
MANUAL = "manual"        # 사용자 수동 지정
SOURCES = (VOLUME, GAINERS, PRESURGE, NIGHTLY, NEWS, MANUAL)

# 소스 그룹 — **같은 정보를 세 번 세지 않기 위한** 구분이다.
# active·gainer·presurge 는 같은 팩터(오늘의 가격·거래량 모멘텀)의 세 가지 뷰다.
# 실제로 filter_candidates 와 filter_gainers 는 둘 다 change_pct 로 정렬한다.
# 단순 합산하면 이벤트 스터디가 잡아낸 바로 그 베타 노출을 3배로 증폭한다.
# 점수는 '그룹 내 max, 그룹 간 합' 으로 낸다(scoring.py).
GROUP = {
    VOLUME: "intraday", GAINERS: "intraday", PRESURGE: "intraday",
    NIGHTLY: "daily", NEWS: "news", MANUAL: "human",
}
GROUPS = ("intraday", "daily", "news", "human")

# 소스별 유효기간(초)과 반감기(초).
# TTL 은 스캔 주기의 2~3배 — 한 번의 조회 실패가 목록을 지우지 못하게.
# 반감기는 정보의 수명 — 장중 순위는 빨리 늙고, 공시는 천천히 늙는다.
TTL_SEC = {
    VOLUME: 180, GAINERS: 180, PRESURGE: 180,
    NIGHTLY: 86_400, NEWS: 43_200, MANUAL: 0,          # 0 = 만료 없음
}
# 반감기는 **TTL 이하**여야 한다. 반감기가 TTL 보다 길면 신호가 만료되기 전까지
# 감쇠가 거의 일어나지 않아 있으나 마나 한 값이 된다(장중 소스 600초 반감기 +
# 180초 TTL 이면 최대 감쇠가 0.81 에 그쳤다 — test_scout_model.py 가 잡았다).
# 장중 소스는 TTL 과 같게 둔다: 폴 1회 실패 → 0.79, 2회 → 0.63, 3회 → 만료.
# 목록이 깜빡 꺼지는 대신 서서히 흐려진다.
HALF_LIFE_SEC = {
    VOLUME: 180, GAINERS: 180, PRESURGE: 180,
    NIGHTLY: 86_400, NEWS: 21_600, MANUAL: 0,          # 0 = 감쇠 없음
}


def _clamp(x: float) -> float:
    if not math.isfinite(x):
        return 0.0
    return max(0.0, min(1.0, x))


def rank_strength(rank: int, total: int) -> float:
    """순위 1~N → 1.0~0.0. 1위가 1.0, 꼴찌가 0에 가깝다."""
    if total <= 1:
        return 1.0 if rank <= 1 else 0.0
    return _clamp(1.0 - (rank - 1) / (total - 1))


# 이 값을 넘는 급증률은 '더 강한 신호' 가 아니라 **분모가 깨진 것**으로 본다.
# 급증률은 직전 기간 거래량 대비 비율인데, 개장 직후에는 그 직전 기간이 사실상
# 비어 있어 비율이 폭발한다. 실측 2026-07-28 09:00:25(개장 25초): 신일전자
# 13,605,700% · NAVER 718,757%(등락률 0.00%) · LG전자 8,738,190%. 대형주가
# 최대 세기 '급등 조짐' 을 받았다 — 거래량이 터진 게 아니라 분모가 0 이었다.
# 1,000배(100,000%)는 정상 장중 거래량 비율로 도달할 수 없는 값이라, 튜닝
# 파라미터가 아니라 **정합성 상한**이다. 근본 방어는 개장 워밍업(intraday.py).
SURGE_SANE_MAX = 100_000.0

# `sdnin_rt` 의 실제 단위 — 실측 2026-07-30 13:30 ka10023 원시 응답:
#
#   prev_trde_qty 174,076 → now_trde_qty 184,076 · sdnin_qty +10,000 · sdnin_rt +5.74
#   10,000 / 174,076 = 5.744%
#
# 즉 **직전 조회 시점의 누적 거래량 대비 증가율(%)** 이다. 분모가 그날 누적이라
# 장중에 300% 는 도달할 수 없다 — 1분 사이에 누적 거래량이 4배가 될 수 없다.
# 그래서 이 값의 현실적 범위는 0~10 이고, 상위권이 5~6 이다.
#
# **분모가 누적이라 시간대 편향이 있다.** 같은 1분 거래량이라도 09시에는 누적이
# 작아 비율이 크고, 15시에는 작다(평균 1분이 그날의 1/390 ≈ 0.26%). 절대 문턱을
# 높게 두면 오후에 아무것도 안 걸린다 — 문턱을 낮게 두고 순위(top_n)로 고르는
# 이유다. 거래대금·등락률 소스가 순위 기반인 것과 같은 규약이 된다.
SURGE_FULL = 10.0        # 이 값 이상이면 만점. 상위권 실측 5~6 의 약 2배


def surge_strength(surge_pct: float) -> float:
    """거래량 급증률 % → 0~1. 로그 스케일 — 1.5%와 2%의 차이보다
    1.5%와 6%의 차이가 크다.

    **종전에는 `surge_pct <= 100` 을 0 으로 잘라 전부 0 이었다.** 100% 문턱은
    이 필드가 '직전 대비 몇 배' 라는 오해에서 나온 값이다(위 실측 참조).
    그 오해 때문에 이 소스는 7/28 개장 워밍업 가드가 들어간 뒤로 **신호를 하나도
    내지 못했다** — 그전에 나온 3,144건은 전부 개장 직후 분모 0 인 쓰레기값이었다.

    상한(`SURGE_SANE_MAX`)을 넘는 값은 여전히 **0** 이다. 1.0 이 아니다 — 깨진
    입력을 최대 세기로 읽는 것이 이 함수가 실제로 저지른 잘못이었다.
    """
    if not math.isfinite(surge_pct) or surge_pct <= 1.0:
        return 0.0
    if surge_pct > SURGE_SANE_MAX:
        return 0.0
    return _clamp(math.log10(surge_pct) / math.log10(SURGE_FULL))


def score_strength(score: float, max_score: float = 3.0) -> float:
    """발굴 3규칙 점수 0~3 → 0~1."""
    if max_score <= 0:
        return 0.0
    return _clamp(score / max_score)


# 결정 원장(`decisions`)에 **시점 고정**으로 싣는 실측 시세 필드.
#
# 목록을 여기 한 군데 두는 이유: 스키마(`store`)와 결정 생성(`promote`)이 서로
# 다른 목록을 들면 컬럼은 생기는데 값이 안 들어오고, 그 침묵은 4주 뒤 측정할 때
# `NULL` 로만 드러난다. 둘 다 model 을 임포트하므로 여기가 유일한 교차점이다.
#
# `quote_obs` 로는 부족해서 따로 싣는다. 그쪽은 **종목·날짜당 한 행으로 접히므로**
# 남는 것이 그날 마지막 값이다. 우리가 물어야 할 것은 "그 종목을 올린 **그 순간**
# 당일 위치가 얼마였나" 이고, 이번 실측이 가리킨 것이 정확히 그 순간의 값이다
# (진입가가 직전 30분 범위의 0.67 지점 · 위치가 높을수록 이후 상승 여력이 단조 감소).
#
# `exp_price`/`exp_qty` 는 넣지 않는다 — 예상체결은 동시호가 값인데 승격 판정은
# `in_session` 안에서만 일어나므로 사실상 늘 비어 있다. 그건 `quote_obs` 쪽에서
# 프리마켓 창을 따로 볼 때 쓴다. 늘 NULL 인 컬럼은 원장을 읽기 어렵게만 한다.
DECISION_QUOTE_FIELDS = (
    "change_pct", "cntr_str", "spread_pct",     # 기존 3종
    "day_pos", "day_range_pct", "from_open_pct",
    "req_imbalance", "tot_imbalance", "vol_vs_prev", "limit_pos",
)

# 야간 발굴 신호의 강도 — **팔에 상관없이 하나다.**
#
# 종전에는 점수 표본이 `score_strength`(2점 → 0.667 · 3점 → 1.0), 무작위 표본이
# 0.4 였다. 즉 자리가 모자랄 때 **점수 표본이 앞자리를 가져갔다.** 실측
# 2026-08-01 이 그 순서가 거꾸로임을 보였다 — 같은 날 변동성이 가장 가까운
# 종목을 대조군으로 두면 점수 표본은 -0.168R(t=-10.70) 이고, 점수가 높을수록
# 더 나쁘다(3점 만점 -0.238R). 3규칙 점수에는 **음의 정보**가 있다.
#
# 그렇다고 무작위를 위에 두지도 않는다. 무작위는 '이긴 쪽' 이 아니라 대조군이다.
# 둘 다 모르므로 **같은 값**이 정직하다. 값을 0.667(종전 2점짜리 수준)로 두는
# 이유는 소스 간 경쟁을 건드리지 않기 위해서다 — 그룹 점수는 '그룹 내 max' 라
# 여기를 낮추면 야간 발굴 전체가 뉴스·장중 소스에 자리를 내준다. 그 비교는
# 측정된 적이 없으므로 이번 변경으로 함께 흔들면 안 된다.
#
# 2026-07-31 실측 기준 감시목록은 상한(max_total 120)에 정확히 차 있다 —
# 이 순서는 화면 표시가 아니라 **누가 실제로 매매 대상이 되는가**를 정한다.
NIGHTLY_STRENGTH = 0.667

def news_strength(score: float) -> float:
    """TNM 뉴스 점수 0~100 → 0~1."""
    return _clamp(score / 100.0)


def decay(age_sec: float, half_life_sec: float) -> float:
    """반감기 감쇠. half_life 0 이면 감쇠 없음(수동 지정)."""
    if half_life_sec <= 0:
        return 1.0
    if age_sec <= 0:
        return 1.0
    return _clamp(0.5 ** (age_sec / half_life_sec))


@dataclass(frozen=True)
class Signal:
    """한 소스가 한 종목을 지목한 사건. **감시목록이 아니라 원장에 쌓인다.**

    감시목록 row 의 `source` 를 신뢰할 수 없다는 것이 이 분리의 이유다 —
    `replace_scanned` 가 이미 감시 중인 코드를 건너뛰므로, gainer 로 먼저 잡힌
    종목이 이후 active 에도 걸려도 source 는 영원히 `gainer` 로 남는다.
    소스별 기여도를 재려면 **모든 지목이 남아야** 한다.
    """
    code: str
    name: str
    source: str
    kind: str = ""                    # 소스 내 세부 종류 (예: "news:공급계약")
    strength: float = 0.0             # 0.0~1.0 정규화 — 소스 간 비교의 전제
    raw: float = 0.0                  # 원시값(순위·급증률·점수) — 화면 근거용
    price: float = 0.0                # 체결가능성 판정 재료(trade_tier_cap)
    evidence: dict = field(default_factory=dict)
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    ttl_sec: int = 0                  # 0 = 만료 없음

    def __post_init__(self):
        if self.source not in SOURCES:
            raise ValueError(f"알 수 없는 소스: {self.source}")
        object.__setattr__(self, "strength", _clamp(float(self.strength)))
        if not self.ttl_sec:
            object.__setattr__(self, "ttl_sec", TTL_SEC.get(self.source, 0))

    @property
    def group(self) -> str:
        return GROUP.get(self.source, "intraday")

    def age_sec(self, now: datetime | None = None) -> float:
        now = now or datetime.now(UTC)
        return max(0.0, (now - self.observed_at).total_seconds())

    def alive(self, now: datetime | None = None) -> bool:
        """TTL 이 지나지 않았는가. 0 이면 영구."""
        return self.ttl_sec <= 0 or self.age_sec(now) < self.ttl_sec

    def effective(self, now: datetime | None = None) -> float:
        """감쇠까지 반영한 현재 세기. 만료됐으면 0."""
        if not self.alive(now):
            return 0.0
        return self.strength * decay(self.age_sec(now),
                                     HALF_LIFE_SEC.get(self.source, 0))
