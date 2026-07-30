"""잡·사용량 영속 저장 (SQLite).

잡이 재시작에도 살아남아야 한다 — 배치 분석을 잃으면 안 되기 때문이다.
기동 시 running 으로 남은 잡은 queued 로 되돌린다(크래시 복구).
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
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

-- 역할 런타임 오버라이드. roles.yaml 이 기본값, 이 표가 그 위에 얹힌다.
-- 개별 컬럼이 아니라 fields_json 인 이유: 나중에 덮어쓸 수 있는 필드를 늘려도
-- 스키마가 안 바뀐다. 검증은 SQL 이 아니라 config.validate_role_fields 가 한다.
CREATE TABLE IF NOT EXISTS role_overrides (
  role        TEXT PRIMARY KEY,
  origin      TEXT NOT NULL,           -- yaml(=기존 역할 덮어쓰기) | db(=신규 역할)
  fields_json TEXT NOT NULL,
  note        TEXT,
  updated_by  TEXT,
  updated_at  TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

-- 관리 작업 감사 로그. 대시보드 감사와 별개로 게이트웨이가 직접 남긴다
-- (MCP·curl 로도 들어올 수 있으므로 두 기록은 다른 질문에 답한다).
CREATE TABLE IF NOT EXISTS admin_audit (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL,
  actor       TEXT NOT NULL,
  action      TEXT NOT NULL,
  target      TEXT,
  detail_json TEXT,
  outcome     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admin_audit_ts ON admin_audit(ts DESC);

CREATE INDEX IF NOT EXISTS idx_usage_model_ts ON usage(model, ts);

-- 모델 A/B 비교 실행. 실행 자체는 평범한 잡 4개(각 측 워밍업 1 + 측정 1)이고,
-- 이 표는 그것들을 묶어 "무엇을 왜 비교했는지" 를 남긴다.
CREATE TABLE IF NOT EXISTS ab_runs (
  id           TEXT PRIMARY KEY,
  created_at   TEXT NOT NULL,
  actor        TEXT NOT NULL,
  prompt       TEXT NOT NULL,
  system       TEXT,
  options_json TEXT,
  model_a      TEXT NOT NULL,
  model_b      TEXT NOT NULL,
  jobs_json    TEXT NOT NULL,     -- {"a": {"warmup": id, "measure": id}, "b": {...}}
  status       TEXT NOT NULL      -- running|done
);
"""

# 기존 표에 **덧붙이는** 컬럼. CREATE TABLE IF NOT EXISTS 로는 못 하므로
# _migrate() 가 PRAGMA 로 확인 후 ALTER TABLE 한다.
# 추가 전용·NULL 기본값만 허용한다(재작성·삭제 금지) — SQLite 의 ADD COLUMN 은
# 메타데이터 연산이라 WAL 라이브 DB 에서 안전하다.
_ADD_COLUMNS = (
    # 잡을 자기완결형으로: 실행 시점이 아니라 **생성 시점**의 역할 설정을 쓴다.
    # 안 그러면 역할 모델을 바꿨을 때 큐에 있던 잡이 옛 모델 + 새 옵션으로 돈다.
    ("jobs", "options_json", "TEXT"),
    ("jobs", "timeout_s", "INTEGER"),
    # Ollama 가 주는 load/prompt_eval/eval 세부 시간. A/B 비교의 tok/s 근거.
    ("jobs", "metrics_json", "TEXT"),
)

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


def _loads_dict(text) -> dict | None:
    """JSON 매핑으로만 받는다. 손상된 값이 잡을 죽이지 않도록 None 으로 떨어뜨린다."""
    if not text:
        return None
    try:
        v = json.loads(text)
    except ValueError:
        return None
    return v if isinstance(v, dict) else None


def _row_to_job(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["metadata"] = _loads_dict(d.pop("metadata_json", None)) or {}
    # 옛 행에는 스냅샷 컬럼이 없다(NULL) — 호출부가 "없으면 live 역할"로 폴백한다
    if "options_json" in d:
        d["options"] = _loads_dict(d.pop("options_json"))
    if "metrics_json" in d:
        d["metrics"] = _loads_dict(d.pop("metrics_json"))
    return d


class Store:
    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._lock, self._connect() as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """기존 DB 에 신규 컬럼을 덧붙인다(추가 전용).

        운영 DB 에는 이미 잡·사용량이 쌓여 있어 재생성이 불가능하다. 표를
        다시 쓰지 않고 ALTER TABLE ADD COLUMN 만 쓰므로 기존 행은 그대로 남고
        새 컬럼은 NULL 이 된다.
        """
        for table, column, decl in _ADD_COLUMNS:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if not cols:
                continue  # 표 자체가 없다 = _SCHEMA 가 만들 것
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

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
        options: dict | None = None,
        timeout: int | None = None,
    ) -> dict:
        """잡 하나를 큐에 넣는다.

        model 뿐 아니라 options·timeout 도 **생성 시점에 스냅샷**한다. 역할의
        모델을 런타임에 바꿀 수 있게 되면서, 큐에 있던 잡이 옛 모델 + 새 옵션으로
        도는 조용한 오염 경로가 생기기 때문이다. 잡은 자기완결형이어야 한다.
        """
        job_id = secrets.token_hex(6)
        now = utcnow()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO jobs (id, service, role, model, lane, status, priority,"
                " prompt, system, metadata_json, created_at, options_json, timeout_s)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id, service, role, model, lane, QUEUED, priority,
                    prompt, system, json.dumps(metadata or {}, ensure_ascii=False), now,
                    json.dumps(options, ensure_ascii=False) if options is not None else None,
                    int(timeout) if timeout is not None else None,
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
               error: str | None = None, metrics: dict | None = None) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status=?, response=?, error=?, finished_at=?,"
                " metrics_json=? WHERE id=?",
                (status, response, error, utcnow(),
                 json.dumps(metrics, ensure_ascii=False) if metrics else None,
                 job_id),
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

    def usage_by_model(self, days: int = 30, limit: int = 50) -> list[dict]:
        """모델별 사용 이력. "이 모델을 정말 쓰고 있나" 를 삭제 전에 확인한다.

        usage.model 컬럼은 예전부터 있었는데 usage_summary 가 서비스로만 묶어서
        쓰이지 않고 있었다. 스키마 변경 없이 다른 각도로 집계한다.
        보존 기간(purge_old, 기본 30일)을 넘는 이력은 남아 있지 않다.
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT model, COUNT(*) calls, SUM(eval_count) tokens,"
                " SUM(duration_ms) total_ms,"
                " SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) ok,"
                " MAX(ts) last_used FROM usage"
                " WHERE model IS NOT NULL AND ts >= datetime('now', ?)"
                " GROUP BY model ORDER BY calls DESC LIMIT ?",
                (f"-{int(days)} days", max(1, min(int(limit), 200))),
            ).fetchall()
        return [dict(r) for r in rows]

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

    def usage_by_service(self, days: int = 7) -> dict[str, dict]:
        """서비스별 마지막 사용 시각 + 최근 N일 호출 수.

        `usage_summary` 를 확장하지 않고 따로 둔다 — 그쪽은 `/v1/status` 응답에
        그대로 실려 나가므로 키를 늘리면 소비자 계약이 바뀐다.
        `last_at` 은 전 기간, `calls` 는 창(window) 안이다.
        """
        since = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).isoformat()
        with self._lock, self._connect() as conn:
            totals = conn.execute(
                "SELECT service, MAX(ts) last_at, COUNT(*) total FROM usage"
                " GROUP BY service"
            ).fetchall()
            recent = conn.execute(
                "SELECT service, COUNT(*) c FROM usage WHERE ts >= ? GROUP BY service",
                (since,),
            ).fetchall()
        window = {r["service"]: int(r["c"]) for r in recent}
        return {
            r["service"]: {
                "last_at": r["last_at"],
                "calls_total": int(r["total"]),
                "calls_window": window.get(r["service"], 0),
            }
            for r in totals
        }

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

    def set_model_request_size(self, model: str, est_size_gb: float) -> None:
        """추정 크기만 갱신. ensure_model_request 는 기존 행의 크기를 안 고친다 —
        실측·카탈로그로 추정이 좋아졌는데 옛 값이 UI 에 남으면 안 된다."""
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE model_requests SET est_size_gb=? WHERE model=?",
                         (float(est_size_gb), model))

    def delete_model_request(self, model: str) -> bool:
        """요청 행 자체를 지운다(모델을 삭제했을 때).

        ready 로 남기면 _triage_missing 이 ready→pending 으로 되살리고 Slack 까지
        쏜다. rejected 로 바꾸는 것도 답이 아니다 — DEAD_STATUSES 라 이후 잡이
        "설치가 거부됨" 이라는 **거짓 사유**로 하드 실패한다. 행이 없는 것이
        의미상 정확하다("미결 요청 없음"). 이력은 admin_audit 에 남는다.
        """
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM model_requests WHERE model=?", (model,))
            return cur.rowcount == 1

    def queued_job_models(self) -> dict[str, int]:
        """대기 중인 잡의 모델별 개수. 느슨한 매칭은 SQL 이 아니라 파이썬에서 한다."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT model, COUNT(*) c FROM jobs WHERE status=? AND model IS NOT NULL"
                " GROUP BY model", (QUEUED,),
            ).fetchall()
        return {r["model"]: int(r["c"]) for r in rows}

    def model_request_statuses(self) -> dict[str, str]:
        """모델 → 요청 상태. 스케줄러가 잡을 대기시킬지/실패시킬지 판단한다."""
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT model, status FROM model_requests").fetchall()
        return {r["model"]: r["status"] for r in rows}

    # --- 역할 오버라이드 ---
    def list_role_overrides(self) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM role_overrides ORDER BY role"
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["fields"] = _loads_dict(d.pop("fields_json")) or {}
            out.append(d)
        return out

    def set_role_override(self, role: str, *, origin: str, fields: dict,
                          note: str | None = None,
                          updated_by: str | None = None) -> None:
        now = utcnow()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO role_overrides (role, origin, fields_json, note,"
                " updated_by, updated_at, created_at) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(role) DO UPDATE SET origin=excluded.origin,"
                " fields_json=excluded.fields_json, note=excluded.note,"
                " updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (role, origin, json.dumps(fields, ensure_ascii=False), note,
                 updated_by, now, now),
            )

    def delete_role_override(self, role: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM role_overrides WHERE role=?", (role,))
            return cur.rowcount == 1

    # --- A/B 비교 ---
    def create_ab_run(self, *, actor: str, prompt: str, system: str | None,
                      options: dict | None, model_a: str, model_b: str,
                      jobs: dict) -> dict:
        run_id = secrets.token_hex(6)
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO ab_runs (id, created_at, actor, prompt, system,"
                " options_json, model_a, model_b, jobs_json, status)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (run_id, utcnow(), actor, prompt, system,
                 json.dumps(options or {}, ensure_ascii=False),
                 model_a, model_b, json.dumps(jobs, ensure_ascii=False), "running"),
            )
        return self.get_ab_run(run_id)

    def get_ab_run(self, run_id: str) -> dict | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM ab_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["options"] = _loads_dict(d.pop("options_json")) or {}
        d["jobs"] = _loads_dict(d.pop("jobs_json")) or {}
        return d

    def list_ab_runs(self, limit: int = 10) -> list[dict]:
        limit = max(1, min(int(limit), 50))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM ab_runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self.get_ab_run(r["id"]) for r in rows]

    def set_ab_run_status(self, run_id: str, status: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE ab_runs SET status=? WHERE id=?", (status, run_id))

    # --- 감사 로그 ---
    def record_audit(self, *, actor: str, action: str, target: str | None = None,
                     detail: dict | None = None, outcome: str = "ok") -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO admin_audit (ts, actor, action, target, detail_json,"
                " outcome) VALUES (?,?,?,?,?,?)",
                (utcnow(), actor, action, target,
                 json.dumps(detail or {}, ensure_ascii=False), outcome),
            )

    def list_audit(self, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit), 200))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM admin_audit ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = _loads_dict(d.pop("detail_json")) or {}
            out.append(d)
        return out

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
