"""신호 원장 (SQLite) — `signals` / `decisions` / `source_health`.

감시목록과 **분리된 별도 DB** 다. 이유는 역할이 다르기 때문이다.

  signals   원장  — 어느 소스가 언제 무엇을 지목했는가. 덮어쓰지 않고 쌓는다.
  watchlist 투영 — 그 원장을 읽어 "지금 무엇을 보고 있는가" 를 만든 결과.

지금은 이 둘이 같은 표다. 그래서 `replace_scanned` 가 이미 감시 중인 코드를
건너뛰는 순간(watchlist.py:167) 소스 귀속이 첫 소스로 굳어 버리고, 소스별
기여도 측정이 원리적으로 불가능해진다. 원장을 떼어 내야 "거래대금 상위가
가리킨 종목의 성적" 을 나중에 물을 수 있다.

`decisions` 는 더 중요하다. **장중 소스(거래대금·등락률)는 지금까지 한 번도
측정된 적이 없다** — 무엇을 골랐는지 남은 기록이 없기 때문이다. 매 승격·강등을
사유와 함께 남기면 그 측정이 처음으로 가능해진다.
"""
import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import settings
from .model import TTL_SEC, Signal

KST = ZoneInfo("Asia/Seoul")   # 관측을 **거래일** 단위로 접기 위한 기준

log = logging.getLogger(__name__)
DB_PATH = Path(settings.DATA_DIR) / "scout.db"

# live() 의 1차 스캔 범위. model 에서 유도해 값이 따로 놀지 않게 한다.
TTL_MAX = max([*TTL_SEC.values(), 60])

# 원장 보존 기간. 사후 측정(소스별 IC)에는 수십 거래일이면 충분하고,
# 장중 소스가 60초마다 쌓이므로 무한 보존은 디스크만 먹는다.
RETAIN_DAYS = 60


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL, name TEXT, source TEXT NOT NULL, kind TEXT,
            strength REAL NOT NULL, raw REAL, price REAL,
            evidence TEXT, observed_at TEXT NOT NULL, ttl_sec INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_signals_live ON signals (observed_at, code);
        CREATE INDEX IF NOT EXISTS ix_signals_source ON signals (source, observed_at);

        CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
            action TEXT NOT NULL,          -- promote_collect|promote_trade|demote|drop|hold
            from_tier TEXT, to_tier TEXT,
            score REAL, sources TEXT,      -- 기여 소스 목록(JSON) — 사후 귀속의 입력
            reason TEXT,
            mode TEXT NOT NULL,            -- shadow|collect|full — 실제로 적용됐는지
            applied INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS ix_decisions_ts ON decisions (ts);
        CREATE INDEX IF NOT EXISTS ix_decisions_code ON decisions (code, ts);
        """
    )
    # 결정 시점의 실측 등락률·체결강도·호가 스프레드. **판단에 쓰지 않고 기록만
    # 한다** — 4주 뒤 "체결강도가 실제로 성적과 상관있나", "스프레드 넓은 종목의
    # 성적이 실제로 나쁜가" 를 묻기 위한 재료다. 측정 전에 승격 조건에 넣으면
    # 발굴 3규칙 점수가 저지른 일을 반복한다.
    have = {r[1] for r in conn.execute("PRAGMA table_info(decisions)")}
    for col in ("change_pct", "cntr_str", "spread_pct"):
        if col not in have:
            conn.execute(f"ALTER TABLE decisions ADD COLUMN {col} REAL")
    conn.executescript(
        """

        CREATE TABLE IF NOT EXISTS source_health (
            source TEXT PRIMARY KEY,
            last_ok TEXT, last_error TEXT, error_msg TEXT,
            fails INTEGER NOT NULL DEFAULT 0,
            signals INTEGER NOT NULL DEFAULT 0
        );

        -- 시세 관측 — **종목·날짜당 한 행**으로 접어 쌓는다.
        --
        -- `decisions` 에만 실으면 승격 판정이 일어난 종목만 남는다. 실측
        -- 2026-07-31: `promote_trade` 결정이 7/29 이후 0건이라(매매 tier 포화)
        -- 스프레드가 하나도 안 쌓였다. 관측은 판정과 독립이어야 한다.
        --
        -- `refresh_quotes` 가 이미 수집 tier 후보 전체(실측 92종목)를 1콜로
        -- 받아 오므로 **추가 호출이 0** 이다. 매 사이클 원행으로 쌓으면 하루
        -- 7만 행이라 종목·날짜로 접는다 — 우리가 물을 질문("이 종목의 스프레드가
        -- 넓은가")에는 그 해상도면 충분하고, 장중 변동은 min/max 로 남는다.
        CREATE TABLE IF NOT EXISTS quote_obs (
            d TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
            n INTEGER NOT NULL DEFAULT 0,
            spread_n INTEGER NOT NULL DEFAULT 0,
            spread_sum REAL NOT NULL DEFAULT 0,
            spread_min REAL, spread_max REAL, spread_last REAL,
            cntr_str_last REAL, price_last REAL, trde_prica_last INTEGER,
            updated_at TEXT,
            PRIMARY KEY (d, code)
        );
        CREATE INDEX IF NOT EXISTS ix_quote_obs_code ON quote_obs (code, d);
        """
    )
    # 연속 0건 폴 횟수. 조회 성공만 보던 종전 규약은 "정상적으로 아무것도 못 찾는"
    # 상태를 침묵으로 넘겼다(presurge 7/28~30, `fails: 0` 인 채 신호 0건).
    have = {r[1] for r in conn.execute("PRAGMA table_info(source_health)")}
    if "empty" not in have:
        conn.execute(
            "ALTER TABLE source_health ADD COLUMN empty INTEGER NOT NULL DEFAULT 0")
    return conn


# --- 시세 관측 ---------------------------------------------------------------

def record_quotes(quotes: dict[str, dict], names: dict[str, str] | None = None,
                  now: datetime | None = None) -> int:
    """실측 시세를 종목·날짜당 한 행으로 접어 쌓는다. 반환: 반영한 종목 수.

    **판단에 쓰지 않는다.** 체결강도와 같은 규약이다 — 2주 뒤 "스프레드가 넓은
    종목의 성적이 실제로 나쁜가" 를 물을 수 있게 하는 것이 목적이고, 문턱을 지금
    손으로 정하면 1.5단계에서 폐기한 실수를 반복한다.

    스프레드는 낼 수 없는 순간이 있다(장 전·거래정지·데이터 결손). 그때 0 으로
    세면 평균이 아래로 끌려가므로 **스프레드 표본만 따로 센다**(`spread_n`).
    """
    if not quotes:
        return 0
    names = names or {}
    ts = (now or datetime.now(UTC))
    day = ts.astimezone(KST).strftime("%Y-%m-%d")
    rows = []
    for code, q in quotes.items():
        sp = q.get("spread_pct")
        rows.append((day, code, names.get(code),
                     1 if sp is None else 0,          # n 은 항상 +1, 아래에서 처리
                     sp, q.get("cntr_str"), q.get("price"), q.get("trde_prica"),
                     ts.isoformat()))
    with _conn() as conn:
        conn.executemany(
            "INSERT INTO quote_obs (d, code, name, n, spread_n, spread_sum,"
            " spread_min, spread_max, spread_last, cntr_str_last, price_last,"
            " trde_prica_last, updated_at)"
            " VALUES (?,?,?,1,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(d, code) DO UPDATE SET"
            "   n = quote_obs.n + 1,"
            "   name = COALESCE(excluded.name, quote_obs.name),"
            "   spread_n = quote_obs.spread_n + excluded.spread_n,"
            "   spread_sum = quote_obs.spread_sum + excluded.spread_sum,"
            "   spread_min = CASE WHEN excluded.spread_last IS NULL"
            "        THEN quote_obs.spread_min"
            "        ELSE MIN(COALESCE(quote_obs.spread_min, excluded.spread_last),"
            "                 excluded.spread_last) END,"
            "   spread_max = CASE WHEN excluded.spread_last IS NULL"
            "        THEN quote_obs.spread_max"
            "        ELSE MAX(COALESCE(quote_obs.spread_max, excluded.spread_last),"
            "                 excluded.spread_last) END,"
            "   spread_last = COALESCE(excluded.spread_last, quote_obs.spread_last),"
            "   cntr_str_last = COALESCE(excluded.cntr_str_last, quote_obs.cntr_str_last),"
            "   price_last = COALESCE(excluded.price_last, quote_obs.price_last),"
            "   trde_prica_last = COALESCE(excluded.trde_prica_last,"
            "                              quote_obs.trde_prica_last),"
            "   updated_at = excluded.updated_at",
            [(d, c, nm, 0 if sp is None else 1, 0.0 if sp is None else sp,
              sp, sp, sp, cs, pr, tp, at)
             for (d, c, nm, _null, sp, cs, pr, tp, at) in rows],
        )
    return len(rows)


def quote_stats(day: str | None = None, limit: int = 500) -> list[dict]:
    """종목별 스프레드 관측 요약. 화면·분석용 읽기 전용."""
    with _conn() as conn:
        sql = ("SELECT *, CASE WHEN spread_n > 0 THEN spread_sum / spread_n END"
               " AS spread_avg FROM quote_obs")
        args: tuple = ()
        if day:
            sql += " WHERE d = ?"
            args = (day,)
        sql += " ORDER BY spread_avg DESC NULLS LAST LIMIT ?"
        return [dict(r) for r in conn.execute(sql, (*args, limit))]


# --- 신호 적재 ---------------------------------------------------------------

def record(signals: list[Signal]) -> int:
    """지목을 원장에 쌓는다. **덮어쓰지 않는다** — 같은 종목을 두 소스가
    가리키면 두 행이 남아야 나중에 둘 다 셀 수 있다."""
    if not signals:
        return 0
    with _conn() as conn:
        conn.executemany(
            "INSERT INTO signals (code, name, source, kind, strength, raw, price,"
            " evidence, observed_at, ttl_sec) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(s.code, s.name, s.source, s.kind, s.strength, s.raw, s.price,
              json.dumps(s.evidence, ensure_ascii=False),
              s.observed_at.isoformat(), s.ttl_sec) for s in signals],
        )
    return len(signals)


def live(now: datetime | None = None) -> list[Signal]:
    """아직 만료되지 않은 신호. 종목당 소스별 **가장 최근 것 하나만**.

    같은 소스가 60초마다 같은 종목을 보고하므로 전부 세면 그 소스만 점수가
    부푼다. 원장에는 다 남기되(사후 측정용) 현재 점수 계산에는 최신만 쓴다.

    SQL 의 시각 조건은 스캔 범위를 줄이는 1차 필터일 뿐이다. **정확한 만료
    판정은 Signal.alive() 가 한다** — `ttl_sec <= 0`(수동 지정)은 만료가 없으므로
    아무리 오래돼도 1차 필터에서 빠지면 안 된다.
    """
    now = now or datetime.now(UTC)
    floor = (now - timedelta(seconds=TTL_MAX)).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE id IN ("
            "  SELECT MAX(id) FROM signals WHERE observed_at >= ? OR ttl_sec <= 0"
            "  GROUP BY code, source)",
            (floor,),
        ).fetchall()
    out = []
    for r in rows:
        s = Signal(
            code=r["code"], name=r["name"] or r["code"], source=r["source"],
            kind=r["kind"] or "", strength=r["strength"], raw=r["raw"] or 0.0,
            price=r["price"] or 0.0, evidence=_loads(r["evidence"]),
            observed_at=datetime.fromisoformat(r["observed_at"]),
            ttl_sec=r["ttl_sec"],
        )
        if s.alive(now):
            out.append(s)
    return out


def _loads(raw) -> dict:
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def purge(days: int = RETAIN_DAYS, now: datetime | None = None) -> int:
    """오래된 원장 정리. manual 은 만료가 없으므로 남긴다."""
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(days=days)).isoformat()
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM signals WHERE observed_at < ? AND ttl_sec > 0", (cutoff,))
        conn.execute("DELETE FROM decisions WHERE ts < ?", (cutoff,))
    return cur.rowcount


# --- 결정 이력 ---------------------------------------------------------------

def _decision_key(d: dict) -> tuple:
    """같은 결정인지 판별하는 키. **점수는 넣지 않는다** — 감쇠로 매 사이클
    조금씩 움직이므로 점수를 넣으면 모든 행이 '새 결정' 이 된다."""
    return (d["action"], d.get("from_tier"), d.get("to_tier"))


def _last_keys(conn, codes: list[str]) -> dict[str, tuple]:
    """코드별 가장 최근 결정의 키."""
    if not codes:
        return {}
    marks = ",".join("?" * len(codes))
    rows = conn.execute(
        f"SELECT code, action, from_tier, to_tier FROM decisions WHERE id IN ("
        f"  SELECT MAX(id) FROM decisions WHERE code IN ({marks}) GROUP BY code)",
        codes,
    ).fetchall()
    return {r["code"]: (r["action"], r["from_tier"], r["to_tier"]) for r in rows}


def log_decisions(rows: list[dict], mode: str, applied: bool) -> int:
    """승격·강등 결정을 남긴다. **바뀔 때만 남긴다**(regime_log 와 같은 규약).

    shadow 에서도 남긴다 — 적용하지 않은 결정이야말로 "엔진이라면 이렇게 했을
    것" 의 기록이다. 다만 shadow 는 결정을 반영하지 않으므로 상태가 영원히 그대로고,
    그대로 두면 30초마다 **같은 결론이 다시 쌓인다**. 실측 2026-07-28 09:00~09:36:
    1,140행이 쌓였는데 고유 종목은 27개였다(펌텍코리아 promote_trade 71회).

    이게 원장을 못 쓰게 만든다. 소스별 기여도 측정(4주 뒤)이 읽을 표본이
    "한 번의 판단"이 아니라 "그 판단을 몇 사이클 유지했는가"로 가중돼 버린다.
    직전과 같은 결정은 정보가 없으므로 쓰지 않는다.

    반환: **실제로 기록한** 행 수.
    """
    if not rows:
        return 0
    ts = datetime.now(UTC).isoformat()
    with _conn() as conn:
        last = _last_keys(conn, [d["code"] for d in rows])
        fresh = [d for d in rows if last.get(d["code"]) != _decision_key(d)]
        if not fresh:
            return 0
        conn.executemany(
            "INSERT INTO decisions (ts, code, name, action, from_tier, to_tier,"
            " score, sources, reason, mode, applied, change_pct, cntr_str,"
            " spread_pct) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(ts, d["code"], d.get("name"), d["action"], d.get("from_tier"),
              d.get("to_tier"), d.get("score"),
              json.dumps(sorted(d.get("sources") or []), ensure_ascii=False),
              d.get("reason"), mode, 1 if applied else 0,
              d.get("change_pct"), d.get("cntr_str"),
              d.get("spread_pct")) for d in fresh],
        )
    return len(fresh)


def recent_decisions(limit: int = 200) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) | {"sources": _list(r["sources"])} for r in rows]


def _list(raw) -> list:
    try:
        v = json.loads(raw or "[]")
        return v if isinstance(v, list) else []
    except (TypeError, ValueError):
        return []


# --- 소스 상태 ---------------------------------------------------------------

def mark_ok(source: str, count: int) -> int:
    """성공 폴을 기록하고 **연속 0건 횟수**를 돌려준다.

    조회 성공만 보면 "정상적으로 아무것도 못 찾는" 상태를 잡을 수 없다. 실제로
    `presurge` 가 7/28~7/30 사흘간 `fails: 0 · last_ok` 최신인 채로 신호 0건이었고,
    화면·API 어디에도 그 사실이 드러나지 않았다 — 문턱값 단위가 틀려서 200건을
    받아 전량 탈락시키는 중이었다. **조회 성공은 소스가 살아 있다는 증거가 아니다.**

    장 마감처럼 정당하게 0건인 시간대는 소스를 `enabled()` 에서 끄므로 여기까지
    오지 않는다 — 그래서 이 카운터가 울리면 실제로 이상한 것이다.
    """
    with _conn() as conn:
        conn.execute(
            "INSERT INTO source_health (source, last_ok, fails, signals, empty)"
            " VALUES (?,?,0,?,?) ON CONFLICT(source) DO UPDATE SET"
            " last_ok=excluded.last_ok, fails=0, signals=excluded.signals,"
            " empty=CASE WHEN excluded.signals > 0 THEN 0"
            "            ELSE source_health.empty + 1 END",
            (source, datetime.now(UTC).isoformat(), count, 0 if count else 1),
        )
        row = conn.execute(
            "SELECT empty FROM source_health WHERE source=?", (source,)).fetchone()
    return int(row["empty"] or 0) if row else 0


def mark_fail(source: str, msg: str) -> int:
    """연속 실패 횟수를 돌려준다 — 호출자가 지수 백오프에 쓴다."""
    with _conn() as conn:
        conn.execute(
            "INSERT INTO source_health (source, last_error, error_msg, fails)"
            " VALUES (?,?,?,1) ON CONFLICT(source) DO UPDATE SET"
            " last_error=excluded.last_error, error_msg=excluded.error_msg,"
            " fails=source_health.fails+1",
            (source, datetime.now(UTC).isoformat(), str(msg)[:300]),
        )
        row = conn.execute(
            "SELECT fails FROM source_health WHERE source=?", (source,)).fetchone()
    return int(row["fails"]) if row else 1


def health() -> dict[str, dict]:
    with _conn() as conn:
        return {r["source"]: dict(r)
                for r in conn.execute("SELECT * FROM source_health")}
