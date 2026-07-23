"""감사 로그 (SQLite).

모든 도구 호출과 백그라운드 잡 종결을 기록한다. 임의 명령/파일 쓰기를
허용하는 서버이므로, 이 로그가 "누가 언제 무엇을 실행했는가" 의 유일한
사후 추적 수단이다.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  ts             TEXT NOT NULL,
  tool           TEXT NOT NULL,
  params_json    TEXT NOT NULL,
  confirm        INTEGER NOT NULL DEFAULT 0,
  risk           TEXT,
  outcome        TEXT NOT NULL,
  result_summary TEXT,
  job_id         TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
"""

_SUMMARY_MAX = 600


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuditLog:
    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(_SCHEMA)

    def log(
        self,
        *,
        tool: str,
        params: dict | None = None,
        confirm: bool = False,
        risk: str | None = None,
        outcome: str,
        result_summary: str | None = None,
        job_id: str | None = None,
    ) -> None:
        try:
            params_json = json.dumps(params or {}, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            params_json = json.dumps({"_unserializable": str(params)})
        summary = result_summary
        if summary and len(summary) > _SUMMARY_MAX:
            summary = summary[:_SUMMARY_MAX] + "…"
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO audit_log "
                "(ts, tool, params_json, confirm, risk, outcome, result_summary, job_id) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    _utcnow(),
                    tool,
                    params_json,
                    1 if confirm else 0,
                    risk,
                    outcome,
                    summary,
                    job_id,
                ),
            )

    def recent(self, limit: int = 50) -> list[dict]:
        limit = max(1, min(limit, 500))
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, ts, tool, params_json, confirm, risk, outcome, "
                "result_summary, job_id FROM audit_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
