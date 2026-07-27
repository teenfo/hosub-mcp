"""잡·사용량 영속 저장 (SQLite).

잡이 재시작에도 살아남아야 한다 — 배치 분석을 잃으면 안 되기 때문이다.
기동 시 running 으로 남은 잡은 queued 로 되돌린다(크래시 복구).
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id            TEXT PRIMARY KEY,
  service       TEXT NOT NULL,
  role          TEXT NOT NULL,
  model         TEXT,
  lane          TEXT NOT NULL,
  status        TEXT NOT NULL,
  priority      INTEGER NOT NULL DEFAULT 0,
  prompt        TEXT NOT NULL,
  system        TEXT,
  response      TEXT,
  error         TEXT,
  metadata_json TEXT,
  attempts      INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL,
  started_at    TEXT,
  finished_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_queue
  ON jobs(lane, status, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_service ON jobs(service, created_at DESC);

CREATE TABLE IF NOT EXISTS usage (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL,
  service     TEXT NOT NULL,
  role        TEXT NOT NULL,
  model       TEXT,
  eval_count  INTEGER,
  duration_ms INTEGER,
  status      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts);

-- 모델 설치 요청 (미설치 모델을 만나면 자동 생성 → 사용자가 승인하면 pull)
CREATE TABLE IF NOT EXISTS model_requests (
  model        TEXT PRIMARY KEY,
  status       TEXT NOT NULL,          -- pending|approved|pulling|ready|rejected|failed
  requested_by TEXT,                   -- 최초로 필요로 한 서비스
  roles_json   TEXT,                   -- 이 모델을 쓰는 역할들
  est_size_gb  REAL,
  progress     INTEGER DEFAULT 0,      -- 0~100
  error        TEXT,
  created_at   TEXT NOT NULL,
  decided_at   TEXT,
  finished_at  TEXT
);
"""

MR_PENDING = "pending"
MR_APPROVED = "approved"
MR_PULLING = "pulling"
MR_READY = "ready"
MR_REJECTED = "rejected"
MR_FAILED = "failed"

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL = (SUCCEEDED, FAILED, CANCELLED)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_job(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["metadata"] = json.loads(d.pop("metadata_json") or "{}")
    return d


class Store:
    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._lock, self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # --- 복구 ---
    def recover_running(self) -> int:
        """크래시로 running 에 멈춘 잡을 queued 로 되돌린다. 되돌린 개수 반환."""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status=?, started_at=NULL WHERE status=?",
                (QUEUED, RUNNING),
            )
            return cur.rowcount

    # --- 생성/조회 ---
    def create_job(
        self,
        *,
        service: str,
        role: str,
        model: str,
        lane: str,
        prompt: str,
        system: str | None,
        priority: int = 0,
        metadata: dict | None = None,
    ) -> dict:
        job_id = secrets.token_hex(6)
        now = utcnow()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO jobs (id, service, role, model, lane, status, priority,"
                " prompt, system, metadata_json, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id, service, role, model, lane, QUEUED, priority,
                    prompt, system, json.dumps(metadata or {}, ensure_ascii=False), now,
                ),
            )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    def list_jobs(self, service: str | None = None, status: str | None = None,
                  limit: int = 20) -> list[dict]:
        limit = max(1, min(int(limit), 200))
        q = "SELECT * FROM jobs"
        conds, args = [], []
        if service:
            conds.append("service=?"); args.append(service)
        if status:
            conds.append("status=?"); args.append(status)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._lock, self._connect() as conn:
            rows = conn.execute(q, args).fetchall()
        return [_row_to_job(r) for r in rows]

    def queue_position(self, job_id: str) -> int | None:
        """같은 레인에서 앞에 몇 개가 대기 중인지(0 = 다음 차례)."""
        job = self.get_job(job_id)
        if not job or job["status"] != QUEUED:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) c FROM jobs WHERE lane=? AND status=? AND"
                " (priority > ? OR (priority = ? AND created_at < ?))",
                (job["lane"], QUEUED, job["priority"], job["priority"], job["created_at"]),
            ).fetchone()
        return int(row["c"])

    # --- 스케줄러용 ---
    def queued_jobs(self, lane: str) -> list[dict]:
        """레인의 대기 잡을 우선순위·생성순으로."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE lane=? AND status=?"
                " ORDER BY priority DESC, created_at ASC",
                (lane, QUEUED),
            ).fetchall()
        return [_row_to_job(r) for r in rows]

    def claim(self, job_id: str) -> bool:
        """queued → running 원자적 전이. 이미 누가 가져갔으면 False."""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status=?, started_at=COALESCE(started_at, ?),"
                " attempts=attempts+1 WHERE id=? AND status=?",
                (RUNNING, utcnow(), job_id, QUEUED),
            )
            return cur.rowcount == 1

    def requeue(self, job_id: str) -> None:
        """재시도를 위해 running → queued 로 되돌린다(attempts 는 유지)."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status=?, started_at=NULL WHERE id=? AND status=?",
                (QUEUED, job_id, RUNNING),
            )

    def fail_if_queued(self, job_id: str, error: str) -> bool:
        """대기 중일 때만 실패 처리(실행 중인 잡을 가로채지 않도록 원자적으로)."""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status=?, error=?, finished_at=?"
                " WHERE id=? AND status=?",
                (FAILED, error, utcnow(), job_id, QUEUED),
            )
            return cur.rowcount == 1

    def finish(self, job_id: str, *, status: str, response: str | None = None,
               error: str | None = None) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status=?, response=?, error=?, finished_at=?"
                " WHERE id=?",
                (status, response, error, utcnow(), job_id),
            )

    def cancel(self, job_id: str, service: str) -> bool:
        """대기 중인 잡만 취소 가능. 본인 서비스 잡만."""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status=?, finished_at=? WHERE id=? AND service=?"
                " AND status=?",
                (CANCELLED, utcnow(), job_id, service, QUEUED),
            )
            return cur.rowcount == 1

    # --- 사용량 ---
    def record_usage(self, *, service: str, role: str, model: str | None,
                     eval_count: int | None, duration_ms: int | None,
                     status: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO usage (ts, service, role, model, eval_count, duration_ms,"
                " status) VALUES (?,?,?,?,?,?,?)",
                (utcnow(), service, role, model, eval_count, duration_ms, status),
            )

    def usage_summary(self, limit_services: int = 20) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT service, COUNT(*) calls, SUM(eval_count) tokens,"
                " SUM(duration_ms) total_ms,"
                " SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) ok"
                " FROM usage GROUP BY service ORDER BY calls DESC LIMIT ?",
                (limit_services,),
            ).fetchall()
        return [dict(r) for r in rows]

    def counts_by_status(self) -> dict:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT lane, status, COUNT(*) c FROM jobs"
                " WHERE status IN (?,?) GROUP BY lane, status",
                (QUEUED, RUNNING),
            ).fetchall()
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            out.setdefault(r["lane"], {})[r["status"]] = int(r["c"])
        return out

    # --- 모델 설치 요청 ---
    def ensure_model_request(self, model: str, *, requested_by: str,
                             roles: list[str], est_size_gb: float) -> dict:
        """미설치 모델 요청을 만든다(이미 있으면 갱신만 하고 반환).

        거부/실패했던 요청을 다시 만나도 pending 으로 되돌리지 않는다 —
        사용자의 거부 결정을 존중하고, 재요청은 명시적 재승인으로만.
        예외는 ready 였던 모델이 다시 사라진 경우(맥에서 삭제 등)로,
        이때는 pending 으로 되돌려 다시 승인을 받는다.
        """
        roles_json = json.dumps(sorted(roles), ensure_ascii=False)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM model_requests WHERE model=?", (model,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO model_requests (model, status, requested_by, roles_json,"
                    " est_size_gb, created_at) VALUES (?,?,?,?,?,?)",
                    (model, MR_PENDING, requested_by, roles_json, est_size_gb, utcnow()),
                )
            elif row["status"] == MR_READY:
                conn.execute(
                    "UPDATE model_requests SET status=?, roles_json=?, progress=0,"
                    " error=NULL, decided_at=NULL, finished_at=NULL, created_at=?"
                    " WHERE model=?",
                    (MR_PENDING, roles_json, utcnow(), model),
                )
            else:
                # 쓰는 역할 목록은 최신으로 갱신(정보성)
                conn.execute(
                    "UPDATE model_requests SET roles_json=? WHERE model=?",
                    (roles_json, model),
                )
        return self.get_model_request(model)

    def get_model_request(self, model: str) -> dict | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM model_requests WHERE model=?", (model,)
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["roles"] = json.loads(d.pop("roles_json") or "[]")
        return d

    def list_model_requests(self, status: str | None = None) -> list[dict]:
        q = "SELECT * FROM model_requests"
        args: list = []
        if status:
            q += " WHERE status=?"; args.append(status)
        q += " ORDER BY created_at DESC"
        with self._lock, self._connect() as conn:
            rows = conn.execute(q, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["roles"] = json.loads(d.pop("roles_json") or "[]")
            out.append(d)
        return out

    def set_model_request_status(self, model: str, status: str, *,
                                 error: str | None = None,
                                 progress: int | None = None) -> bool:
        sets = ["status=?"]
        args: list = [status]
        if error is not None:
            sets.append("error=?"); args.append(error)
        if progress is not None:
            sets.append("progress=?"); args.append(progress)
        if status in (MR_APPROVED, MR_REJECTED):
            sets.append("decided_at=?"); args.append(utcnow())
        if status in (MR_READY, MR_FAILED):
            sets.append("finished_at=?"); args.append(utcnow())
        args.append(model)
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                f"UPDATE model_requests SET {', '.join(sets)} WHERE model=?", args
            )
            return cur.rowcount == 1

    def claim_approved_model(self) -> dict | None:
        """approved → pulling 원자 전이. 설치할 모델이 없으면 None."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT model FROM model_requests WHERE status=? ORDER BY created_at LIMIT 1",
                (MR_APPROVED,),
            ).fetchone()
            if not row:
                return None
            cur = conn.execute(
                "UPDATE model_requests SET status=?, progress=0 WHERE model=? AND status=?",
                (MR_PULLING, row["model"], MR_APPROVED),
            )
            if cur.rowcount != 1:
                return None
        return self.get_model_request(row["model"])

    def model_request_statuses(self) -> dict[str, str]:
        """모델 → 요청 상태. 스케줄러가 잡을 대기시킬지/실패시킬지 판단한다."""
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT model, status FROM model_requests").fetchall()
        return {r["model"]: r["status"] for r in rows}

    def purge_old(self, days: int = 30) -> dict:
        """오래된 잡·사용량 정리. 반환: {"jobs": n, "usage": n}

        모든 요청이 프롬프트·응답·사용량 행을 남기므로 정리하지 않으면 DB 가
        무한히 커진다. /data 디스크를 채우기 전에 주기적으로 부른다.
        """
        cutoff = f"-{int(days)} days"
        with self._lock, self._connect() as conn:
            jobs = conn.execute(
                "DELETE FROM jobs WHERE status IN (?,?,?)"
                " AND finished_at IS NOT NULL AND finished_at < datetime('now', ?)",
                (SUCCEEDED, FAILED, CANCELLED, cutoff),
            ).rowcount
            # 사용량은 집계용이라 잡보다 오래 남길 이유가 없다
            usage = conn.execute(
                "DELETE FROM usage WHERE ts < datetime('now', ?)", (cutoff,)
            ).rowcount
        return {"jobs": jobs, "usage": usage}
