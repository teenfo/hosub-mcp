"""야간 배치 — 발굴 엔진(scout)의 전종목 수집 팔. 옛 `app/discovery.py`.

2026-08-01 완전 폐기 이관: 감시목록 직접 편입이 먼저 삭제됐고(#245), 남아 있던
수집 기능(전종목 일봉·피처·국면·표본 선정)도 엔진 패키지로 들어왔다.
독립 모듈 discovery 는 더 이상 존재하지 않는다 — 이 배치는 엔진의 한 팔이고,
산출물(picks)은 NightlySource 가 신호로 올린다.

데이터 연속성을 위해 바꾸지 않은 것 둘: DB 파일명(discovery.db — 원장 이력이
이어져야 4주 측정이 성립한다)과 무작위 시드 문자열(f"{day}:discovery" —
시드가 바뀌면 같은 날 표본이 달라져 재현성이 깨진다). 이름이 아니라 데이터
규약이다.

흐름 (평일 장 마감 후 1회):
  1. 전종목 리스트 조회 (ka10099, 코스피+코스닥)
  2. 종목별 일봉 수집 (ka10081, 레이트리밋 4req/s 준수 → 전종목 약 12분)
  3. 스크리닝 3규칙 + 합산 점수 → 상위 N 을 SQLite 에 저장
스크리닝 규칙 (전일 종가 기준):
  - vol_surge: 전일 거래량 ≥ 20일 평균의 N배 (세력 유입 흔적)
  - near_high: 종가가 60일 최고가의 97% 이상 (신고가 돌파 임박)
  - ma_align: 5>20>60 정배열이 최근 5일 내 새로 형성 (추세 전환 초기)
발굴은 후보 제시까지만 — 진입은 장중 신호 엔진과 승인 흐름이 담당한다.
"""
import asyncio
import json
import logging
import random
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .. import export, settings
from ..data import store
from ..data.collector import parse_chart_response
from ..features import compute_features
from ..kiwoom import flows as kflows
from . import store as scout_store

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
DB_PATH = Path(settings.DATA_DIR) / "discovery.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS picks (
            date TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
            close INTEGER, score REAL, reasons TEXT,
            PRIMARY KEY (date, code)
        )"""
    )
    # 차트 지표 원장 — **유동성 통과 전량**(ETF/ETN/리츠 제외, 하루 약 780행)이다.
    # 전종목 피처는 계산되지만 여기 남는 것은 매매 가능 유니버스뿐이다 — 4주 뒤
    # 지표-실현 R 상관을 물을 표본의 정의가 이것이다. '전종목 원장' 으로 읽으면
    # 표본 정의를 착각한다(전종목 피처는 features.csv 내보내기에만 실린다).
    conn.execute(
        """CREATE TABLE IF NOT EXISTS chart_obs (
            date TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
            score REAL, feats TEXT,
            PRIMARY KEY (date, code)
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_chart_obs_code ON chart_obs (code, date)")
    # 표본 구분 — `score`(3규칙 상위) 인가 `random`(유동성 유니버스 무작위) 인가.
    # 1.5단계 실측에서 무작위가 모든 랭킹을 이겼다(random -0.184R vs score -0.502R).
    # 그 결과를 실거래에서 다시 확인하려면 두 표본이 **같은 게이트**를 통과해
    # 나란히 돌아야 한다. 이 컬럼이 그 대조를 가능하게 한다.
    have = {r[1] for r in conn.execute("PRAGMA table_info(picks)")}
    if "pick_kind" not in have:
        conn.execute("ALTER TABLE picks ADD COLUMN pick_kind TEXT")
    return conn


def parse_stock_list(raw: dict) -> list[dict]:
    """ka10099 응답에서 종목 배열 추출. 실제 응답은 배열 키 'list',
    필드 code/name (문서의 stk_infr/stk_cd/stk_nm 과 다름 — 실호출 검증됨).
    두 형식 모두 수용한다."""
    items = None
    for v in raw.values():
        if isinstance(v, list) and v and isinstance(v[0], dict) and (
            "code" in v[0] or "stk_cd" in v[0]
        ):
            items = v
            break
    out = []
    for it in items or []:
        code = str(it.get("code") or it.get("stk_cd") or "").lstrip("A_")
        name = it.get("name") or it.get("stk_nm") or it.get("list_nm") or ""
        if code.isdigit() and len(code) == 6:
            out.append({"code": code, "name": name})
    return out


# 제외 정책은 data/exclude.py 가 단일 소스다 — scanner 와 서로 다르게 판정해
# 실매수 사고가 났다(2026-07-27 462900 KoAct). 여기서는 재노출만 한다.
from ..data.exclude import is_excluded  # noqa: E402  (기존 import 경로 유지)


def screen_pass(f: dict, cfg: dict) -> bool:
    """문서 조건식(2026-08-01 공유) 통과 여부. 재료는 이미 계산돼 있다 — 콜 0.

    ## 네 조건

        정배열          `ma_aligned`            5 > 20 > 60
        20일 이격도     |disparity20 - 100| ≤ 5  이평에 붙어 있다
        신고가 근접     `near_high60_pct` ≥ 95   60일 고가의 95% 이상
        거래대금        `trade_value` ≥ 100억

    ## 이 팔은 기본값이 꺼져 있다 — 근거가 무너졌기 때문이다

    **이 함수는 켜기 위해서가 아니라 무엇을 쟀는지 남기려고 있다.**
    `nightly.screen_fill` 기본값은 0 이고, 아래 측정이 그 이유다.

    처음 보고한 값은 "손절 2.5% 기준 -0.135R vs 무작위 -0.304R, 우리가 만든
    랭킹 중 유일하게 무작위를 이겼다" 였다. 그 표본을 다시 열어 보니 **87.4%가
    금리형·머니마켓 ETF** 였다(KODEX CD금리액티브·TIGER CD1년금리액티브·
    KODEX 머니마켓액티브는 195일 **전부** 통과). 매일 0.01%씩 오르는 현금성
    상품이라 항상 신고가·항상 이평 위·항상 정배열이다. 백테스트 하네스는 ETF 를
    유니버스에 넣는데 발굴은 이름으로 제외한다 — 그 차이를 확인하지 않았다.

    ETF 를 빼도 격차는 남았다(-0.094R vs -0.244R · n=701 · 초과 R t=+6.48).
    그래서 한 겹 더 팠다. 통과 상위가 KT&G 50일 · 기업은행 40일 · KT 29일 ·
    우리금융지주 27일 · 신한지주 23일 — **전부 저변동성 대형주**다. 조건식 통과분의
    `atr_pct` 는 3.04% 로 유동성 통과 전체 6.87% 의 절반이 안 된다.

        같은 날 **비슷한 변동성** 종목 중 무작위를 대조군으로 삼으면

            거래대금 100억   -0.094R vs -0.124R   격차 +0.030R  t=+0.70
            거래대금  10억   -0.180R vs -0.182R   격차 +0.001R  t=+0.07

    **0 과 구분되지 않는다.** 이 조건식은 종목을 고른 것이 아니라 **베타를 줄인
    것**이고, 우리 표본 195일이 하락 편중이라 그것이 유리했을 뿐이다. ETF 발견과
    같은 구조가 한 겹 아래에서 반복된 것이다.

    ## 그래서 켜지 않는다 — 이유가 둘이다

    1. 근거가 없다. 무작위보다 낫다는 주장이 변동성 통제로 사라졌다.
    2. 켜도 판정할 표본이 안 나온다. 우리 진입 규칙(ORB·모멘텀)은 변동성을 먹고
       사는데 이 조건식은 저변동성만 고른다. 거래대금 100억 기준 최근 20거래일
       통과는 **총 3건**이었다(0이 17일). 후보로 올려도 체결이 없으면 4주 뒤
       판정할 것이 없다.

    ## 살아남은 사실 하나 — 축별 기여(보통주 기준)

    ETF 를 뺀 뒤 다시 분해하면 종전에 보고한 순서가 뒤집힌다. 종전 값(신고가가
    가장 크고 거래대금은 무시할 만하다)은 ETF 87% 표본에서 나온 것이라 폐기한다.

        네 조건 전부  -0.094R   |  '정배열' 제외  -0.099R (기여 ≈ 0)
        '거래대금' 제외 -0.183R  |  '신고가95' 제외 -0.291R
        '이격도5' 제외 -0.365R (가장 크다)  |  유동성 통과 전체 -0.330R

    다만 위 변동성 통제가 이 순서 전체를 무력화한다 — 세 축 모두 '덜 움직이는
    종목' 을 가리키는 다른 방법이었을 수 있다. 남기는 이유는 결론이 아니라
    **다음에 같은 조건식을 만났을 때 다시 재지 않기 위해서**다.

    ## 되살리려면

    `nightly.screen_fill` 을 0 이 아닌 값으로 두면 그날부터 팔이 돈다. 다만
    되살리기 전에 **변동성 정합 대조군을 이기는가**를 먼저 통과해야 한다 —
    그냥 무작위를 이기는 것으로는 부족하다는 것이 이 절의 결론이다.

    `trade_value` 는 당일 거래대금이고 백테스트는 20일 평균(`tov20`)을 썼다.
    `features.py` 에 20일 평균이 없어 근사한 것이다.
    """
    s = cfg.get("screen") or {}
    if s.get("require_ma_aligned", True) and not f.get("ma_aligned"):
        return False
    # disparity20 은 `close / ma20 * 100` 이다(100 이 이평 위). ma20 을 못 구하면
    # 0 이 들어오므로 |0-100| = 100 으로 자연히 탈락한다.
    if abs(float(f.get("disparity20") or 0) - 100.0) > float(s.get("disparity_max_pct", 5)):
        return False
    if float(f.get("near_high60_pct") or 0) < float(s.get("near_high60_min", 95)):
        return False
    return float(f.get("trade_value") or 0) >= float(
        s.get("min_trade_value_krw", 10_000_000_000))


def _select(scored: list[dict], pool: list[dict], cfg: dict, day: str) -> list[dict]:
    """그날 내보낼 발굴 후보 — 점수 표본 + **조건식 표본** + **무작위 표본**.

    ## 왜 무작위를 섞나

    유입 깔때기 실측 2026-07-31: 상장 3,925 → 유동성 통과 1,165 → `min_score`
    2점 이상 **8종목**. 전종목을 평가하는 유일한 경로가 하루 8건을 내놓고
    있었고, 5거래일 실거래 50종목 중 이 경로 기여는 7종목이었다. 나머지는
    전부 '상위 N' API(거래대금·등락률) 라 매일 같은 종목이 돌아온다 —
    "비슷한 종목이 순환한다" 는 관찰의 실체가 이것이다.

    그런데 **점수 순으로 자리를 늘리는 것은 근거가 없다.** 1.5단계 실측
    (736,291행 / 3,907종목 / 191거래일): 무작위 -0.184R · 매수가능 무작위
    -0.283R · 현행 점수 -0.502R · 차트지표 합성(walk-forward) -0.683R.
    **어느 랭킹도 무작위를 못 이겼고 점수는 적극적으로 해로웠다.**

    ## 변동성을 통제해도 그대로다 — 오히려 더 나빠진다 (2026-08-01)

    위 비교에는 변동성 통제가 없었다. 조건식 팔이 **저변동성이라 유리해 보였던**
    일(`screen_pass` 참조)을 겪은 뒤 같은 잣대를 우리 점수에도 댔다. 같은 날
    **atr_pct 가 가장 가까운** 유동성 통과 종목을 대조군으로 쓴다.

    보통주만(ETF 제외) · 497,773행 / 195일 · 손절 2.5%:

        점수 상위 20    -0.480R  vs 정합 대조군 -0.329R   격차 -0.151R  t= -7.04
        점수 2점 이상   -0.462R  vs 정합 대조군 -0.294R   격차 -0.168R  t=-10.70
        점수 3점(만점)  -0.533R  vs 정합 대조군 -0.295R   격차 -0.238R  t= -5.23

    세 가지가 드러났다.

    1. **점수 표본은 변동성이 오히려 낮다** — atr 5.0~5.8% vs 풀 6.87%. 3규칙이
       움직이는 종목을 고른다는 예상이 틀렸다. 신고가 근접·정배열이 실제로는
       꾸준한 종목 쪽으로 기운다.
    2. 그래서 정합 대조군이 단순 무작위보다 잘 나오고, **격차가 오히려 벌어진다**
       (-0.134 → -0.168). 점수의 해로움은 베타 탓이 아니라 종목 선정 자체다.
    3. **점수가 높을수록 더 나쁘다** — 2점 이상 -0.168R → 3점 만점 -0.238R.
       단조다. 이 점수를 더 신뢰할 근거가 어디에도 없다.

    덧붙여, 1.5단계 하네스는 ETF 를 유니버스에 넣었다. ETF 를 포함하면 '점수 2점
    이상'의 단순 무작위 대비 격차가 +0.007(t=+0.67)로 **무해해 보인다.** 빼면
    -0.135(t=-8.33)다. 그 하네스가 점수의 해로움을 가리고 있었다.

    그래서 자리를 늘리되 늘린 자리는 유동성 통과 유니버스에서 무작위로 채운다.
    측정상 가장 나은 선택법이고, 풀을 최대로 다양하게 만들며, 부수적으로
    **점수 표본 vs 무작위 표본을 같은 게이트 아래 나란히 돌리는 실거래
    대조군**이 된다(`pick_kind`). 1.5단계 결론을 일봉 근사가 아니라 실제
    체결로 다시 묻는 셈이다.

    ## 세 번째 팔 — 조건식 표본 (`screen`) · **기본 꺼짐**

        score   3규칙 상위 N        — 실측상 가장 나빴던 쪽(기준선으로 남긴다)
        screen  문서 조건식          — `screen_fill: 0`, 근거가 무너져 꺼 뒀다
        random  유동성 유니버스 무작위 — 대조군

    조건식 팔은 "백테스트에서 유일하게 무작위를 이겼다" 를 근거로 만들었는데,
    그 우위가 **변동성 통제로 사라졌다**(정합 대조군 대비 +0.030R · t=+0.70).
    자세한 것은 `screen_pass` 의 docstring 이다. 코드는 남기고 기본값만 0 으로
    둔다 — 지운 자리에는 "왜 안 하는가" 가 남지 않아 같은 조건식을 다시 만나면
    다시 재게 된다.

    시드는 날짜에서 만든다 — 같은 날 재실행하면 같은 표본이 나와야 배포·재시작이
    측정을 흔들지 않는다(1.5단계 `random` 재현성 요건과 같은 이유). 두 팔은
    **다른 시드**를 쓴다. 같은 시드를 나눠 쓰면 조건식 팔을 켜고 끄는 것만으로
    무작위 대조군의 구성이 통째로 바뀌어, 되돌렸을 때 원래 표본으로 안 돌아온다.
    """
    n = int(cfg.get("top_n", 20) or 0)
    top = sorted(scored, key=lambda x: (-x["score"], x["code"]))[:n]
    for p in top:
        p["pick_kind"] = "score"
    taken = {p["code"] for p in top}

    # 조건식 팔을 먼저 채운다. 뒤로 미루면 무작위 팔이 먼저 집어간 종목이
    # `random` 으로 이름 붙어, 조건식 통과 여부가 사후에 안 보인다. 한 종목은
    # 한 팔에만 속해야 4주 뒤 팔별 성적이 성립한다.
    sfill = int(cfg.get("screen_fill", 0) or 0)
    if sfill > 0:
        hit = sorted((p for p in pool if p.get("screen") and p["code"] not in taken),
                     key=lambda x: x["code"])
        # 통과분이 상한을 넘으면 그중에서 무작위로 자른다 — 조건식 안에서 다시
        # 줄을 세울 근거가 없다(정렬 기준을 고르는 순간 그게 새 랭킹이 된다).
        if len(hit) > sfill:
            hit = random.Random(f"{day}:screen").sample(hit, sfill)
        for p in hit:
            top.append({**p, "pick_kind": "screen"})
            taken.add(p["code"])

    fill = int(cfg.get("random_fill", 0) or 0)
    if fill <= 0:
        return top
    rest = sorted((p for p in pool if p["code"] not in taken), key=lambda x: x["code"])
    if not rest:
        return top
    rng = random.Random(f"{day}:discovery")
    for p in rng.sample(rest, min(fill, len(rest))):
        top.append({**p, "pick_kind": "random"})
    return top


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def compute_market(rows: list[dict], cfg: dict) -> dict:
    """전종목 피처 → 시장 국면(breadth)·상대강도(RS)·하락 후보. rows 를 제자리 보강.

    각 행에 rs_20(시장 대비 상대강도), bearish_score(0~3) 를 채우고,
    시장 폭(60이평 상회 비율)으로 강세/중립/약세 국면을 판정한다.
    딥리서치의 '추세 게이트가 전제' 를 발굴 단계에 반영."""
    base = [r for r in rows if r.get("liquid") and not r.get("etf_etn")]
    med20 = _median([r.get("ret_20d", 0) for r in base]) if base else 0.0
    for r in rows:
        r["rs_20"] = round(r.get("ret_20d", 0) - med20, 1)
        bs = 0
        if r.get("bearish_align"):
            bs += 1
        if r.get("close") and r.get("ma60") and r["close"] < r["ma60"]:
            bs += 1
        if r.get("near_low60_pct", 999) <= 105:      # 60일 저점 5% 이내
            bs += 1
        r["bearish_score"] = bs
    n = len(base) or 1
    breadth60 = round(100 * sum(r.get("above_ma60", 0) for r in base) / n, 1)
    breadth20 = round(100 * sum(r.get("above_ma20", 0) for r in base) / n, 1)
    rcfg = cfg.get("regime", {})
    bull_th = rcfg.get("bull_breadth", 60)
    bear_th = rcfg.get("bear_breadth", 40)
    regime = "강세" if breadth60 >= bull_th else ("약세" if breadth60 <= bear_th else "중립")
    # 하락(숏) 후보 상위
    bmin = cfg.get("bearish_min_score", 2)
    bear = [r for r in base if r.get("bearish_score", 0) >= bmin]
    bear.sort(key=lambda r: (-r["bearish_score"], r.get("rs_20", 0)))
    bear_top = [{"code": r["code"], "name": r["name"], "close": r["close"],
                 "bearish_score": r["bearish_score"], "rs_20": r["rs_20"],
                 "near_low60_pct": r.get("near_low60_pct")}
                for r in bear[: cfg.get("top_n", 20)]]
    return {
        "regime": regime, "breadth_ma60": breadth60, "breadth_ma20": breadth20,
        "median_ret20": round(med20, 1), "analyzed": len(base),
        "bearish_count": len(bear), "bearish_top": bear_top,
    }


def latest_picks() -> tuple[str | None, list[dict]]:
    """가장 최근 발굴일의 후보 목록 — **인스턴스 없이** DB 만 읽는다.

    `Discovery` 싱글턴은 `main` 에 있어서, 그것을 통해 읽으면 어댑터가 main 을
    임포트하게 되고 순환 참조가 된다. 발굴 엔진 어댑터는 진행률·국면이 아니라
    후보만 필요하므로 여기서 끊는다. `Discovery.latest()` 도 이 함수를 쓴다.
    """
    with _conn() as conn:
        row = conn.execute("SELECT MAX(date) AS d FROM picks").fetchone()
        date = row["d"] if row else None
        if not date:
            return None, []
        picks = [
            dict(r) | {"reasons": json.loads(r["reasons"])}
            for r in conn.execute(
                "SELECT * FROM picks WHERE date=? ORDER BY score DESC, code", (date,))
        ]
    return date, picks


def liquid_universe(limit: int = 0) -> list[str]:
    """가장 최근 발굴일의 **유동성 통과 전종목**, 거래대금 내림차순.

    분봉 커버리지를 넓힐 대상 목록이다. 정렬을 거래대금으로 두는 이유는
    두 가지다 — 실제로 체결 가능한 쪽부터 채우고, 거래대금 순위는 하루 사이
    잘 바뀌지 않아 **날마다 같은 종목이 담긴다**. 대상이 매일 갈리면 패널에
    구멍이 생겨 나중에 아무것도 못 잰다.
    """
    with _conn() as conn:
        row = conn.execute("SELECT MAX(date) AS d FROM chart_obs").fetchone()
        day = row["d"] if row else None
        if not day:
            return []
        rows = conn.execute("SELECT code, feats FROM chart_obs WHERE date=?", (day,))
        pairs = []
        for r in rows:
            try:
                tv = float(json.loads(r["feats"]).get("trade_value") or 0)
            except (TypeError, ValueError, json.JSONDecodeError):
                tv = 0.0
            pairs.append((tv, r["code"]))
    pairs.sort(key=lambda x: (-x[0], x[1]))
    codes = [c for _, c in pairs]
    return codes[:limit] if limit else codes


class Nightly:
    def __init__(self) -> None:
        self.running = False
        self.progress = ""
        self.last_run = ""
        self.market: dict = {}      # 최근 시장 국면·상대강도·하락 후보 요약

    def latest(self) -> dict:
        date, picks = latest_picks()
        market = self.market or (export.latest_manifest() or {}).get("market", {})
        return {"date": date, "picks": picks, "running": self.running,
                "progress": self.progress, "last_run": self.last_run,
                "market": market}

    async def run_once(self) -> int:
        """전종목 수집 + 스크리닝. 반환: 발굴 종목 수."""
        from ..kiwoom.client import client  # 지연 임포트

        if self.running:
            return 0
        self.running = True
        cfg = settings.CONFIG.get("nightly", {})
        try:
            symbols: list[dict] = []
            for mkt in ("0", "10"):  # 0=코스피, 10=코스닥 (실호출 검증)
                try:
                    symbols += parse_stock_list(await client.stock_list(mkt))
                except Exception as e:  # noqa: BLE001
                    log.warning("종목 리스트 조회 실패 (mrkt=%s): %s", mkt, e)
            if symbols:  # 종목 마스터(코드↔명)도 함께 갱신
                from ..data import symbols as symbol_master

                symbol_master.upsert(symbols)
            limit = cfg.get("max_symbols", 0)
            if limit:
                symbols = symbols[:limit]
            if not symbols:
                self.progress = "종목 리스트 조회 실패 — ka10099 요청 필드 검증 필요"
                return 0

            feature_rows: list[dict] = []   # 전종목 피처 (파일 내보내기용)
            scored: list[dict] = []         # 발굴 후보 (유동성 게이트 통과 + 점수)
            pool: list[dict] = []           # 유동성 통과 전량 — 무작위 표본의 모집단
            obs: list[tuple] = []           # 차트 지표 원장 (전종목 베이스)
            # 수급(ka10086) — 종목당 콜이 하나 더 붙는다. 설정으로 끈다.
            fcfg = settings.CONFIG.get("scout", {}).get("observe", {}).get("flows", {})
            flow_on = bool(fcfg.get("universe", True))
            flow_days = int(fcfg.get("universe_days", 3))
            flow_fail = 0
            for i, s in enumerate(symbols):
                if i % 100 == 0:
                    self.progress = f"수집 중 {i}/{len(symbols)}"
                try:
                    df = parse_chart_response(await client.daily_chart(s["code"]))
                except Exception:  # noqa: BLE001 - 개별 실패는 건너뜀
                    continue
                if df.empty:
                    continue
                store.upsert_bars(s["code"], "1d", df.tail(250))  # 약 1년치 (MA120·기간뷰용)
                f = compute_features(df, cfg)
                if f is None:
                    continue
                etf = is_excluded(s["name"], cfg)
                # CSV 에는 전종목 유지하되 etf_etn 플래그를 실어 스케줄러도 걸러낼 수 있게 함
                feature_rows.append(
                    {"code": s["code"], "name": s["name"], **f, "etf_etn": int(etf)}
                )
                # 발굴 후보(카드·자동편입)에서는 ETF/ETN/리츠/채권형 제외
                if etf or not f["liquid"]:
                    continue
                cand = {"code": s["code"], "name": s["name"],
                        "close": f["close"], "score": f["score"],
                        "reasons": f["reasons"],
                        # 조건식 통과 여부는 여기서만 계산한다 — 피처가 손에
                        # 있는 유일한 지점이고, `_select` 는 pool 만 본다.
                        "screen": int(screen_pass(f, cfg))}
                pool.append(cand)
                # 수급 — `ka10086` 은 OHLCV 에 개인·기관·외국인 순매수 +
                # 프로그램매매 + 외국인 지분율 + 신용비율을 얹어 준다. 일봉
                # (`ka10081`)이 못 주는 축이라 **한 콜을 더 쓴다**(전종목이면
                # 배치가 약 16분 → 33분). 유동성 통과분에만 건다 — 1주도 못 사는
                # 종목의 수급은 4주 뒤 질문에 쓰이지 않는다.
                if flow_on:
                    try:
                        rows = kflows.parse_daily_price(
                            await client.daily_price(s["code"]))
                        scout_store.record_flows(s["code"], rows[:flow_days],
                                                 s["name"])
                    except Exception:  # noqa: BLE001 - 관측이 발굴을 막지 않는다
                        flow_fail += 1
                obs.append((s["code"], s["name"], f["score"],
                            json.dumps({k: v for k, v in f.items() if k != "reasons"},
                                       ensure_ascii=False)))
                if f["score"] >= cfg.get("min_score", 2):
                    scored.append(cand)
            today = datetime.now(KST).date().isoformat()
            top = _select(scored, pool, cfg, today)
            # 시장 국면(breadth) + 상대강도(RS) + 하락(숏) 후보 점수 산출
            market = compute_market(feature_rows, cfg)
            self.market = market
            # 전종목 피처를 파일로 내보내 외부 스케줄러/분석기가 소비하게 한다
            if settings.CONFIG.get("export", {}).get("enabled", True) and feature_rows:
                try:
                    export.write_dataset(today, feature_rows, market=market)
                except Exception:  # noqa: BLE001 - 내보내기 실패는 발굴 자체를 막지 않음
                    log.exception("데이터셋 내보내기 실패")
            with _conn() as conn:
                conn.execute("DELETE FROM picks WHERE date=?", (today,))
                conn.executemany(
                    "INSERT OR REPLACE INTO picks "
                    "(date, code, name, close, score, reasons, pick_kind) "
                    "VALUES (?,?,?,?,?,?,?)",
                    [(today, p["code"], p["name"], p["close"], p["score"],
                      json.dumps(p["reasons"], ensure_ascii=False),
                      p.get("pick_kind", "score")) for p in top],
                )
                # 차트 지표 원장은 후보 선정과 무관하게 **유동성 통과 전량**을 남긴다.
                conn.executemany(
                    "INSERT OR REPLACE INTO chart_obs VALUES (?,?,?,?,?)",
                    [(today, *o) for o in obs],
                )
            # 감시목록에 쓰지 않는다 — 편입은 엔진(scout)의 단일 통로다.
            # NightlySource 가 30분마다 picks 를 읽어 신호로 올린다. 종전
            # apply_auto_watch(top[:5]) 직접 편입은 2026-08-01 완전 통합에서
            # 회수됐다. 수집·피처·국면은 그대로다 — 엔진의 입력이다.
            kinds = Counter(p.get("pick_kind", "score") for p in top)
            self.progress = (f"완료: {len(symbols)}종목 분석 → 유동성 {len(pool)} → "
                             f"후보 {len(top)} (점수 {kinds['score']} · 조건식 "
                             f"{kinds['screen']} · 무작위 {kinds['random']}) · "
                             f"조건식 통과 {sum(p.get('screen', 0) for p in pool)} · "
                             f"지표 원장 {len(obs)}행"
                             + (f" · 수급 실패 {flow_fail}" if flow_fail else ""))
            self.last_run = datetime.now(KST).isoformat(timespec="seconds")
            log.info("야간 발굴 %s", self.progress)
            return len(top)
        finally:
            self.running = False

    async def loop(self) -> None:
        """평일 17:30 KST 에 1회 실행. 재시작해도 오늘 결과가 DB 에 있으면 건너뛴다."""
        done_for: str = ""
        while True:
            try:
                now = datetime.now(KST)
                today = now.date().isoformat()
                if (
                    settings.KIWOOM_APP_KEY
                    and now.weekday() < 5
                    and now.strftime("%H:%M") >= "17:30"
                    and done_for != today
                    and settings.CONFIG.get("nightly", {}).get("enabled", True)
                ):
                    if self.latest().get("date") == today:
                        done_for = today  # 이미 오늘 실행됨 (재시작 후 중복 방지)
                        continue
                    await self.run_once()
                    done_for = today
            except Exception:  # noqa: BLE001
                log.exception("야간 발굴 오류")
            await asyncio.sleep(300)
