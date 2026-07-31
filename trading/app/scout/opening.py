"""개장 창(09:00~09:15) 시장 상태 관측 — 하루 한 행.

## 왜 창을 열지 않고 관측부터 하나

실측 2026-07-31, 5거래일 1,650여 종목·일. 진입 시각별로 09:30 까지 보유한
수익(비용 전)은 개장 직후에 몰려 있다.

    09:02  +0.724%    09:10  +0.520%    09:15  +0.033%   <- 여기서 0
    09:03  +0.697%    09:13  +0.247%    09:18  -0.182%
    09:05  +0.416%    09:14  +0.216%    09:30  -0.524%

그리고 우리 실거래 107건 중 **09:15 이전 진입은 0건**이다. 규칙의 봉 요구량
(orb 09:15 대기 · momentum 25봉 · pullback 40봉)이 정확히 이 창을 막는다.
엣지가 살아 있는 동안 거래하지 않고 소멸한 뒤부터 시작하고 있었다.

**그런데 이 엣지는 지금 쓸 수 없다.** 같은 거래(09:03 진입 → 09:30 청산)를
날짜별로 쪼개면 분산이 엣지의 4배다.

    7/27 +1.105% 승률 73.3%   7/28 -1.553% 승률 17.9%
    7/30 +1.932% 승률 75.6%   7/29 -0.985% 승률 36.7%
    7/31 +1.350% 승률 73.8%

종목 선택 엣지가 아니라 **그날 시장이 올랐는가**다. 손절을 걸면 사라지는 것도
같은 이야기다(손절 없음 +0.385% → 1.0% 손절 -0.034%). 5일 표본으로 규칙을
만들면 1.5단계에서 폐기한 실수를 반복한다.

그래서 창을 여는 대신 **그 시각의 시장 상태를 남긴다.** 4주 뒤 물을 것은
"개장 창 진입의 성적이 이 값들로 갈리는가" 다 — 갈리면 시장 방향 필터가 분산을
줄인다는 뜻이고, 안 갈리면 이 창은 그냥 베타이므로 열지 않는다.

## 추가 API 호출은 없다

이미 저장된 1분봉에서 계산한다. 종목별 원시값은 봉에 그대로 있으므로 남기지
않는다 — 사후에 정확히 재계산되는 값을 또 쌓는 것은 중복이다. 남길 가치가
있는 것은 **유니버스 전체를 훑어야 나오는 집계**(개장 폭·급등 종목 수)뿐이다.
"""
import logging
import statistics as st
from datetime import UTC, datetime, time

from .. import settings
from ..data import store as bars_store
from . import promote, store

log = logging.getLogger(__name__)
OPEN, CLOSE = time(9, 0), time(9, 15)


def _cfg() -> dict:
    return settings.CONFIG.get("scout", {}).get("observe", {}).get("opening", {})


def enabled() -> bool:
    return bool(_cfg().get("enabled", True))


def window_stats(symbols: list[str], day: str) -> dict | None:
    """감시 종목의 개장 창 집계. 표본이 모자라면 None.

    표본 하한을 두는 이유: 재시작 직후나 백필 실패로 몇 종목만 잡힌 날의 집계를
    남기면, 4주 뒤 그 행이 다른 날과 같은 무게로 읽힌다. 남기지 않는 편이 낫다.
    """
    chg, rng, rvol = [], [], []
    for code in symbols:
        df = bars_store.load_bars(code, "1m", limit=800)
        if df.empty:
            continue
        same = df[df.index.date == datetime.fromisoformat(day).date()]
        win = same[(same.index.time >= OPEN) & (same.index.time < CLOSE)]
        if len(win) < 10:
            continue
        op = float(win["open"].iloc[0])
        if op <= 0:
            continue
        chg.append((float(win["close"].iloc[-1]) - op) / op * 100)
        lo = float(win["low"].min())
        if lo > 0:
            rng.append((float(win["high"].max()) - lo) / lo * 100)
        # RVOL — 개장 창 평균 봉 거래량 / 그날 나머지 평균. 그날 안에서만 비교하므로
        # 종목 규모에 무관하다. 창 뒤 봉이 없으면(장 초반 실행) 건너뛴다.
        rest = same[same.index.time >= CLOSE]
        base = float(rest["volume"].mean()) if len(rest) else 0.0
        if base > 0:
            rvol.append(float(win["volume"].mean()) / base)
    if len(chg) < int(_cfg().get("min_symbols", 30)):
        return None
    return {
        "n": len(chg),
        "up_ratio": round(sum(1 for c in chg if c > 0) / len(chg) * 100, 2),
        "mean_chg": round(st.mean(chg), 4),
        "median_chg": round(st.median(chg), 4),
        "surge3": sum(1 for c in chg if c >= 3),
        "surge5": sum(1 for c in chg if c >= 5),
        "mean_range": round(st.mean(rng), 4) if rng else None,
        "mean_rvol": round(st.mean(rvol), 4) if rvol else None,
    }


def collect_once(now: datetime | None = None) -> bool:
    """오늘 아직 안 남겼으면 남긴다. 반환: 새로 썼는가."""
    ts = (now or datetime.now(UTC)).astimezone(promote.KST)
    day = ts.strftime("%Y-%m-%d")
    # 창이 닫힌 뒤에 잰다 — 형성 중인 창을 재면 매 사이클 다른 값이 나온다.
    if ts.time() < CLOSE:
        return False
    stats = window_stats(list(settings.WATCHLIST.keys()), day)
    if not stats:
        return False
    wrote = store.record_opening(day, stats, ts)
    if wrote:
        log.info("개장 창 관측 %s: %d종목 · 상승 %.1f%% · 평균 %+.2f%% · 급등(3%%) %d",
                 day, stats["n"], stats["up_ratio"], stats["mean_chg"],
                 stats["surge3"])
    return wrote
