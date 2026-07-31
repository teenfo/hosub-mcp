"""SQLite 시세·주문 저장소. 홈서버에서는 DATA_DIR=/data/trading 권장."""
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .. import settings

DB_PATH = Path(settings.DATA_DIR) / "market.db"
KST = ZoneInfo("Asia/Seoul")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS bars (
            symbol TEXT NOT NULL,
            tf TEXT NOT NULL,          -- '1m', '1d'
            ts TEXT NOT NULL,          -- ISO8601 (KST)
            open REAL, high REAL, low REAL, close REAL, volume INTEGER,
            PRIMARY KEY (symbol, tf, ts)
        )"""
    )
    # 심층 백필(연속조회) 완료 표시 — 종목당 1회만 과거를 끌어오기 위한 기록.
    # 감시목록에서 빠졌다 재편입돼도 다시 받지 않도록 watchlist 와 분리해 둔다.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS deep_backfill (
            symbol TEXT PRIMARY KEY, done TEXT, bars INTEGER
        )"""
    )
    # 0봉으로 끝난 시도를 세는 칸. 아래 mark_deep_backfill 주석 참조.
    if "attempts" not in {r[1] for r in conn.execute(
            "PRAGMA table_info(deep_backfill)")}:
        conn.execute("ALTER TABLE deep_backfill ADD COLUMN attempts INTEGER DEFAULT 0")
    return conn


def upsert_bars(symbol: str, tf: str, df: pd.DataFrame) -> int:
    """df: index=ts(datetime), columns=open/high/low/close/volume."""
    if df.empty:
        return 0
    rows = [
        (symbol, tf, ts.isoformat(), float(r.open), float(r.high), float(r.low),
         float(r.close), int(r.volume))
        for ts, r in df.iterrows()
    ]
    with _conn() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?)", rows
        )
    return len(rows)


def minute_symbols(min_days: int = 1) -> list[tuple[str, int]]:
    """분봉이 min_days 일 이상 축적된 종목 목록 → [(symbol, 축적일수)].
    ts 는 ISO 문자열이라 날짜는 substr(1,10)='YYYY-MM-DD' 로 센다."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT symbol, COUNT(DISTINCT substr(ts,1,10)) d FROM bars "
            "WHERE tf='1m' GROUP BY symbol HAVING d>=? ORDER BY d DESC, symbol",
            (min_days,),
        ).fetchall()
    return [(r[0], int(r[1])) for r in rows]


def prune_minutes(keep_days: int) -> int:
    """가장 최근 분봉 날짜 기준 keep_days 를 넘긴 오래된 분봉을 삭제(디스크 방어)."""
    if keep_days <= 0:
        return 0
    from datetime import date, timedelta

    with _conn() as conn:
        row = conn.execute(
            "SELECT MAX(substr(ts,1,10)) FROM bars WHERE tf='1m'"
        ).fetchone()
        if not row or not row[0]:
            return 0
        y, m, d = (int(x) for x in row[0].split("-"))
        cutoff = (date(y, m, d) - timedelta(days=keep_days)).isoformat()
        cur = conn.execute(
            "DELETE FROM bars WHERE tf='1m' AND substr(ts,1,10) < ?", (cutoff,)
        )
        return cur.rowcount or 0


# 0봉으로 끝난 시도를 몇 번까지 재시도할지. 상장폐지·거래정지 종목은 몇 번을
# 불러도 0봉이므로 무한 재시도를 막아야 하고, 일시적 빈 응답은 살려야 한다.
DEEP_MAX_ATTEMPTS = 3


def job_done(job: str, day: str) -> bool:
    """그 배치가 그날 이미 끝났는가 — **프로세스가 아니라 디스크에** 묻는다."""
    with _conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS job_marks ("
                     " job TEXT NOT NULL, d TEXT NOT NULL, done_at TEXT,"
                     " PRIMARY KEY (job, d))")
        return conn.execute("SELECT 1 FROM job_marks WHERE job=? AND d=?",
                            (job, day)).fetchone() is not None


def mark_job_done(job: str, day: str, now: str | None = None) -> None:
    """하루 1회 배치의 완료 표시.

    종전에는 루프의 지역변수(`done_for`)로만 들고 있었다. **재시작하면
    초기화된다** — 실측 2026-07-31: 배포를 5번 하니 마감 백필이 5번 돌아
    398종목 × 5 ≈ 2,000콜을 버렸다. 장중이 아니라 피해는 없었지만, 배포가
    잦은 날 조용히 예산을 먹는다.

    야간 발굴은 이 문제가 없다 — 그날 `picks` 가 DB 에 있으면 건너뛰기
    때문이다. 결과가 DB 에 남지 않는 배치(마감 백필·수급)에 같은 성질을 준다.
    """
    with _conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS job_marks ("
                     " job TEXT NOT NULL, d TEXT NOT NULL, done_at TEXT,"
                     " PRIMARY KEY (job, d))")
        conn.execute("INSERT OR REPLACE INTO job_marks (job, d, done_at)"
                     " VALUES (?,?,?)",
                     (job, day, now or datetime.now(KST).isoformat(timespec="seconds")))


def deep_backfilled() -> set[str]:
    """연속조회 심층 백필이 **끝난** 종목 — 봉을 받았거나 재시도를 포기한 것.

    0봉 기록은 '완료' 가 아니다. 그렇게 두면 일시적 빈 응답이 영구 결손이 된다
    (실측 2026-07-27: 동서·JB금융지주가 마감 백필 185종목 직후 0봉으로 기록돼
    다시는 대상에 오르지 않았다. 둘 다 정상 거래 종목이다).
    """
    with _conn() as conn:
        return {r[0] for r in conn.execute(
            "SELECT symbol FROM deep_backfill "
            "WHERE bars > 0 OR COALESCE(attempts, 0) >= ?", (DEEP_MAX_ATTEMPTS,))}


def mark_deep_backfill(symbol: str, bars: int) -> None:
    """심층 백필 결과 기록.

    봉을 받았으면 그대로 완료다. **0봉이면 완료가 아니라 시도 1회**로 센다 —
    분봉 API 는 실패를 200 + 빈 응답으로 돌려주는 일이 있고, 그걸 "데이터가
    없다" 로 읽으면 조용히 과거가 비어 버린다. `DEEP_MAX_ATTEMPTS` 번 연속
    0봉이면 그때 포기한다(상장폐지·거래정지 종목의 재시도 폭주 방어).
    """
    with _conn() as conn:
        conn.execute(
            "INSERT INTO deep_backfill (symbol, done, bars, attempts)"
            " VALUES (?,?,?,1) ON CONFLICT(symbol) DO UPDATE SET"
            " done=excluded.done, bars=excluded.bars,"
            " attempts=COALESCE(deep_backfill.attempts, 0) + 1",
            (symbol, datetime.now(UTC).isoformat(timespec="seconds"), int(bars)),
        )


def load_bars(symbol: str, tf: str = "1m", limit: int = 2000) -> pd.DataFrame:
    with _conn() as conn:
        df = pd.read_sql_query(
            "SELECT ts, open, high, low, close, volume FROM bars "
            "WHERE symbol=? AND tf=? ORDER BY ts DESC LIMIT ?",
            conn,
            params=(symbol, tf, limit),
        )
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"])
    return df.set_index("ts").sort_index()


def market_returns(since: str = "") -> dict[str, float]:
    """날짜별 시장 수익률(%) — 전 종목 일간 등락률의 **횡단면 평균**.

    지수를 따로 받지 않는 이유는 우리가 매매하는 모집단이 지수가 아니기
    때문이다. 국면 판정이 맞았는지는 '코스피가 올랐나' 가 아니라 '우리가 보는
    종목들이 올랐나' 로 재는 것이 정직하다(research/eventstudy.market_daily 와
    같은 정의).

    since: 'YYYY-MM-DD' 이후만. 전 구간 스캔을 피하려고 호출자가 좁힌다.
    """
    sql = (
        "SELECT d, AVG(ret) AS m FROM ("
        "  SELECT date(ts) AS d,"
        "         (close / LAG(close) OVER (PARTITION BY symbol ORDER BY ts) - 1)"
        "         * 100 AS ret"
        "    FROM bars WHERE tf='1d'"
        ") WHERE ret IS NOT NULL"
    )
    params: tuple = ()
    if since:
        sql += " AND d >= ?"
        params = (since,)
    sql += " GROUP BY d"
    with _conn() as conn:
        return {r[0]: float(r[1]) for r in conn.execute(sql, params)}
