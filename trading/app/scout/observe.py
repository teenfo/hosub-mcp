"""관측 전용 수집 루프 — 장중 투자자별 매매 · VI 발동종목.

## 왜 소스(`sources/`)가 아니라 여기인가

어댑터 프로토콜(`Source`)은 `collect() -> list[Signal]` 이다. 신호를 내면
점수 합산과 승격 판정에 들어간다. **이 둘은 신호를 내면 안 된다.**

지금 아는 것이 없기 때문이다. 기관 순매수가 좋은 신호인지 나쁜 신호인지
측정된 적이 없고, 5거래일 표본에서 코리아써키트는 +20.65% 인 날 기관 순매수가
−2,765 였다(그리고 우리는 −5,127 을 잃었다). 표본 하나로는 아무 말도 못 한다.
발굴 3규칙 점수도 '그럴듯해서' 들어갔고 1년 뒤 베타 프록시로 판명됐다 —
그 실수를 반복하지 않으려면 **판단 경로에 닿지 않는 자리**가 필요하다.

4주 뒤 잔차 IC 가 본페로니 문턱을 넘으면 그때 소스로 승격한다. 넘지 못하면
이 계열도 폐기한다. 결과를 본 뒤 문턱을 낮추지 않는다.

## 예산

두 TR 모두 1콜에 대량을 준다(투자자별 800종목 · VI 100종목). 기본 주기 120초
에서 **분당 1콜**이다 — 현재 분당 150~250콜 대비 무시할 수 있다.
"""
import asyncio
import logging
from datetime import UTC, datetime

from .. import settings
from ..kiwoom import observe as parse
from . import opening, promote, store

log = logging.getLogger(__name__)


def _cfg() -> dict:
    return settings.CONFIG.get("scout", {}).get("observe", {})


def enabled() -> bool:
    return bool(_cfg().get("enabled", True))


async def collect_once(now: datetime | None = None) -> dict:
    """1회 수집. 반환: {investor, vi} 적재 행 수.

    한쪽이 실패해도 다른 쪽은 쌓는다 — 관측이 서로를 막을 이유가 없다.
    """
    from ..kiwoom.client import client

    out = {"investor": 0, "vi": 0, "opening": 0}
    cfg = _cfg()
    invsr = str(cfg.get("invsr", "6"))
    if cfg.get("investor", True):
        try:
            rows = parse.parse_investor(await client.intraday_investor(invsr=invsr))
            out["investor"] = store.record_investor(rows, invsr, now)
        except Exception:  # noqa: BLE001 - 관측 실패가 매매를 막지 않는다
            log.warning("투자자별 매매 관측 실패", exc_info=True)
    if cfg.get("vi", True):
        try:
            out["vi"] = store.record_vi(parse.parse_vi(await client.vi_stocks()), now)
        except Exception:  # noqa: BLE001
            log.warning("VI 관측 실패", exc_info=True)
    # 개장 창은 하루 한 번만 쓰인다(`INSERT OR IGNORE`). 여기 얹는 이유는 이미
    # 장중에만 도는 루프라서다 — 별도 루프를 띄우면 같은 게이트를 두 번 쓴다.
    # 저장된 분봉만 읽으므로 API 호출이 없다.
    if opening.enabled():
        try:
            out["opening"] = 1 if opening.collect_once(now) else 0
        except Exception:  # noqa: BLE001
            log.warning("개장 창 관측 실패", exc_info=True)
    return out


async def loop() -> None:
    """장중에만 돈다. 마감 후에는 같은 스냅샷이 밤새 되풀이될 뿐이다
    (장중 소스가 `session_open` 을 갖게 된 것과 같은 이유 — PR #217)."""
    while True:
        try:
            if enabled() and settings.KIWOOM_APP_KEY and promote.in_session():
                got = await collect_once()
                if any(got.values()):
                    log.debug("관측 적재 투자자 %d · VI %d · 개장창 %d",
                              got["investor"], got["vi"], got["opening"])
        except Exception:  # noqa: BLE001
            log.exception("관측 수집 루프 오류")
        await asyncio.sleep(int(_cfg().get("interval_sec", 120)))


def overlap_today(symbols: set[str], now: datetime | None = None) -> set[str]:
    """오늘 우리가 건드린 종목 중 VI 가 걸렸던 것 — 사후 대조용.

    5거래일 실측에서 교집합은 0이었다. 계속 0이면 VI 는 우리와 무관한 축이고,
    0이 아니게 되는 날이 오면 그때가 안전 필터를 논할 시점이다.
    """
    day = (now or datetime.now(UTC)).astimezone(promote.KST).strftime("%Y-%m-%d")
    return symbols & store.vi_codes(day)
