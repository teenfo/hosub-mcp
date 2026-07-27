"""어댑터 규약 — 각 발굴 기능이 신호를 **에스컬레이션**하는 방식.

기존 파싱·필터 함수(`scanner.parse_rank` / `filter_candidates` / … )는 그대로
재사용한다. 바뀌는 것은 "누가 감시목록에 쓰는가" 뿐이다 — 어댑터는 **아무것도
쓰지 않고** 신호만 낸다.

## 어댑터가 감시목록을 보면 안 되는 이유

기존 필터는 `code not in settings.WATCHLIST` 로 이미 감시 중인 종목을 후보에서
뺀다. 화면 표시용으로는 맞지만 투영 모델에서는 **발진**을 일으킨다.

    승격 → 감시목록 진입 → 소스가 보고 중단 → 신호 TTL 만료 → 점수 0
    → 강등 → 다시 보고 → 승격 → …

히스테리시스로 막을 수 없다 — 히스테리시스는 신호가 *진동할 때* 유효하지
신호가 *사라질 때* 는 무력하다. 그래서 어댑터는 필터의 `keep` 인자에 감시목록
전체를 넘겨 그 제외를 무력화한다. **소스는 무조건 보고하고, 중복 제거는
투영기가 한다.**
"""
from typing import Protocol, runtime_checkable

from .. import model
from ..model import Signal


@runtime_checkable
class Source(Protocol):
    name: str

    def enabled(self) -> bool:
        """config 스위치. 꺼져 있으면 루프가 건너뛴다."""

    def interval_sec(self) -> int:
        """수집 주기(초)."""

    async def collect(self) -> list[Signal]:
        """신호를 낸다. 예외는 호출자(engine)가 격리·백오프한다."""


def watched_keep() -> frozenset:
    """감시목록 전체 — 기존 필터의 '이미 감시 중이면 제외' 를 무력화하는 값.

    `filter_*` 의 조건이 `code not in WATCHLIST or code in keep` 이므로,
    keep 에 감시목록을 통째로 넘기면 제외가 사라진다. 필터 자체를 고치지 않고
    같은 함수를 두 용도(화면·엔진)로 쓰기 위한 방법이다.
    """
    from ... import settings

    return frozenset(settings.WATCHLIST)


def rank_signals(items: list[dict], source: str, kind: str = "") -> list[Signal]:
    """순위 기반 소스(거래대금·등락률) 공통 변환.

    strength 는 **목록 안에서의 순위**다. 목록 자체가 이미 필터를 통과한
    상위권이므로, 20위 종목의 0.0 은 "나쁘다"가 아니라 "이 목록의 끝"이다.
    """
    total = len(items)
    return [
        Signal(
            code=it["code"], name=it.get("name") or it["code"], source=source,
            kind=kind or source,
            strength=model.rank_strength(i + 1, total),
            raw=float(it.get("change_pct", 0.0)),
            price=float(it.get("price", 0.0)),
            evidence={"rank": i + 1, "of": total,
                      "change_pct": it.get("change_pct"),
                      "trade_value": it.get("trade_value")},
        )
        for i, it in enumerate(items) if it.get("code")
    ]
