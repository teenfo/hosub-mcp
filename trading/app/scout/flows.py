"""수급 관측 — 누가 샀고 누가 팔았나. 마감 후 1회.

## 무엇을 왜

`ka10086` 하나가 OHLCV + 개인·기관·외국인 순매수 + 프로그램매매 + 외국인
지분율 + 신용비율을 **한 콜에** 준다. 그래서 프로그램(ka90004)·신용(ka10013)을
따로 부르려던 계획이 사라졌다 — TR 3개를 아꼈다.

나머지 셋은 `ka10086` 이 못 주는 축이고, 대상을 **감시목록으로 한정**한다.

    ka10059  기관 세부 13주체(금융투자·보험·투신·연기금·사모 …)
    ka10014  공매도량·비중·평균가
    ka10015  장전/장중/장후 거래량 비중

전종목까지 넓히지 않는 이유는 콜이 아니라 **질문**이다. 우리가 답해야 할 것은
"우리가 산 종목의 수급이 어땠나" 이고 그건 감시목록이면 답이 된다. 축을 한꺼번에
늘리면 4주 뒤 어느 것이 유의한지 가리기만 어려워진다.

## 판단에 쓰지 않는다

체결강도·스프레드·투자자별 매매와 같은 규약이다. 기관 순매수가 좋은 신호인지
나쁜 신호인지는 측정된 적이 없다 — 5거래일 표본에서 코리아써키트는 +20.65% 인
날 기관 순매수가 -2,765 였고 우리는 -5,127 을 잃었다. 표본 하나로는 아무 말도
못 한다. 4주 뒤 잔차 IC 가 본페로니 문턱을 넘으면 그때 게이트로 올린다.

## 예산

감시목록 120종목 × 3콜 = 360콜, 4 rps 에서 약 90초. 마감 후라 경합이 없다.
전종목 `ka10086` 은 야간 배치에 얹혀 돈다(`scout/nightly.py`) — 그쪽은 이미
종목마다 일봉을 부르므로 콜이 2배가 된다(약 16분 → 33분).
"""
import asyncio
import logging
from datetime import UTC, datetime, timedelta

from .. import settings
from ..kiwoom import flows as parse
from . import store

log = logging.getLogger(__name__)


def _cfg() -> dict:
    return settings.CONFIG.get("scout", {}).get("observe", {}).get("flows", {})


def enabled() -> bool:
    return bool(_cfg().get("enabled", True))


def _name_of(v) -> str | None:
    """감시목록 값에서 종목명. 문자열이 정상이고 dict 도 받아 준다.

    이름을 못 얻는 것은 관측을 멈출 이유가 아니다 — `flow_obs.name` 은
    사람이 읽으려고 두는 칸이고 코드가 이미 키다.
    """
    if isinstance(v, str):
        return v or None
    if isinstance(v, dict):
        return v.get("name")
    return None


async def collect_symbol(code: str, name: str | None, day: datetime) -> dict:
    """감시목록 한 종목의 세부 수급 3종. 반환: 소스별 반영 일자 수.

    **한 소스가 실패해도 나머지는 넣는다.** 셋은 서로 독립인 축이고, 하나가
    죽었다고 나머지를 버리면 그날 관측이 통째로 빈다.
    """
    from ..kiwoom.client import client

    ymd = day.strftime("%Y%m%d")
    frm = (day - timedelta(days=int(_cfg().get("lookback_days", 10)))).strftime("%Y%m%d")
    got = {"detail": 0, "short": 0, "session": 0}
    jobs = (
        ("detail", lambda: client.investor_daily(code, ymd), parse.parse_investor_daily),
        ("short", lambda: client.short_trend(code, frm, ymd), parse.parse_short),
        # `ka10015` 의 `strt_dt` 는 기준일이고 거기서 **과거로** 준다 —
        # 범위 시작으로 알고 `frm` 을 넘기면 최근 날짜가 하나도 안 온다.
        ("session", lambda: client.trade_detail(code, ymd), parse.parse_trade_detail),
    )
    for key, call, fn in jobs:
        try:
            rows = fn(await call())
        except Exception:  # noqa: BLE001 - 관측 실패가 다른 축을 막지 않는다
            log.warning("수급 조회 실패 %s/%s", code, key, exc_info=True)
            continue
        if rows:
            got[key] = store.record_flows(code, rows, name)
    return got


async def collect_once(now: datetime | None = None) -> dict:
    """감시목록 전체를 훑는다. 반환: 종목 수 + 소스별 반영 합계."""
    ts = (now or datetime.now(UTC)).astimezone(store.KST)
    watch = dict(settings.WATCHLIST)
    cap = int(_cfg().get("max_symbols", 200))
    codes = list(watch)[:cap]
    total = {"symbols": 0, "detail": 0, "short": 0, "session": 0}
    for code in codes:
        # `settings.WATCHLIST` 는 `dict[str, str]` — 값이 **종목명 문자열**이다.
        # dict 로 읽고 `.get("name")` 을 부르면 AttributeError 로 루프 전체가
        # 죽는다(실측 2026-07-31, `/api/flows/run` 500). 테스트가 값을 dict 로
        # 흉내 냈던 탓에 통과했다 — 픽스처가 현실과 다르면 검사가 아니다.
        got = await collect_symbol(code, _name_of(watch.get(code)), ts)
        total["symbols"] += 1
        for k in ("detail", "short", "session"):
            total[k] += got[k]
    log.info("수급 관측 %d종목 — 세부 %d · 공매도 %d · 장전비중 %d",
             total["symbols"], total["detail"], total["short"], total["session"])
    return total


async def loop(interval_sec: int = 300) -> None:
    """평일 마감 후 1회. 재시작해도 당일 중복 실행하지 않는다.

    `eod_backfill_after`(15:35)보다 **뒤에** 둔다 — 마감 백필이 그날 분봉을
    확정하는 동안 예산을 나눠 쓰면 둘 다 늘어진다.

    완료 표시는 디스크에 남긴다(`store.job_done`). 지역변수로 들고 있으면
    재시작마다 초기화돼 배포가 잦은 날 같은 배치가 여러 번 돈다.
    """
    from ..data import store as bars_store

    fails: dict[str, int] = {}
    while True:
        try:
            now = datetime.now(store.KST)
            today = now.date().isoformat()
            if (enabled() and settings.KIWOOM_APP_KEY and now.weekday() < 5
                    and now.strftime("%H:%M") >= str(_cfg().get("after", "15:50"))
                    and not await asyncio.to_thread(
                        bars_store.job_done, "flows", today)):
                got = await collect_once(now)
                # **전 소스가 실패한 날을 완료로 마크하지 않는다.** 종전에는
                # 무조건 마크해서 토큰 만료 같은 전면 장애일의 수급 관측이
                # 통째로 비고 조용히 넘어갔다(감사 2026-08-01). 재시도는
                # 3회로 제한한다 — 120종목 × 3콜을 5분마다 무한 반복하면
                # 장애일에 레이트리밋만 태운다.
                ok = got["detail"] + got["short"] + got["session"] > 0
                if ok or got["symbols"] == 0 or fails.get(today, 0) >= 2:
                    if not ok and got["symbols"]:
                        log.error("수급 관측 %d회 전면 실패 — 오늘은 포기 "
                                  "(내일 lookback 이 메운다)", fails.get(today, 0) + 1)
                    await asyncio.to_thread(bars_store.mark_job_done, "flows", today)
                else:
                    fails[today] = fails.get(today, 0) + 1
                    fails = {today: fails[today]}       # 지난 날짜는 버린다
        except Exception:  # noqa: BLE001
            log.exception("수급 관측 루프 오류")
        await asyncio.sleep(interval_sec)
