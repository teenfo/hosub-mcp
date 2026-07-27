"""뉴스 영향 소급 측정 — LLM 이 매기는 `impact_horizon`·`impact_direction` 이
실제 수익률과 상관이 있는가.

## 왜 지금

두 필드 다 매 분석마다 LLM 이 만들어 저장하는데, `impact_horizon` 은 **어떤
판정에도 쓰이지 않는다** — 점수·알림·편입 어디에도 안 들어가고 화면 뱃지로만
쓰인다. 누적 수천 건이 이미 있으므로 **오늘 측정 가능하다.** 상관이 없으면
LLM 스키마에서 빼는 것이 맞다(프롬프트가 짧아지고 재시도가 줄어든다).

`impact_direction` 은 사정이 다르다 — **발굴 엔진의 news 어댑터가 이 값으로
악재를 걸러낸다.** 그 필터가 근거 있는지 같이 재지 않으면 잡음을 필터라고
믿는 셈이다.

## 무엇을 재는가

`impact_horizon` 은 "영향이 얼마나 오래 가는가" 를 말한다. 그러면 즉시(immediate)
공시는 1일에 몰리고 3·5일에는 사라져야 하고, 장기(long)는 오래 남아야 한다.
**수익률의 크기가 아니라 곡선의 모양**이 검증 대상이다.

    fwd_1 / fwd_3 / fwd_5   t+1 시가 진입 → t+k 종가 (eventstudy 와 같은 정의)

진입을 t+1 시가로 잡는 이유는 이벤트 스터디와 같다 — 장중에 뜬 뉴스를 그
시각에 잡을 수 있었다고 가정하면 실현 불가능한 수익을 세게 된다. 보수적으로
다음 날 시가부터 잡는다.

## 시장 효과 제거

뉴스는 시장이 크게 움직인 날에 몰린다. 원시 수익률을 그대로 보면 뉴스의 공이
아니라 그날 시장을 재게 된다. 같은 날 전 종목 평균을 빼 초과수익으로 본다.
"""
import json
import logging
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .. import settings
from ..data import store
from . import eventstudy
from . import panel as panel_mod

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
OUT_FILE = Path(settings.DATA_DIR) / "news_impact.json"

HORIZONS = ("immediate", "short", "long")
HORIZON_KO = {"immediate": "즉시", "short": "단기", "long": "장기"}
DIRECTIONS = ("positive", "neutral", "negative")
DIRECTION_KO = {"positive": "호재", "neutral": "중립", "negative": "악재"}
TARGETS = ("fwd_1", "fwd_3", "fwd_5")
MIN_BUCKET = 30       # 이보다 적은 표본의 수치는 믿지 않는다
PAGE = 500            # TNM list_items 의 limit 상한
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def fetch_analyses(limit: int = 20_000, min_score: int = 0) -> list[dict]:
    """TNM 분석 전량을 페이지로 받아 온다.

    **trading 자신의 INTERNAL_TOKEN 이 아니라 TNM_TOKEN 을 쓴다** — 서비스마다
    토큰이 다르다(2026-07-27 발굴 엔진 news 어댑터가 이걸로 401 을 맞았다).
    """
    import httpx

    if not settings.TNM_TOKEN:
        return []
    out: list[dict] = []
    with httpx.Client(timeout=30.0) as c:
        while len(out) < limit:
            res = c.get(
                f"{settings.TNM_URL}/api/items",
                params={"limit": PAGE, "offset": len(out),
                        "status": settings.TNM_STATUS_OK,
                        "min_score": min_score},
                headers={"X-Internal-Token": settings.TNM_TOKEN})
            res.raise_for_status()
            page = res.json().get("items") or []
            out.extend(page)
            if len(page) < PAGE:
                break              # 마지막 페이지
    return out[:limit]


def event_date(item: dict) -> str | None:
    """뉴스 발행 시각 → KST 날짜. 이게 이벤트의 t0 다."""
    raw = str(item.get("published_at") or "")
    if not raw:
        return None
    try:
        t = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        # 파싱은 실패했지만 앞이 날짜꼴이면 그것만 쓴다. 아무 문자열이나
        # 통과시키면 조인에서 조용히 안 맞고 '봉이 없는 종목' 으로 오인된다.
        head = raw[:10]
        return head if _DATE_RE.fullmatch(head) else None
    if t.tzinfo:
        t = t.astimezone(KST)
    return t.date().isoformat()


def forward_map(symbols: set[str]) -> pd.DataFrame:
    """대상 종목만 일봉을 읽어 날짜별 선행 수익률 표를 만든다.

    전종목을 읽지 않는 이유는 뉴스가 붙은 종목이 훨씬 적기 때문이다 —
    이벤트 스터디는 3,907종목을 읽느라 50초를 쓴다.
    """
    frames = []
    for code in sorted(symbols):
        df = store.load_bars(code, "1d", limit=600)
        if len(df) < 2:
            continue
        f = panel_mod.forward(df[["open", "close"]], horizons=(1, 3, 5))
        f = f.reset_index().rename(columns={"index": "ts", "ts": "date"})
        f["date"] = pd.to_datetime(f["date"]).dt.date.astype(str)
        f["symbol"] = code
        frames.append(f[["symbol", "date", *TARGETS]])
    if not frames:
        return pd.DataFrame(columns=["symbol", "date", *TARGETS])
    return pd.concat(frames, ignore_index=True)


def market_forward(market: dict[str, float],
                   horizons=(1, 3, 5)) -> dict[str, dict[int, float]]:
    """날짜별 **선행 k일 누적** 시장 수익률 — 종목 수익률과 기간을 맞춘다.

    이걸 안 하면 3일·5일 종목 수익률에서 **하루치** 시장 수익률만 빼게 되어
    시장 노출이 덜 빠진다. 초안이 정확히 그랬다 — 게다가 뺄 값도 이벤트 당일
    (t0)이라 종목 쪽 구간(t+1~t+k)과 하루 어긋나 있었다.

    종목의 fwd_k 는 t+1 시가 → t+k 종가다. 그래서 시장도 t+1 부터 t+k 까지의
    일간 수익률을 더한다. 거래일이 모자라면 None — 반쪽짜리 창으로 빼지 않는다.
    """
    days = sorted(market)
    out: dict[str, dict[int, float]] = {}
    for i, d in enumerate(days):
        out[d] = {}
        for k in horizons:
            win = days[i + 1:i + 1 + k]
            out[d][k] = sum(market[x] for x in win) if len(win) == k else None
    return out


def join(items: list[dict], fwd: pd.DataFrame,
         market: dict[str, float]) -> pd.DataFrame:
    """분석 레코드 × 선행 수익률. 시장 효과를 뺀 초과수익까지 붙인다."""
    rows = []
    for it in items:
        code, date = str(it.get("ticker") or ""), event_date(it)
        if not code or not date:
            continue
        rows.append({
            "symbol": code, "date": date,
            "horizon": it.get("impact_horizon") or "",
            "direction": it.get("impact_direction") or "",
            "category": it.get("category") or "",
            "score": it.get("score"),
        })
    if not rows or fwd.empty:
        return pd.DataFrame()
    df = pd.DataFrame(rows).merge(fwd, on=["symbol", "date"], how="inner")
    # 뉴스는 시장이 크게 움직인 날에 몰린다 — 그날 시장을 뉴스의 공으로 세지 않는다.
    # **기간을 맞춰서 뺀다** — 3일 수익률에서 하루치 시장을 빼면 노출이 남는다.
    mf = market_forward(market, horizons=[int(t.split("_")[1]) for t in TARGETS])
    for t in TARGETS:
        k = int(t.split("_")[1])
        adj = df["date"].map(lambda d, _k=k: (mf.get(d) or {}).get(_k))
        # 시장 구간을 못 채운 날(표본 끝)은 초과수익을 내지 않는다 — 0으로
        # 채우면 시장 몫이 그대로 뉴스의 공으로 남는다.
        df[f"exc_{t}"] = df[t] - adj
    return df.dropna(subset=[TARGETS[0]])


def _bucket(g: pd.DataFrame) -> dict:
    out = {"n": len(g)}
    for t in TARGETS:
        vals = g[t].dropna()
        exc = g[f"exc_{t}"].dropna()
        out[t] = round(float(vals.mean()), 3) if len(vals) else None
        out[f"exc_{t}"] = round(float(exc.mean()), 3) if len(exc) else None
        out[f"t_{t}"] = eventstudy._t_stat(
            float(exc.mean()), float(exc.std()), len(exc)) if len(exc) > 1 else 0.0
    out["reliable"] = len(g) >= MIN_BUCKET
    return out


def _by(df: pd.DataFrame, col: str, order: tuple) -> list[dict]:
    known = [v for v in order if v in set(df[col])]
    extra = sorted(set(df[col]) - set(order) - {""})
    return [{"key": v, **_bucket(df[df[col] == v])} for v in known + extra]


CAVEATS = [
    "진입은 t+1 시가 기준이다. 장중에 뜬 뉴스를 그 시각에 잡았다고 가정하면 "
    "실현 불가능한 수익을 세게 되므로 보수적으로 다음 날부터 잡는다.",
    "초과수익(exc_)은 같은 날 전 종목 평균을 뺀 값이다 — 뉴스가 몰린 날의 "
    "시장 움직임을 뉴스의 공으로 돌리지 않는다.",
    f"표본 {MIN_BUCKET}건 미만 버킷은 reliable=false — 수치를 믿지 않는다.",
    "일봉이 감시목록 수집분이라, 뉴스가 붙었지만 봉이 없는 종목은 빠진다(생존 편향).",
    "impact_horizon 은 '얼마나 오래 가는가' 를 말한다. 수익률의 크기가 아니라 "
    "1일 → 3일 → 5일 곡선의 **모양**이 검증 대상이다.",
]


def analyze(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"rows": 0}
    return {
        "rows": len(df),
        "symbols": int(df["symbol"].nunique()),
        "date_from": df["date"].min(), "date_to": df["date"].max(),
        "by_horizon": _by(df, "horizon", HORIZONS),
        "by_direction": _by(df, "direction", DIRECTIONS),
        "by_category": sorted(_by(df, "category", ()), key=lambda r: -r["n"])[:12],
        "horizon_ko": HORIZON_KO, "direction_ko": DIRECTION_KO,
        "targets": list(TARGETS), "min_bucket": MIN_BUCKET,
        "caveats": CAVEATS,
    }


def run_once() -> dict:
    if not settings.TNM_TOKEN:
        return {"ok": False, "error": "TNM_TOKEN 미설정 — 분석 목록을 읽을 수 없습니다"}
    items = fetch_analyses()
    if not items:
        return {"ok": False, "error": "TNM 분석 기록이 없습니다"}
    symbols = {str(i.get("ticker") or "") for i in items} - {""}
    log.info("뉴스 영향 측정: 분석 %d건 / %d종목", len(items), len(symbols))
    fwd = forward_map(symbols)
    market = store.market_returns()
    df = join(items, fwd, market)
    if df.empty:
        return {"ok": False, "error": "일봉과 겹치는 분석이 없습니다(종목 봉 미수집)"}
    result = analyze(df) | {
        "ok": True,
        "run_ts": datetime.now(KST).isoformat(timespec="seconds"),
        "analyses": len(items), "matched": len(df),
    }
    save(result)
    log.info("뉴스 영향 측정 완료: %d건 매칭", len(df))
    return result


def save(result: dict) -> None:
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(result, ensure_ascii=False, default=str),
                        encoding="utf-8")


def latest() -> dict:
    if not OUT_FILE.exists():
        return {"ok": False, "run_ts": None,
                "error": "아직 실행되지 않았습니다 — '지금 실행'을 누르세요"}
    try:
        return json.loads(OUT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": False, "run_ts": None, "error": "결과 파일을 읽을 수 없습니다"}
