"""NXT 프리마켓(08:00~08:50) 관측 — 우리가 못 보던 한 시간.

## 무엇을 못 보고 있었나

`stex_tp` 를 실호출로 확정한 결과(`kiwoom/venue.py`), 순위 3종·관측 2종·주문·
잔고가 **전부 KRX** 였다. NXT(넥스트레이드)는 08:00 부터 실제 체결이 일어나고
지정가 주문을 받는다.

    NXT   프리마켓 08:00~08:50 · 메인 09:00:30~15:20 · 애프터 15:30~20:00
    KRX   동시호가 08:30~09:00 · 정규장 09:00~15:30

이게 앞선 측정과 이어진다. 진입 시각별 09:30 청산 수익(비용 전)이

    09:02 +0.724%  09:03 +0.697%  09:15 +0.033%  09:30 -0.524%

로 개장 직후에 몰렸는데, **그 시점에 가격 발견은 이미 한 시간 진행돼 있었다.**
그 한 시간이 무엇이었는지가 지금 어디에도 남지 않는다. KRX 동시호가의 예상체결
(`exp_cntr_pric`)은 성격이 다르다 — 그건 미체결 호가의 추정이고 이쪽은 실제
체결이다.

## 관측만 한다

신호를 내지 않고 원장에만 쌓는다. 프리마켓 활동이 좋은 신호인지 나쁜 신호인지
측정된 적이 없고, 지금 판단 경로에 넣으면 발굴 3규칙 점수가 저지른 일을
반복한다. 4주 뒤 물을 것은 "프리마켓에서 움직인 종목의 정규장 성적이 다른가" 다.

**주문은 여전히 KRX 로만 나간다.** 이 모듈은 `stex_tp` 를 조회에만 쓰고
`dmst_stex_tp` 는 건드리지 않는다 — NXT 라우팅은 별개의 결정이다.

## 예산

거래대금 상위 1콜에 100종목. 기본 주기 120초면 프리마켓 50분에 **약 25콜**이다.
그 시간대에는 다른 루프가 전부 자고 있다(장중 소스는 `in_session` 09:00~15:30,
데스크는 보유 종목이 없다).
"""
import asyncio
import logging
from datetime import UTC, datetime, time

from .. import settings
from ..kiwoom import venue
from ..signals.scanner import parse_rank
from . import store

log = logging.getLogger(__name__)
# 넥스트레이드 프리마켓. 창을 넘겨 부르면 빈 응답이거나 정규장 값이 섞인다.
DEFAULT_WINDOW = ("08:00", "08:50")


def _cfg() -> dict:
    return settings.CONFIG.get("scout", {}).get("observe", {}).get("premarket", {})


def enabled() -> bool:
    return bool(_cfg().get("enabled", True))


def window() -> tuple[str, str]:
    """관측 창. 애프터마켓(15:30~20:00)을 보고 싶으면 설정만 바꾸면 된다 —
    코드는 창을 모른다."""
    cfg = _cfg()
    return (str(cfg.get("start") or DEFAULT_WINDOW[0]),
            str(cfg.get("end") or DEFAULT_WINDOW[1]))


def in_window(now: datetime | None = None) -> bool:
    t = (now or datetime.now(UTC)).astimezone(store.KST)
    if t.weekday() >= 5:
        return False
    lo, hi = window()
    return lo <= t.strftime("%H:%M") <= hi


def to_rows(payload: dict) -> list[dict]:
    """순위 응답 → 적재 행. **코드 접미사를 벗기고 거래소를 따로 싣는다.**

    `parse_rank` 를 재사용하는 이유는 화면·엔진이 보는 파싱과 갈리지 않기
    위해서다. 다만 그 함수는 `lstrip("A")` 만 하므로 `005930_NX` 가 그대로
    통과한다 — 여기서 정규화한다.

    KRX 로 읽힌 행은 버린다. NXT 를 요청했는데 접미사가 없는 행이 왔다면 그건
    거래소 구분이 없는 응답이라는 뜻이고, 그 상태로 섞어 쌓으면 나중에 이 표가
    무엇의 기록인지 말할 수 없게 된다.
    """
    out = []
    for i, r in enumerate(parse_rank(payload), start=1):
        code, vn = venue.split_venue(r.get("code", ""))
        if vn != venue.NXT or not (code.isdigit() and len(code) == 6):
            continue
        out.append({
            "code": code, "venue": vn, "name": r.get("name"), "rank": i,
            "price": r.get("price"), "change_pct": r.get("change_pct"),
            "trade_value": r.get("trade_value"), "volume": r.get("volume"),
        })
    return out


async def collect_once(now: datetime | None = None) -> int:
    """1회 수집. 반환: 적재 행 수. 창 밖이면 0."""
    from ..kiwoom.client import client

    if not in_window(now):
        return 0
    try:
        raw = await client.trade_value_rank(
            str(_cfg().get("market", "000")), stex_tp=venue.TP_NXT)
    except Exception:  # noqa: BLE001 - 관측 실패가 다른 것을 막지 않는다
        log.warning("NXT 프리마켓 조회 실패", exc_info=True)
        return 0
    rows = to_rows(raw)
    if not rows:
        # 조용히 0건이 이어지면 창이 틀렸거나 stex_tp 가 안 먹는 것이다.
        # `source_health` 의 empty 카운터와 같은 이유로 로그를 남긴다.
        log.info("NXT 프리마켓 응답에 NXT 행이 없다 — 창·stex_tp 확인 필요")
        return 0
    return store.record_premarket(rows, now)


async def loop() -> None:
    """프리마켓 창에서만 돈다. 창 밖에서는 순위가 정규장 값이라 의미가 없다."""
    while True:
        try:
            if enabled() and settings.KIWOOM_APP_KEY and in_window():
                n = await collect_once()
                if n:
                    log.debug("NXT 프리마켓 적재 %d종목", n)
        except Exception:  # noqa: BLE001
            log.exception("NXT 프리마켓 관측 루프 오류")
        await asyncio.sleep(int(_cfg().get("interval_sec", 120)))
