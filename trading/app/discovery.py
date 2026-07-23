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

from . import settings
from .data import store
from .data.collector import parse_chart_response

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
    """ka10099 응답에서 종목 배열을 generic 탐색으로 추출."""
    items = None
    for v in raw.values():
        if isinstance(v, list) and v and isinstance(v[0], dict) and "stk_cd" in v[0]:
            items = v
            break
    out = []
    for it in items or []:
        code = str(it.get("stk_cd", "")).lstrip("A_")
        if code.isdigit() and len(code) == 6:
            out.append({"code": code, "name": it.get("stk_nm") or it.get("list_nm", "")})
    return out


def screen_daily(df: pd.DataFrame, cfg: dict) -> tuple[float, list[str]]:
    """일봉 DataFrame(오름차순, 최소 60행) → (점수, 사유 목록). 규칙별 1점."""
    if len(df) < 60:
        return 0.0, []
    last = df.iloc[-1]
    reasons: list[str] = []
    # 유동성/가격 필터
    if last.close < cfg.get("min_price", 1_000):
        return 0.0, []
    if last.close * last.volume < cfg.get("min_trade_value_krw", 1_000_000_000):
        return 0.0, []
    # 1) 거래량 급증
    avg20 = df["volume"].iloc[-21:-1].mean()
    if avg20 > 0 and last.volume >= cfg.get("vol_surge_ratio", 3.0) * avg20:
        reasons.append(f"거래량 20일평균 {last.volume / avg20:.1f}배")
    # 2) 신고가 근접
    high60 = df["high"].iloc[-60:].max()
    if high60 > 0 and last.close >= high60 * cfg.get("near_high_ratio", 0.97):
        reasons.append(f"60일 고가({high60:,.0f}) 대비 {last.close / high60 * 100:.0f}%")
    # 3) 정배열 신규 형성 (최근 5일 내)
    c = df["close"]
    ma5, ma20, ma60 = c.rolling(5).mean(), c.rolling(20).mean(), c.rolling(60).mean()
    aligned = (ma5 > ma20) & (ma20 > ma60)
    if bool(aligned.iloc[-1]) and not bool(aligned.iloc[-6:-1].all()):
        reasons.append("이평 정배열 신규 형성")
    return float(len(reasons)), reasons


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
            for market in ("0", "10"):  # 코스피, 코스닥
                try:
                    symbols += parse_stock_list(await client.stock_list(market))
                except Exception as e:  # noqa: BLE001
                    log.warning("종목 리스트 조회 실패 (market=%s): %s", market, e)
            limit = cfg.get("max_symbols", 0)
            if limit:
                symbols = symbols[:limit]
            if not symbols:
                self.progress = "종목 리스트 조회 실패 — ka10099 요청 필드 검증 필요"
                return 0

            scored: list[dict] = []
            for i, s in enumerate(symbols):
                if i % 100 == 0:
                    self.progress = f"수집 중 {i}/{len(symbols)}"
                try:
                    df = parse_chart_response(await client.daily_chart(s["code"]))
                except Exception:  # noqa: BLE001 - 개별 실패는 건너뜀
                    continue
                if df.empty:
                    continue
                store.upsert_bars(s["code"], "1d", df.tail(80))
                score, reasons = screen_daily(df, cfg)
                if score >= cfg.get("min_score", 2):
                    scored.append(
                        {"code": s["code"], "name": s["name"],
                         "close": int(df.iloc[-1].close), "score": score,
                         "reasons": reasons}
                    )
            scored.sort(key=lambda x: -x["score"])
            top = scored[: cfg.get("top_n", 20)]
            today = datetime.now(KST).date().isoformat()
            with _conn() as conn:
                conn.execute("DELETE FROM picks WHERE date=?", (today,))
                conn.executemany(
                    "INSERT OR REPLACE INTO picks VALUES (?,?,?,?,?,?)",
                    [(today, p["code"], p["name"], p["close"], p["score"],
                      json.dumps(p["reasons"], ensure_ascii=False)) for p in top],
                )
            self.progress = f"완료: {len(symbols)}종목 분석 → {len(top)}종목 발굴"
            self.last_run = datetime.now(KST).isoformat(timespec="seconds")
            log.info("야간 발굴 %s", self.progress)
            return len(top)
        finally:
            self.running = False

    async def loop(self) -> None:
        """평일 17:30 KST 에 1회 실행."""
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
                    await self.run_once()
                    done_for = today
            except Exception:  # noqa: BLE001
                log.exception("야간 발굴 오류")
            await asyncio.sleep(300)
