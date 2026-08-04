"""KRX 장전 동시호가(08:30~09:00) 관측 — 예상체결로 개장을 미리 본다.

## 무엇을 왜

NXT 프리마켓(premarket.py, 08:00~08:50 실체결)이 못 보는 다른 축이다 — KRX
동시호가의 **예상체결가·예상체결수량**(`exp_cntr_pric`/`exp_cntr_qty`)은
"09:00 시가가 어디서 열릴 것 같은가" 의 유일한 사전 관측이고, ka10095 가
같은 행에 실어 준다(quote.extra_fields — 추가 파싱 0).

동기는 사용자 질문(2026-08-03) 그대로다: "08:30 부터 쌓이는 호가를 보고
09:00 부터 거래할 수 있는 종목을 뽑아낼 수 없나." 뽑는 것은 측정 통과 후의
일이고, 이 모듈은 그 측정의 재료를 쌓는다. 시간대 분석(2026-08-01)이 보류한
09:00 버킷 모순 — 합성(최고) vs 실거래(최악권) — 을 푸는 재료이기도 하다:
예상체결가 대비 실제 시가·체결가의 괴리가 곧 개장 마찰이다.

## 관측만 한다 (불변 조건 4)

신호·점수·주문에 넣지 않고 원장(`preopen_obs`)에만 쌓는다. 예상체결 갭이
좋은 신호인지 나쁜 신호인지 측정된 적이 없다.

## 판정 — 사전 확정 (구현 2026-08-03, 결과를 본 뒤 바꾸지 않는다)

  시점   수집 시작(2026-08-04) 4주 뒤, 2026-09-01경.
         **거래일 15일 미만이면 판정하지 말고 연기 보고.**
  지표   ① 선별력 — 마지막 스냅샷(08:56 이후) 기준 예상체결 갭
           (exp_price/전일종가 − 1) **상위 4분위** 종목의 09:00 시가 진입 →
           09:30 청산(저장 1분봉) 브래킷 R(손절 1.0~2.5% 스윕)을 같은 날
           변동성 정합(atr_pct 최근접) 무작위 대조군과 비교.
         ② 개장 마찰 — |시가 − 마지막 예상체결가| / 예상체결가 분포
           (중앙값·상위 10%). 이 값이 커지면 합성 백테스트의 09:00 우위는
           체결 불가능한 신기루라는 뜻이다.
  분할   예상체결 갭 4분위(예상체결가가 있는 종목만).
  표본 제외  ETF(이름, data/exclude), exp_price 가 없는 스냅샷(공개 전 —
         NULL 은 NULL 로 남기고 표본에서 뺀다), 전일종가 없는 종목.

## 예산 (불변 조건 8)

유니버스 = 감시목록(≤120) + 당일 NXT 프리마켓 관측 종목(≈100~130), 상한
`max_symbols`(200). ka10095 는 80종목/콜이므로 사이클당 ≤3콜, 기본 120초
주기로 08:30~09:00 창에서 **하루 ≤45콜** — 그 시간대에 장중 루프는 전부
자고 있어 경합이 없다.

## 첫 실측에서 확정할 것

예상체결가 공개 시각(08:30 부터인지 이후인지)은 문서가 아니라 이 원장이
답한다 — exp_price 가 처음 채워지는 스냅샷 시각이 그것이다. 값을 못 받는
동안은 NULL 로 쌓인다('못 쟀다' 와 '0' 을 섞지 않는다).

**확정(실측 2026-08-04, 첫 가동일)**: 08:31~08:49 스냅샷은 전부 NULL,
**08:51 스냅샷에서 첫 비NULL**(194/200종목, 이후 198/200 유지) — 공개
개시는 08:49~08:51 사이로, KRX 의 예상체결 공표 시각(08:50)과 일치한다.
따라서 08:30~08:50 구간의 NULL 은 결측이 아니라 '공개 전'이 맞고, 표본
분석은 08:51 이후 스냅샷만 쓰면 된다. 창을 08:50 부터로 좁히는 것은
관측 4주 판정(9/1) 때 콜 절감과 함께 판단한다.
"""
import asyncio
import logging
from datetime import UTC, datetime

from .. import settings
from . import store

log = logging.getLogger(__name__)
# KRX 장전 동시호가 창. 예상체결가 공개 시각이 더 늦다면 첫 실측(NULL 구간)이
# 알려 준다 — 창을 좁히는 것은 그 뒤의 일이다.
DEFAULT_WINDOW = ("08:30", "09:00")
CHUNK = 80          # ka10095 한 콜 상한 — engine.WATCH_INFO_CHUNK 와 같은 근거


def _cfg() -> dict:
    return settings.CONFIG.get("scout", {}).get("observe", {}).get("preopen", {})


def enabled() -> bool:
    return bool(_cfg().get("enabled", True))


def window() -> tuple[str, str]:
    cfg = _cfg()
    return (str(cfg.get("start") or DEFAULT_WINDOW[0]),
            str(cfg.get("end") or DEFAULT_WINDOW[1]))


def in_window(now: datetime | None = None) -> bool:
    t = (now or datetime.now(UTC)).astimezone(store.KST)
    if t.weekday() >= 5:
        return False
    lo, hi = window()
    return lo <= t.strftime("%H:%M") <= hi


def universe(now: datetime | None = None) -> tuple[list[str], dict[str, str]]:
    """감시목록 + 당일 NXT 프리마켓 관측 종목. 반환: (코드 목록, 이름 맵).

    프리마켓 종목을 합치는 이유: 08:00~08:50 에 NXT 에서 움직인 종목이
    동시호가에서 어디로 수렴하는지가 정확히 이 관측의 질문이다 — 감시목록만
    보면 그 표본이 빠진다. ka10095 는 코드만 주면 되므로 추가 TR 이 없다.
    """
    watch = dict(settings.WATCHLIST)
    names: dict[str, str] = {c: n for c, n in watch.items() if isinstance(n, str)}
    codes = list(watch)
    day = (now or datetime.now(UTC)).astimezone(store.KST).date().isoformat()
    for code, name in store.premarket_codes(day):
        if code not in watch:
            codes.append(code)
            if name:
                names.setdefault(code, name)
    cap = int(_cfg().get("max_symbols", 200))
    return codes[:cap], names


async def collect_once(now: datetime | None = None) -> int:
    """1회 스냅샷. 반환: 적재 행 수. 창 밖이면 0.

    **한 청크 실패가 나머지를 막지 않는다** — 청크는 서로 다른 종목이고,
    스냅샷 하나가 비는 것보다 절반이라도 남는 쪽이 4주 뒤 표본에 낫다.
    """
    from ..kiwoom.client import client
    from ..kiwoom.quote import parse_watch_info

    if not in_window(now):
        return 0
    codes, names = universe(now)
    if not codes:
        return 0
    quotes: dict[str, dict] = {}
    for i in range(0, len(codes), CHUNK):
        chunk = codes[i:i + CHUNK]
        try:
            quotes.update(parse_watch_info(await client.watch_info(chunk)))
        except Exception:  # noqa: BLE001 - 관측 실패가 다른 청크를 막지 않는다
            log.warning("동시호가 조회 실패 %d종목", len(chunk), exc_info=True)
    if not quotes:
        return 0
    return store.record_preopen(quotes, names, now)


async def loop() -> None:
    """동시호가 창에서만 돈다. premarket.loop 와 같은 골격이다."""
    while True:
        try:
            if enabled() and settings.KIWOOM_APP_KEY and in_window():
                n = await collect_once()
                if n:
                    log.debug("동시호가 예상체결 적재 %d종목", n)
        except Exception:  # noqa: BLE001
            log.exception("동시호가 관측 루프 오류")
        await asyncio.sleep(int(_cfg().get("interval_sec", 120)))
