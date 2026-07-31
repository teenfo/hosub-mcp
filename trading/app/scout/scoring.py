"""신호 취합 — "서로 다른 정보원이 같은 종목을 가리켰는가".

## 그룹 내 max, 그룹 간 합

```
total(code) = Σ_groups  max_{s ∈ group} effective(s)
```

단순 합산이 아닌 이유가 이 파일의 핵심이다. `volume`·`gainers`·`presurge` 는
같은 팩터(오늘의 가격·거래량 모멘텀)의 **세 가지 뷰**다 — `filter_candidates`
와 `filter_gainers` 는 실제로 **둘 다 `change_pct` 로 정렬한다.** 더하면
이벤트 스터디가 잡아낸 바로 그 베타 노출을 3배로 증폭한다. 그룹 안에서는
가장 센 것 하나만 세고, 그룹이 다를 때만 더한다.

**서로 다른 정보원이 같은 종목을 가리킬 때만 점수가 오른다** — 이것이
"정보 취합" 의 정직한 구현이다.

## 가중치를 학습하지 않는다

원안은 소스별 가중치를 측정해 조정하려 했다. 그 근거로 쓰려던 잔차 IC 가
**실현 R 로 환산되지 않는다는 것이 실측됐다**(2026-07-27: 잔차 IC 로 학습한
합성 랭킹이 6개 방식 중 꼴찌였다). 그래서 가중치는 전 소스 1.0 고정이다.
측정 없이 손으로 정하는 것도, 무효로 판명된 값으로 학습하는 것도 하지 않는다.

점수의 용도는 **강등 순서와 화면 표시**뿐이다. 매매 tier 승격은 점수가 아니라
게이트로 결정한다(promote.py).
"""
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .model import GROUP, GROUPS, MANUAL, NEWS, NIGHTLY, Signal

# 소스 가중치 — **전부 1.0 고정**. 위 주석 참조.
# dict 로 둔 이유는 나중에 근거가 생겼을 때 바꿀 자리를 남겨 둔 것이고,
# 지금 값을 손대는 것은 실측을 무시하는 일이다.
WEIGHT = 1.0

# **점수에 넣지 않는 그룹.** 사용자 결정(2026-07-31):
# > 수동 입력은 단순 관심 종목일 수 있으므로 특별 가중치를 두지 않는다.
# > 다만 관련 정보를 계속 수집한다는 의미로 둔다.
#
# 종전 `human` 그룹(manual)은 세기 1.0 고정 · TTL 없음 · 감쇠 없음이라
# **영구 만점**이었다. 게다가 그룹을 혼자 쓰므로, 장중 3소스가 동시에 1위를
# 해도(그룹 내 max 로 1.0) 사람이 한 번 찍은 것과 점수가 같았다. 그 점수가
# 매매 tier 상한 초과 시 '누구를 뺄지' 정렬의 최상위로 가서 자리를 영구
# 점유했다 — 실측 2026-07-31: 매매 tier 28 중 22가 수동/seed 였다.
#
# 이제 수동은 **점수가 아니라 고정핀**이다. 감시목록에서 빠지지 않아 뉴스·공시·
# 리포트가 계속 붙지만(`Current.pinned`), 매매 자리를 예약하지는 않는다.
# 여전히 게이트(유동성·체결가능성·probation)를 통과하면 매매 tier 로 올라간다 —
# 점수가 아니라 게이트로 판정하므로 불리해지지 않는다.
#
# `by_group` 에는 그대로 남긴다. 화면이 "어느 소스가 지목했나" 를 보여줘야 한다.
UNSCORED = frozenset({"human"})


# 가격이 **낡은** 소스. 장중 소스는 스캔 응답의 현재가(60초 이내)를 싣지만
# 이 둘은 각각 발굴 배치일 종가와 뉴스 수집 시점 가격이다. 실측 2026-07-28:
# 펌텍코리아 승격 결정의 '체결가능 48,600원' 이 7/27 종가였고 당시 현재가는
# 47,050원(−3.19%)이었다. 체결가능성 게이트가 어제 값으로 판정한 것이다.
STALE_PRICE_SOURCES = frozenset({NIGHTLY, NEWS, MANUAL})


@dataclass
class Candidate:
    """한 종목에 대한 현재 취합 결과."""
    code: str
    name: str
    score: float = 0.0
    price: float = 0.0
    sources: list[str] = field(default_factory=list)
    by_group: dict[str, float] = field(default_factory=dict)
    signals: list[Signal] = field(default_factory=list)
    # 승격 직전에 실측한 현재 시세 {price, change_pct, cntr_str, volume}.
    # 매매 승격 후보에만 채운다 — 전 후보에 부르면 레이트리밋을 먹는다.
    quote: dict | None = None

    @property
    def group_count(self) -> int:
        """독립 정보원의 개수 — 취합의 실질이다. 점수보다 이쪽이 해석 가능하다."""
        return sum(1 for v in self.by_group.values() if v > 0)

    @property
    def price_fresh(self) -> bool:
        """게이트가 믿어도 되는 가격인가.

        실측 시세가 있으면 무조건 신선하다. 없으면 장중 소스가 하나라도
        기여했을 때만 — 그쪽은 스캔 응답의 현재가를 싣는다.
        """
        if self.quote:
            return True
        return any(s not in STALE_PRICE_SOURCES for s in self.sources)

    @property
    def live_price(self) -> float:
        """게이트가 쓸 가격. 실측이 있으면 그것이 이긴다."""
        return float(self.quote["price"]) if self.quote else self.price

    def evidence(self) -> list[dict]:
        """화면 근거 — 어느 소스가 무엇을 보고 올렸는지."""
        return [{"source": s.source, "kind": s.kind, "raw": s.raw,
                 "strength": round(s.strength, 3),
                 "observed_at": s.observed_at.isoformat(timespec="seconds"),
                 **s.evidence} for s in self.signals]


def aggregate(signals: list[Signal], now: datetime | None = None) -> list[Candidate]:
    """신호 목록 → 종목별 후보. 점수 내림차순, **동점은 종목코드 오름차순**.

    tie-break 를 못박는 이유: 현행 발굴은 `scored.sort(key=-score)` 만 하는데
    점수가 0~3 정수라 동점이 대량 발생하고, 상위 5 선택이 전종목 API 응답
    순서에 의존한다(discovery.py:205). 같은 편중을 물려받지 않는다.
    """
    now = now or datetime.now(UTC)
    by_code: dict[str, Candidate] = {}
    for s in signals:
        eff = s.effective(now)
        if eff <= 0:
            continue                       # 만료됐거나 세기 0 — 취합에 넣지 않는다
        c = by_code.get(s.code)
        if c is None:
            c = by_code[s.code] = Candidate(code=s.code, name=s.name)
        if s.name and not c.name:
            c.name = s.name
        if s.price > 0:
            c.price = max(c.price, s.price)
        c.signals.append(s)
        if s.source not in c.sources:
            c.sources.append(s.source)
        g = GROUP.get(s.source, "intraday")
        # 그룹 내 max — 같은 팩터를 두 번 세지 않는다
        c.by_group[g] = max(c.by_group.get(g, 0.0), WEIGHT * eff)
    for c in by_code.values():
        c.score = round(sum(v for g, v in c.by_group.items()
                            if g not in UNSCORED), 4)
        c.sources.sort()
        c.signals.sort(key=lambda s: (s.source, s.observed_at))
    return sorted(by_code.values(), key=lambda c: (-c.score, c.code))


def max_possible() -> float:
    """이론상 최대 점수 — 화면의 임계선을 비율로 그릴 때 쓴다.

    점수에 안 들어가는 그룹은 빼야 화면의 비율이 실제와 맞는다.
    """
    return float(len(set(GROUPS) - UNSCORED)) * WEIGHT
