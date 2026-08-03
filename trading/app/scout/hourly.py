"""장중 시간당 발굴 관측 — 야간 발굴의 3규칙을 **진행 중인 오늘 봉**으로 재평가.

## 무엇을 왜 (사용자 승인 2026-08-03)

야간 발굴(nightly)은 17:30 에 완성된 일봉으로 전종목을 평가한다. 장중에만
보이는 패턴 — 오전에 3규칙을 만족했다가 종가에는 사라지는 형태 — 은 그
평가가 원리적으로 못 잡는다. 이 모듈은 매시간 "**이번 시간에 새로** 3규칙을
만족하게 된 종목"(전일 chart_obs 점수 < min_score → 지금 합성 점수 ≥
min_score)을 원장에 남긴다.

**관측 전용이다** (1단계). 신호·점수·편입에 넣지 않는다 — 장중 패턴이 좋은
신호인지 야간과 중복인지 측정된 적이 없다. 소스 편입(2단계)은 아래 판정
통과 + 사용자 결정 후의 일이고, 그때도 OBSERVE_ONLY(max_tier=collect)로
들어간다.

## 방법 — 스냅샷 합성 (추가 봉 수집 없음)

저장 일봉(어제까지, 야간 배치가 매일 적재) 위에 ka10095 스냅샷(당일
O/H/L/현재가/누적 거래량)으로 오늘 봉을 합성해 `compute_features` 를 그대로
재계산한다. 유니버스는 chart_obs 최신일(유동성 통과 + ETF 제외, 약 786종목)
— 발굴 후보 자격이 있는 전량이다.

근사 두 가지(사전 명시):
  · 오늘 봉은 미완성이다 — 거래량은 세션 진행률로 일할 환산해 vol_ratio 의
    장중 과소평가를 보정한다(09:30 에 하루의 1/13 만 지난 거래량을 avg20 과
    그대로 비교하면 급증 규칙이 오후까지 침묵한다)
  · 진행률 환산은 거래가 하루에 고르게 퍼진다고 가정한다 — 실제로는 개장
    직후에 몰리므로 오전 픽의 vol_ratio 는 **과대** 방향으로 치우친다.
    판정이 대조군 비교라 방향 편향은 상쇄되지만, 원값을 원장에 남긴다

## 판정 — 사전 확정 (구현 2026-08-03, 결과를 본 뒤 바꾸지 않는다)

  시점   수집 시작 4주 뒤(2026-09-01경). **거래일 15일 미만이면 연기 보고.**
  지표   ① 픽의 익일 실현 — 픽 종목의 익일 시가 진입 브래킷 R(손절 1.0~2.5%
           스윕) vs 같은 날 변동성 정합(atr_pct 최근접) 무작위 대조군
         ② **기존 소스 중복률** — 같은 날 signals 원장(volume/gainers/
           presurge/nightly)이 지목한 종목과의 교집합 비율. **80% 이상이면
           독립 정보가 아니므로 소스 편입 없이 폐기**
  분할   픽 시각(오전 09:30~12:00 / 오후 13:00~15:00)
  표본 제외  chart_obs 없는 날(야간 배치 실패일), 픽 0건인 시각은 그대로
         0건으로 기록(못 잰 것과 구분)

## 예산 (불변 조건 8)

유니버스 786종목 / 80종목·콜 = **10콜/시간 × 장중 6회 = 하루 약 60콜**
(분당 1콜 미만). 피처 재계산은 to_thread 로 이벤트 루프 밖에서 돈다 —
pandas 연산은 GIL 을 대부분 놓으므로 틱 처리를 굶기지 않는다.
"""
import asyncio
import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from .. import settings
from ..features import compute_features
from . import store as scout_store

log = logging.getLogger(__name__)
KST = scout_store.KST
DEFAULT_WINDOW = ("09:30", "15:00")
CHUNK = 80                  # ka10095 한 콜 상한 — engine.WATCH_INFO_CHUNK 와 동일 근거
SESSION_MIN = 390.0         # 09:00~15:30
DB_PATH_FN = lambda: Path(settings.DATA_DIR) / "discovery.db"  # noqa: E731 - chart_obs 위치


def _cfg() -> dict:
    return settings.CONFIG.get("scout", {}).get("hourly", {})


def enabled() -> bool:
    return bool(_cfg().get("enabled", True))


def window() -> tuple[str, str]:
    cfg = _cfg()
    return (str(cfg.get("start") or DEFAULT_WINDOW[0]),
            str(cfg.get("end") or DEFAULT_WINDOW[1]))


def in_window(now: datetime | None = None) -> bool:
    t = (now or datetime.now(UTC)).astimezone(KST)
    if t.weekday() >= 5:
        return False
    lo, hi = window()
    return lo <= t.strftime("%H:%M") <= hi


def universe() -> list[dict]:
    """chart_obs 최신일 — 유동성 통과·ETF 제외 전량. [{code,name,score}].

    야간 배치가 실패한 날은 전날 단면이 나온다 — 평가 기준(전일 점수)으로는
    그대로 유효하다. chart_obs 자체가 없으면 빈 목록(첫 배치 전).
    """
    path = DB_PATH_FN()
    if not path.exists():
        return []
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT MAX(date) AS d FROM chart_obs").fetchone()
        day = row["d"] if row else None
        if not day:
            return []
        return [{"code": r["code"], "name": r["name"], "score": r["score"] or 0}
                for r in conn.execute(
                    "SELECT code, name, score FROM chart_obs WHERE date=?", (day,))]


def elapsed_fraction(now: datetime) -> float:
    """세션 진행률(0~1] — 거래량 일할 환산의 분모."""
    t = now.astimezone(KST)
    mins = (t.hour - 9) * 60 + t.minute
    return min(1.0, max(mins / SESSION_MIN, 1 / SESSION_MIN))


def synth_today_bar(df: pd.DataFrame, snap: dict, now: datetime) -> pd.DataFrame:
    """저장 일봉 + 스냅샷 → 오늘 봉을 붙인 프레임. 재료가 모자라면 원본 그대로.

    저장 봉의 마지막 행이 이미 오늘이면(같은 날 야간 배치 후 재실행 등)
    스냅샷으로 교체한다 — 한 날짜에 봉 두 개를 만들지 않는다.
    """
    o, h, low = snap.get("open"), snap.get("high"), snap.get("low")
    px, vol = snap.get("price"), snap.get("volume")
    if not (o and h and low and px):
        return df
    today = now.astimezone(KST).date()
    if len(df) and df.index[-1].date() == today:
        df = df.iloc[:-1]
    vol_est = int((vol or 0) / elapsed_fraction(now))
    row = pd.DataFrame(
        [{"open": o, "high": h, "low": low, "close": px, "volume": vol_est}],
        index=pd.DatetimeIndex([pd.Timestamp(today)]))
    return pd.concat([df, row])


def _evaluate(codes: list[dict], quotes: dict, now: datetime,
              min_score: int) -> tuple[int, list[dict]]:
    """동기 계산부 — to_thread 로 돈다. 반환: (평가 종목 수, 신규 픽)."""
    from ..data import store as bars_store

    evaluated, picks = 0, []
    for u in codes:
        snap = quotes.get(u["code"])
        if not snap:
            continue
        df = bars_store.load_bars(u["code"], "1d", limit=250)
        if len(df) < 60:
            continue
        f = compute_features(synth_today_bar(df, snap, now))
        if f is None:
            continue
        evaluated += 1
        # **신규 충족만** 픽이다 — 전일에 이미 점수를 갖고 있던 종목은 야간
        # 발굴이 이미(혹은 오늘 저녁에) 다룬다. 이 관측의 질문은 "장중에
        # 새로 생긴 패턴" 이다.
        if f["score"] >= min_score and (u.get("score") or 0) < min_score:
            picks.append({"code": u["code"], "name": u["name"],
                          "score": f["score"], "prev_score": u.get("score") or 0,
                          "close": f["close"],
                          "reasons": f.get("reasons") or []})
    picks.sort(key=lambda p: (-p["score"], p["code"]))
    return evaluated, picks


async def evaluate_once(now: datetime | None = None) -> dict:
    """1회 평가. 반환: {evaluated, picks}. 창 밖이면 빈 결과."""
    from ..kiwoom.client import client
    from ..kiwoom.quote import parse_watch_info

    now = now or datetime.now(UTC)
    if not in_window(now):
        return {"evaluated": 0, "picks": 0}
    codes = universe()
    if not codes:
        log.info("시간당 발굴 — chart_obs 가 비어 있다(첫 야간 배치 전)")
        return {"evaluated": 0, "picks": 0}
    quotes: dict[str, dict] = {}
    ordered = [u["code"] for u in codes]
    for i in range(0, len(ordered), CHUNK):
        chunk = ordered[i:i + CHUNK]
        try:
            quotes.update(parse_watch_info(await client.watch_info(chunk)))
        except Exception:  # noqa: BLE001 - 한 청크 실패가 나머지를 막지 않는다
            log.warning("시간당 발굴 스냅샷 실패 %d종목", len(chunk), exc_info=True)
    if not quotes:
        return {"evaluated": 0, "picks": 0}
    min_score = int(_cfg().get("min_score", 2))
    evaluated, picks = await asyncio.to_thread(
        _evaluate, codes, quotes, now, min_score)
    cap = int(_cfg().get("max_picks", 50))
    if len(picks) > cap:
        log.warning("시간당 발굴 픽 %d건 > 상한 %d — 상한만 기록(문턱 검토 필요)",
                    len(picks), cap)
        picks = picks[:cap]
    scout_store.record_hourly(picks, evaluated, now)
    if picks:
        log.info("시간당 발굴 — 평가 %d종목 · 신규 픽 %d건: %s", evaluated,
                 len(picks), " ".join(p["code"] for p in picks[:10]))
    return {"evaluated": evaluated, "picks": len(picks)}


async def loop() -> None:
    """장중 매시. 픽이 있으면 슬랙으로도 알린다 — 사용자가 장중 후보를 보게
    하는 것이 이 관측의 절반이다(승인대기 확대 논의 2026-08-03)."""
    from ..notify import slack

    next_due = 0.0
    while True:
        try:
            mono = asyncio.get_event_loop().time()
            if (enabled() and settings.KIWOOM_APP_KEY and in_window()
                    and mono >= next_due):
                got = await evaluate_once()
                next_due = mono + int(_cfg().get("interval_sec", 3600))
                if got["picks"]:
                    rows = scout_store.hourly_latest()
                    lines = [f"🕐 장중 신규 발굴 후보 {got['picks']}건 "
                             f"(관측 전용 — 편입·매매 아님)"]
                    lines += [f"• {r['name']}({r['code']}) 점수 {r['score']}"
                              f" ({', '.join(json.loads(r['reasons'] or '[]'))})"
                              for r in rows[:5]]
                    await slack.notify("\n".join(lines))
        except Exception:  # noqa: BLE001
            log.exception("시간당 발굴 루프 오류")
        await asyncio.sleep(60)
