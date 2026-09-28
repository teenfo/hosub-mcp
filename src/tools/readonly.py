"""읽기 전용 조회 도구: unit_status, unit_logs, stat_path, sqlite_query (전부 Low).

운영 점검은 대부분 읽기다(`systemctl is-active`, `journalctl`, `stat`, SQLite
SELECT). 전용 도구가 없으면 run_command(High)로 돌려야 해서 `date` 한 줄과
`rm -rf` 가 같은 승인 등급이 되고, 결국 세션이 confirm=true 를 습관적으로 붙여
승인 게이트가 형식이 된다. 이 모듈이 그 읽기들을 Low 로 떼어낸다.

Low 로 둘 수 있는 근거는 "셸을 거치지 않는다"는 것 하나다:
  - systemctl/journalctl 은 고정 argv 템플릿 + shell=False 로만 실행한다.
    유닛 이름은 정규식으로 검증하고, 옵션으로 해석될 수 없게 막는다.
  - sudo 를 쓰지 않는다 (hosub 권한으로 읽히는 것만 읽는다).
  - SQLite 는 파이썬 sqlite3 로 연다 — 읽기 전용 URI + query_only + authorizer
    3중으로 쓰기를 막는다. 대상은 허용 루트 안의 파일뿐이다.
임의 명령 실행 경로가 없으므로 run_command 의 우회로가 되지 않는다.
"""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from ..context import AppContext

KST = timezone(timedelta(hours=9))

# systemd 유닛 이름. 접미사 생략 시 .service. 첫 글자는 옵션('-')이 될 수 없다.
_UNIT_RE = re.compile(
    r"^[A-Za-z0-9_][A-Za-z0-9_.@:\-]{0,200}"
    r"(\.(service|timer|socket|scope|slice|target|mount|path))?$"
)
_UNIT_SUFFIX_RE = re.compile(r"\.(service|timer|socket|scope|slice|target|mount|path)$")
# journalctl --since 값: "2026-09-28 09:00", "-1h", "today", "10 min ago" 등.
_SINCE_RE = re.compile(r"^[A-Za-z0-9 :+.\-]{1,40}$")

_SHOW_PROPS = (
    "LoadState,ActiveState,SubState,Result,ExecMainStatus,ExecMainCode,MainPID,"
    "ActiveEnterTimestamp,ExecMainStartTimestamp,ExecMainExitTimestamp,"
    "InactiveEnterTimestamp,UnitFileState,Description"
)

_SQL_ROW_DEFAULT = 200
_SQL_ROW_MAX = 1000
_SQL_CELL_MAX = 2000
_SQL_TIME_LIMIT = 10.0  # 초. 재귀 CTE 폭주 같은 쿼리를 끊는다.
_DEFAULT_SQLITE_ROOTS = "/data/trading"


def now_kst() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def normalize_unit(ctx: AppContext, unit: str) -> str | None:
    """유닛 이름 검증·정규화. 레지스트리 서비스 이름(예: 'trading')도 받는다."""
    unit = (unit or "").strip()
    entry = ctx.registry.service(unit)
    if entry is not None:
        return entry.unit
    if not _UNIT_RE.match(unit):
        return None
    if not _UNIT_SUFFIX_RE.search(unit):
        unit += ".service"
    return unit


def show_unit(ctx: AppContext, unit: str) -> dict:
    """systemctl show 결과를 dict 로. 셸 없이 고정 argv."""
    res = ctx.runner.run(
        ["systemctl", "show", unit, f"--property={_SHOW_PROPS}", "--no-pager"],
        timeout=10,
    )
    props: dict[str, str] = {}
    for line in res.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            props[k] = v
    return {"ok": res.ok, "props": props, "error": None if res.ok else res.stderr.strip()}


def stat_info(path: str) -> dict:
    p = Path(path)
    if not p.is_absolute():
        return {"status": "rejected", "reason": "절대경로만 허용됩니다.", "path": path}
    try:
        st = p.lstat()
    except FileNotFoundError:
        return {"status": "ok", "path": path, "exists": False}
    except OSError as exc:
        return {"status": "error", "path": path, "error": str(exc)}
    kind = (
        "link" if stat.S_ISLNK(st.st_mode)
        else "dir" if stat.S_ISDIR(st.st_mode)
        else "file" if stat.S_ISREG(st.st_mode)
        else "other"
    )
    out = {
        "status": "ok",
        "path": path,
        "exists": True,
        "type": kind,
        "size": st.st_size,
        "mode": stat.filemode(st.st_mode),
        "uid": st.st_uid,
        "gid": st.st_gid,
        "mtime": datetime.fromtimestamp(st.st_mtime, KST).isoformat(timespec="seconds"),
        "age_seconds": round(time.time() - st.st_mtime, 1),
    }
    if kind == "link":
        try:
            out["link_target"] = os.readlink(p)
            out["target_exists"] = p.exists()
        except OSError:
            pass
    return out


# --- SQLite -------------------------------------------------------------------

# authorizer 가 허용하는 동작. 이 외(INSERT/UPDATE/DELETE/CREATE/DROP/ATTACH/
# PRAGMA 쓰기/트랜잭션 등)는 전부 SQLITE_DENY.
_SQL_ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),
}
# 읽기 전용 PRAGMA 중 스키마 파악에 쓰는 것만.
_SQL_ALLOWED_PRAGMAS = {"table_info", "table_xinfo", "index_list", "index_info", "table_list"}
# 부수효과가 있거나 서버 정보를 흘리는 함수는 막는다.
_SQL_DENIED_FUNCTIONS = {"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer"}


def _authorizer(action, arg1, arg2, dbname, source):
    if action == sqlite3.SQLITE_PRAGMA:
        # 조회형 PRAGMA 만. table_info(t) 처럼 인자(arg2)는 테이블 이름이라 허용한다.
        ok = (arg1 or "").lower() in _SQL_ALLOWED_PRAGMAS
        return sqlite3.SQLITE_OK if ok else sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION:
        return sqlite3.SQLITE_DENY if (arg2 or "").lower() in _SQL_DENIED_FUNCTIONS else sqlite3.SQLITE_OK
    return sqlite3.SQLITE_OK if action in _SQL_ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def sqlite_roots() -> list[str]:
    raw = os.environ.get("HOSUB_SQLITE_ROOTS", _DEFAULT_SQLITE_ROOTS)
    return [os.path.realpath(r.strip()) for r in raw.split(",") if r.strip()]


def _resolve_db(db: str) -> tuple[str | None, str | None]:
    if not os.path.isabs(db):
        return None, "절대경로만 허용됩니다."
    real = os.path.realpath(db)  # 심볼릭 링크·'..' 로 허용 루트를 벗어나는 것 차단
    roots = sqlite_roots()
    if not any(real == r or real.startswith(r + os.sep) for r in roots):
        return None, f"허용 경로 밖입니다. 허용 루트: {roots} (HOSUB_SQLITE_ROOTS)"
    if not os.path.isfile(real):
        return None, "파일이 없습니다."
    return real, None


def _first_keyword(sql: str) -> str:
    s = sql.lstrip()
    while True:  # 선행 주석 건너뛰기
        if s.startswith("--"):
            s = s.split("\n", 1)[1].lstrip() if "\n" in s else ""
        elif s.startswith("/*"):
            s = s.split("*/", 1)[1].lstrip() if "*/" in s else ""
        else:
            break
    m = re.match(r"[A-Za-z]+", s)
    return m.group(0).upper() if m else ""


def _cell(v):
    if isinstance(v, bytes):
        return f"<blob {len(v)} bytes>"
    if isinstance(v, str) and len(v) > _SQL_CELL_MAX:
        return v[:_SQL_CELL_MAX] + "…"
    return v


def run_sqlite_query(db: str, sql: str, max_rows: int) -> dict:
    """읽기 전용 SQLite 조회. 쓰기 방어선은 3중이다:
    1) URI mode=ro 로 연다 (파일 핸들 자체가 읽기 전용)
    2) PRAGMA query_only=ON
    3) authorizer — SELECT/READ/FUNCTION/RECURSIVE 와 조회형 PRAGMA 외 전부 거부
    첫 키워드 검사(SELECT/WITH/PRAGMA)는 친절한 오류 메시지용일 뿐 방어선이 아니다.
    """
    max_rows = max(1, min(int(max_rows), _SQL_ROW_MAX))
    kw = _first_keyword(sql)
    if kw not in ("SELECT", "WITH", "PRAGMA", "VALUES"):
        return {
            "status": "rejected",
            "reason": "SELECT / WITH 조회만 허용됩니다. 쓰기는 run_command(High)로 하세요.",
        }
    real, err = _resolve_db(db)
    if err:
        return {"status": "rejected", "reason": err, "db": db}

    deadline = time.monotonic() + _SQL_TIME_LIMIT
    try:
        conn = sqlite3.connect(f"file:{real}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        return {"status": "error", "db": db, "error": str(exc)}
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.set_authorizer(_authorizer)
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
        try:
            cur = conn.execute(sql)  # 다중 문장은 sqlite3 가 거부한다
            rows = cur.fetchmany(max_rows + 1)
        except sqlite3.OperationalError as exc:
            msg = str(exc)
            if "interrupted" in msg:
                msg = f"시간 제한({_SQL_TIME_LIMIT:.0f}s) 초과로 중단"
            elif "not authorized" in msg:
                msg = "허용되지 않는 동작입니다(읽기 전용). " + msg
            return {"status": "error", "db": db, "error": msg}
        except (sqlite3.Error, ValueError) as exc:  # Warning(다중 문장) 포함
            return {"status": "error", "db": db, "error": str(exc)}
        columns = [d[0] for d in (cur.description or [])]
    finally:
        conn.close()
    truncated = len(rows) > max_rows
    rows = rows[:max_rows]
    return {
        "status": "ok",
        "db": db,
        "columns": columns,
        "rows": [[_cell(v) for v in r] for r in rows],
        "row_count": len(rows),
        "truncated": truncated,
    }


def register(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool()
    def unit_status(unit: str, path: str | None = None) -> dict:
        """systemd 유닛 상태를 조회한다 (읽기 전용, 승인 불필요).

        unit: 유닛 이름(예: trading.service, mcp-sweep, hosub-mcp-update.timer)
              또는 레지스트리 서비스 이름(예: trading). 접미사 생략 시 .service.
              systemd-run/run_unit 으로 띄운 transient 유닛도 조회된다.
        path: (선택) 함께 확인할 결과 파일 경로 — 존재·크기·mtime 을 붙여 준다.

        반환: active_state/sub_state/result/exit_status 와 각종 타임스탬프,
              server_time(KST). `systemctl is-active` 는 active_state 로 본다.
        """
        norm = normalize_unit(ctx, unit)
        if norm is None:
            ctx.audit.log(tool="unit_status", params={"unit": unit}, outcome="rejected", risk="low")
            return {"status": "rejected", "reason": f"올바르지 않은 유닛 이름: {unit!r}"}
        info = show_unit(ctx, norm)
        p = info["props"]
        out = {
            "status": "ok" if info["ok"] else "error",
            "unit": norm,
            "load_state": p.get("LoadState"),
            "active_state": p.get("ActiveState"),
            "sub_state": p.get("SubState"),
            "result": p.get("Result"),
            "exit_status": p.get("ExecMainStatus"),
            "main_pid": p.get("MainPID"),
            "active_enter": p.get("ActiveEnterTimestamp") or None,
            "exec_start": p.get("ExecMainStartTimestamp") or None,
            "exec_exit": p.get("ExecMainExitTimestamp") or None,
            "inactive_enter": p.get("InactiveEnterTimestamp") or None,
            "unit_file_state": p.get("UnitFileState") or None,
            "description": p.get("Description") or None,
            "server_time": now_kst(),
        }
        if info["error"]:
            out["error"] = info["error"]
        if p.get("LoadState") == "not-found":
            out["note"] = "유닛이 없습니다. 이름을 확인하거나, --collect 로 띄운 transient 유닛이면 끝난 뒤 사라진 것입니다(unit_logs 로 기록 확인)."
        if path:
            out["path_info"] = stat_info(path)
        ctx.audit.log(tool="unit_status", params={"unit": norm, "path": path}, outcome=out["status"], risk="low")
        return out

    @mcp.tool()
    def unit_logs(unit: str, lines: int = 100, since: str | None = None) -> dict:
        """systemd 유닛의 journald 로그를 조회한다 (읽기 전용, 승인 불필요).

        레지스트리에 없는 유닛(transient 유닛 포함)도 받는다 — read_service_logs 와 차이.
        unit: 유닛 이름 또는 레지스트리 서비스 이름.
        lines: 최근 N 줄 (1~2000, 기본 100).
        since: (선택) 시작 시각. 예) "2026-09-28 09:00", "-2h", "today", "30 min ago".
        """
        norm = normalize_unit(ctx, unit)
        if norm is None:
            ctx.audit.log(tool="unit_logs", params={"unit": unit}, outcome="rejected", risk="low")
            return {"status": "rejected", "reason": f"올바르지 않은 유닛 이름: {unit!r}"}
        if since is not None and not _SINCE_RE.match(since):
            return {"status": "rejected", "reason": f"since 형식이 올바르지 않습니다: {since!r}"}
        lines = max(1, min(int(lines), 2000))
        argv = ["journalctl", "-u", norm, "-n", str(lines), "--no-pager", "-o", "short-iso"]
        if since:
            argv.append(f"--since={since}")  # '=' 로 붙여 옵션 해석 여지를 없앤다
        res = ctx.runner.run(argv, timeout=20)
        ctx.audit.log(
            tool="unit_logs",
            params={"unit": norm, "lines": lines, "since": since},
            outcome="ok" if res.ok else "error",
            risk="low",
        )
        return {
            "status": "ok" if res.ok else "error",
            "unit": norm,
            "lines": lines,
            "since": since,
            "log": res.stdout if res.ok else "",
            "error": None if res.ok else (res.stderr.strip() or "journalctl 조회 실패"),
        }

    @mcp.tool()
    def stat_path(path: str) -> dict:
        """파일/디렉터리의 존재·종류·크기·권한·mtime(KST)·경과 초를 조회한다 (읽기 전용).

        path: 절대경로. 결과 파일이 갱신됐는지 확인하는 용도로 쓴다.
        응답의 server_time 으로 서버 현재 시각(KST)도 함께 알 수 있다.
        """
        out = stat_info(path)
        out["server_time"] = now_kst()
        ctx.audit.log(tool="stat_path", params={"path": path}, outcome=out["status"], risk="low")
        return out

    @mcp.tool()
    def sqlite_query(db: str, sql: str, max_rows: int = _SQL_ROW_DEFAULT) -> dict:
        """SQLite DB 에 읽기 전용 조회를 실행한다 (승인 불필요).

        db: DB 파일 절대경로. 허용 루트(기본 /data/trading, 환경변수
            HOSUB_SQLITE_ROOTS 로 변경) 안의 파일만 열 수 있다.
        sql: 단일 SELECT / WITH 문. 스키마 확인은
             `SELECT name, sql FROM sqlite_master` 또는 `PRAGMA table_info(t)`.
        max_rows: 최대 행 수 (기본 200, 상한 1000). 초과 시 truncated=true.
        쓰기·ATTACH·트랜잭션은 모두 거부된다. 시간 제한 10초.
        """
        out = run_sqlite_query(db, sql, max_rows)
        ctx.audit.log(
            tool="sqlite_query",
            params={"db": db, "sql": sql[:500]},
            outcome=out["status"],
            risk="low",
            result_summary=out.get("reason") or out.get("error") or f"{out.get('row_count')} rows",
        )
        return out
