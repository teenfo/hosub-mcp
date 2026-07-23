"""야간 종목 발굴 — 전일자 전종목 일봉을 수집·분석해 익일 후보를 추린다.

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
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from . import export, settings
from .data import store
from .data.collector import parse_chart_response
from .features import compute_features

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


# ETF·ETN·리츠·스팩·채권형 등 '일반 보통주가 아닌' 종목 판별용 기본값.
# config.yaml discovery.exclude_keywords / suffixes / prefixes 로 덮어쓸 수 있다.
# 주의: '리츠'는 부분일치로 두면 '메리츠금융지주'가 오탐되므로 접미사로만 본다.
_DEFAULT_EXCLUDE_KEYWORDS = [
    "스팩", "ETN", "ETF", "레버리지", "인버스", "선물", "채권", "국채",
    "금리", "액티브", "커버드콜",
]
_DEFAULT_EXCLUDE_SUFFIXES = ["리츠"]
_DEFAULT_EXCLUDE_PREFIXES = [
    "KODEX", "TIGER", "KOSEF", "ARIRANG", "HANARO", "TIMEFOLIO", "KOACT",
    "TREX", "PLUS", "RISE", "ACE", "SOL", "KBSTAR", "히어로즈", "마이다스",
]


def is_excluded(name: str, cfg: dict) -> bool:
    """ETF/ETN/리츠/채권형 등 발굴 대상에서 뺄 종목이면 True."""
    n = (name or "").upper().replace(" ", "")
    for kw in cfg.get("exclude_keywords", _DEFAULT_EXCLUDE_KEYWORDS):
        if kw.upper() in n:
            return True
    for sf in cfg.get("exclude_suffixes", _DEFAULT_EXCLUDE_SUFFIXES):
        if n.endswith(sf.upper()):
            return True
    for pf in cfg.get("exclude_prefixes", _DEFAULT_EXCLUDE_PREFIXES):
        if n.startswith(pf.upper()):
            return True
    return False


def screen_daily(df: pd.DataFrame, cfg: dict) -> tuple[float, list[str]]:
    """일봉 → (점수, 사유). 유동성 게이트 미통과·60행 미만이면 (0, [])."""
    f = compute_features(df, cfg)
    if f is None or not f["liquid"]:
        return 0.0, []
    return f["score"], f["reasons"]


class Discovery:
    def __init__(self) -> None:
        self.running = False
        self.progress = ""
        self.last_run = ""

    def latest(self) -> dict:
        with _conn() as conn:
            row = conn.execute("SELECT MAX(date) AS d FROM picks").fetchone()
            date = row["d"] if row else None
            picks = []
            if date:
                picks = [
                    dict(r) | {"reasons": json.loads(r["reasons"])}
                    for r in conn.execute(
                        "SELECT * FROM picks WHERE date=? ORDER BY score DESC, code",
                        (date,),
                    )
                ]
        return {"date": date, "picks": picks, "running": self.running,
                "progress": self.progress, "last_run": self.last_run}

    async def run_once(self) -> int:
        """전종목 수집 + 스크리닝. 반환: 발굴 종목 수."""
        from .kiwoom.client import client  # 지연 임포트

        if self.running:
            return 0
        self.running = True
        cfg = settings.CONFIG.get("discovery", {})
        try:
            symbols: list[dict] = []
            for mkt in ("0", "10"):  # 0=코스피, 10=코스닥 (실호출 검증)
                try:
                    symbols += parse_stock_list(await client.stock_list(mkt))
                except Exception as e:  # noqa: BLE001
                    log.warning("종목 리스트 조회 실패 (mrkt=%s): %s", mkt, e)
            if symbols:  # 종목 마스터(코드↔명)도 함께 갱신
                from .data import symbols as symbol_master

                symbol_master.upsert(symbols)
            limit = cfg.get("max_symbols", 0)
            if limit:
                symbols = symbols[:limit]
            if not symbols:
                self.progress = "종목 리스트 조회 실패 — ka10099 요청 필드 검증 필요"
                return 0

            feature_rows: list[dict] = []   # 전종목 피처 (파일 내보내기용)
            scored: list[dict] = []         # 발굴 후보 (유동성 게이트 통과 + 점수)
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
                if not etf and f["liquid"] and f["score"] >= cfg.get("min_score", 2):
                    scored.append(
                        {"code": s["code"], "name": s["name"],
                         "close": f["close"], "score": f["score"],
                         "reasons": f["reasons"]}
                    )
            scored.sort(key=lambda x: -x["score"])
            top = scored[: cfg.get("top_n", 20)]
            today = datetime.now(KST).date().isoformat()
            # 전종목 피처를 파일로 내보내 외부 스케줄러/분석기가 소비하게 한다
            if settings.CONFIG.get("export", {}).get("enabled", True) and feature_rows:
                try:
                    export.write_dataset(today, feature_rows)
                except Exception:  # noqa: BLE001 - 내보내기 실패는 발굴 자체를 막지 않음
                    log.exception("데이터셋 내보내기 실패")
            with _conn() as conn:
                conn.execute("DELETE FROM picks WHERE date=?", (today,))
                conn.executemany(
                    "INSERT OR REPLACE INTO picks VALUES (?,?,?,?,?,?)",
                    [(today, p["code"], p["name"], p["close"], p["score"],
                      json.dumps(p["reasons"], ensure_ascii=False)) for p in top],
                )
            # 발굴 상위 종목 자동 감시 편입 (이전 auto 항목은 교체)
            if cfg.get("auto_watch", True) and top:
                from .data import watchlist

                watchlist.replace_auto(top[: cfg.get("auto_watch_n", 5)])
                await watchlist.notify()
            self.progress = f"완료: {len(symbols)}종목 분석 → {len(top)}종목 발굴"
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
                    and settings.CONFIG.get("discovery", {}).get("enabled", True)
                ):
                    if self.latest().get("date") == today:
                        done_for = today  # 이미 오늘 실행됨 (재시작 후 중복 방지)
                        continue
                    await self.run_once()
                    done_for = today
            except Exception:  # noqa: BLE001
                log.exception("야간 발굴 오류")
            await asyncio.sleep(300)
