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

from .. import settings
from .model import TTL_SEC, Signal

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

        CREATE TABLE IF NOT EXISTS source_health (
            source TEXT PRIMARY KEY,
            last_ok TEXT, last_error TEXT, error_msg TEXT,
            fails INTEGER NOT NULL DEFAULT 0,
            signals INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    return conn


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
            " score, sources, reason, mode, applied) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(ts, d["code"], d.get("name"), d["action"], d.get("from_tier"),
              d.get("to_tier"), d.get("score"),
              json.dumps(sorted(d.get("sources") or []), ensure_ascii=False),
              d.get("reason"), mode, 1 if applied else 0) for d in fresh],
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

def mark_ok(source: str, count: int) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO source_health (source, last_ok, fails, signals)"
            " VALUES (?,?,0,?) ON CONFLICT(source) DO UPDATE SET"
            " last_ok=excluded.last_ok, fails=0, signals=excluded.signals",
            (source, datetime.now(UTC).isoformat(), count),
        )


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
