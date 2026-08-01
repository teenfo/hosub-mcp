"""저장 분봉 기반 마감 관측 — 매물대 + VPIN. API 콜 0.

연구 문서(2026-08-01) 반영도 점검에서 확인된 공백 두 개를 메운다:
매물대(§3.2)와 VPIN(§6). 둘 다 재료가 **이미 DB 에 있는 1분봉**이라
키움 콜을 한 번도 쓰지 않는다 — 그래서 flows(감시목록 × 3콜)와 달리
예산 절이 없다.

## 관측 전용 규약

다른 관측 수집기(체결강도·투자자별·VI·수급)와 같다: 신호·점수·승격·주문
어디에도 넣지 않는다. 4주 뒤 물을 질문을 미리 못박는다 —
  매물대: 진입가의 밸류에어리어 내/외 위치가 실현 R 과 상관있나
  VPIN:  높은 VPIN 날의 **다음 날** 이 실제로 나빴나(분포 상위 20% 기준)

## 스케줄

평일 마감 백필(15:35) 뒤 — 분봉이 확정된 다음이어야 한다. 하루 1회,
완료 표시는 디스크(job_marks). 대상은 감시목록 전체.
"""
import asyncio
import logging
from datetime import UTC, datetime

from .. import settings
from ..research import profile as profile_mod
from ..research import vpin as vpin_mod
from . import store

log = logging.getLogger(__name__)

JOB = "bars_obs"


def _cfg() -> dict:
    return settings.CONFIG.get("scout", {}).get("observe", {}).get("bars", {})


def enabled() -> bool:
    return bool(_cfg().get("enabled", True))


def _name_of(v) -> str | None:
    if isinstance(v, str):
        return v or None
    if isinstance(v, dict):        # flows._name_of 와 같은 방어 — 픽스처 호환
        return v.get("name")
    return None


def collect_symbol(code: str, name: str | None, day: str) -> dict:
    """한 종목의 매물대(며칠치)와 VPIN(당일). 동기 — 전부 DB 읽기다."""
    from ..data import store as bars_store

    cfg = _cfg()
    got = {"profile": 0, "vpin": 0}
    # 매물대는 여러 날(기본 10일 ≈ 3,900봉) — 지지·저항은 하루로 안 선다
    bars = bars_store.load_bars(code, "1m",
                                int(cfg.get("profile_days", 10)) * 390)
    prof = profile_mod.volume_profile(bars, int(cfg.get("bins", 24)))
    if prof.get("ok"):
        store.record_profile(day, code, prof, name)
        got["profile"] = 1
    # VPIN 은 당일 — 문서의 용법이 '오늘의 독성' 이다. load_bars 는 ts 를
    # DatetimeIndex 로 돌려준다.
    if not bars.empty:
        today = bars[bars.index.strftime("%Y-%m-%d") == day]
        vp = vpin_mod.vpin(today, int(cfg.get("vpin_buckets", 50)))
        if vp.get("ok"):
            store.record_vpin(day, code, vp, name)
            got["vpin"] = 1
    return got


async def collect_once(now: datetime | None = None) -> dict:
    ts = (now or datetime.now(UTC)).astimezone(store.KST)
    day = ts.date().isoformat()
    watch = dict(settings.WATCHLIST)
    cap = int(_cfg().get("max_symbols", 200))
    total = {"symbols": 0, "profile": 0, "vpin": 0}
    for code in list(watch)[:cap]:
        try:
            got = await asyncio.to_thread(
                collect_symbol, code, _name_of(watch.get(code)), day)
        except Exception:  # noqa: BLE001 - 한 종목 실패가 나머지를 막지 않는다
            log.warning("분봉 관측 실패 %s", code, exc_info=True)
            continue
        total["symbols"] += 1
        total["profile"] += got["profile"]
        total["vpin"] += got["vpin"]
    log.info("분봉 관측 %d종목 — 매물대 %d · VPIN %d",
             total["symbols"], total["profile"], total["vpin"])
    return total


async def loop(interval_sec: int = 300) -> None:
    """평일 마감 후 1회 — eod_backfill(15:35)이 분봉을 확정한 **뒤**여야 한다.

    전면 실패(전 종목 ok=False)는 3회까지 재시도하고 그날은 접는다 — 콜 0
    이어도 5분마다 200종목 × 3,900봉 DB 읽기는 공짜가 아니다(감사 M12).
    """
    from ..data import store as bars_store

    fails: dict[str, int] = {}
    while True:
        try:
            now = datetime.now(store.KST)
            today = now.date().isoformat()
            if (enabled() and now.weekday() < 5
                    and now.strftime("%H:%M") >= str(_cfg().get("after", "15:55"))
                    and not await asyncio.to_thread(
                        bars_store.job_done, JOB, today)):
                got = await collect_once(now)
                # 전 종목이 ok=False(분봉 부족 등)면 재시도하되 3회 상한 —
                # 무한 반복이면 마감 후 자정까지 5분마다 200종목 × 3,900봉
                # DB 읽기를 돌린다(감사 2026-08-01 M12). 콜 0 이어도 공짜가 아니다.
                if (got["profile"] or got["vpin"] or got["symbols"] == 0
                        or fails.get(today, 0) >= 2):
                    await asyncio.to_thread(bars_store.mark_job_done, JOB, today)
                else:
                    fails = {today: fails.get(today, 0) + 1}
        except Exception:  # noqa: BLE001
            log.exception("분봉 관측 루프 오류")
        await asyncio.sleep(interval_sec)
