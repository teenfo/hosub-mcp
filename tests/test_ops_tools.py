"""운영 점검 도구(읽기 전용 계층·run_unit·배포 시간창·잡 소실 표시) 검증."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
from datetime import datetime

import pytest

from src.audit import AuditLog
from src.jobs import JobManager, JobState, Step
from src.policy import Risk, risk_of
from src.registry import KST, Registry, RegistryError
from src.runner import RunResult
from src.server import build_context, build_mcp
from tests.conftest import FakeRunner

REG = {
    "services": {
        "trading": {
            "unit": "trading.service",
            "deploy": {
                "workdir": "/opt/t",
                "steps": [["git", "pull"]],
                "restart_after": True,
                "blackout": [
                    {"days": "mon-fri", "from": "08:50", "to": "15:40"},
                    {"days": "mon-fri", "from": "17:25", "to": "20:00"},
                ],
            },
        }
    }
}


def _make(runner=None, reg=REG):
    registry = Registry.from_dict(reg)
    audit = AuditLog(tempfile.mktemp(suffix=".db"))
    ctx = build_context(registry, runner or FakeRunner(), audit)
    return build_mcp(ctx), ctx


async def _call(mcp, name, args):
    result = await mcp.call_tool(name, args)
    if isinstance(result, list) and result and hasattr(result[0], "text"):
        return json.loads(result[0].text)
    if isinstance(result, tuple):  # (content, structured)
        return json.loads(result[0][0].text)
    return result


# --- 정책 ---------------------------------------------------------------------


def test_readonly_tools_are_low_and_run_unit_high():
    for t in ("unit_status", "unit_logs", "stat_path", "sqlite_query"):
        assert risk_of(t) is Risk.LOW
    assert risk_of("run_unit") is Risk.HIGH
    assert risk_of("run_command") is Risk.HIGH


async def test_run_command_approval_points_to_readonly_tools():
    mcp, _ = _make()
    out = await _call(mcp, "run_command", {"command": "systemctl is-active trading"})
    assert out["status"] == "approval_required"
    assert "unit_status" in out["hint"] and "run_unit" in out["hint"]


# --- unit_status / unit_logs --------------------------------------------------


async def test_unit_status_without_confirm_uses_fixed_argv():
    runner = FakeRunner(
        default=RunResult(0, "LoadState=loaded\nActiveState=active\nSubState=exited\nResult=success\nExecMainStatus=0\n", "")
    )
    mcp, _ = _make(runner)
    out = await _call(mcp, "unit_status", {"unit": "mcp-sweep"})
    assert out["status"] == "ok"
    assert out["unit"] == "mcp-sweep.service"
    assert out["active_state"] == "active" and out["result"] == "success"
    argv, _, shell = runner.calls[-1]
    assert argv[:3] == ("systemctl", "show", "mcp-sweep.service")
    assert shell is False and "sudo" not in argv


async def test_unit_status_resolves_registry_name_and_timer():
    runner = FakeRunner()
    mcp, _ = _make(runner)
    await _call(mcp, "unit_status", {"unit": "trading"})
    assert runner.calls[-1][0][2] == "trading.service"
    await _call(mcp, "unit_status", {"unit": "hosub-mcp-update.timer"})
    assert runner.calls[-1][0][2] == "hosub-mcp-update.timer"


@pytest.mark.parametrize(
    "bad",
    ["--all", "-H", "a b", "x;rm -rf /", "$(id)", "a`id`", "a|b", "../etc", "a\\b", "", "a\nb"],
)
async def test_unit_name_injection_rejected(bad):
    runner = FakeRunner()
    mcp, _ = _make(runner)
    for tool in ("unit_status", "unit_logs"):
        out = await _call(mcp, tool, {"unit": bad})
        assert out["status"] == "rejected", (tool, bad)
    assert runner.calls == []


async def test_unit_logs_since_is_bound_to_option():
    runner = FakeRunner(default=RunResult(0, "line", ""))
    mcp, _ = _make(runner)
    out = await _call(mcp, "unit_logs", {"unit": "mcp-x", "lines": 50, "since": "-2h"})
    assert out["status"] == "ok"
    argv = runner.calls[-1][0]
    assert argv[:3] == ("journalctl", "-u", "mcp-x.service")
    assert "--since=-2h" in argv
    bad = await _call(mcp, "unit_logs", {"unit": "mcp-x", "since": "x; rm -rf /"})
    assert bad["status"] == "rejected"


# --- stat_path ----------------------------------------------------------------


async def test_stat_path(tmp_path):
    f = tmp_path / "result.json"
    f.write_text("{}")
    mcp, _ = _make()
    out = await _call(mcp, "stat_path", {"path": str(f)})
    assert out["exists"] and out["type"] == "file" and out["size"] == 2
    assert out["mtime"].endswith("+09:00") and "server_time" in out
    missing = await _call(mcp, "stat_path", {"path": str(tmp_path / "nope")})
    assert missing["exists"] is False
    rel = await _call(mcp, "stat_path", {"path": "relative"})
    assert rel["status"] == "rejected"


# --- sqlite_query -------------------------------------------------------------


@pytest.fixture
def db(tmp_path, monkeypatch):
    root = tmp_path / "trading"
    root.mkdir()
    path = root / "t.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    conn.executemany("INSERT INTO t VALUES (?, ?)", [(i, f"n{i}") for i in range(20)])
    conn.commit()
    conn.close()
    monkeypatch.setenv("HOSUB_SQLITE_ROOTS", str(root))
    return path


def _count(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT count(*) FROM t").fetchone()[0]
    finally:
        conn.close()


async def test_sqlite_select(db):
    mcp, _ = _make()
    out = await _call(mcp, "sqlite_query", {"db": str(db), "sql": "SELECT id, name FROM t ORDER BY id", "max_rows": 5})
    assert out["status"] == "ok"
    assert out["columns"] == ["id", "name"]
    assert out["rows"][0] == [0, "n0"] and out["row_count"] == 5 and out["truncated"]
    w = await _call(mcp, "sqlite_query", {"db": str(db), "sql": "WITH x AS (SELECT 1 AS a) SELECT a FROM x"})
    assert w["rows"] == [[1]]
    p = await _call(mcp, "sqlite_query", {"db": str(db), "sql": "PRAGMA table_info(t)"})
    assert p["status"] == "ok" and p["row_count"] == 2


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM t",
        "INSERT INTO t VALUES (99, 'x')",
        "UPDATE t SET name = 'x'",
        "DROP TABLE t",
        "SELECT 1; DELETE FROM t",
        "WITH d AS (SELECT 1) DELETE FROM t",
        "/* x */ DELETE FROM t",
        "ATTACH DATABASE '/tmp/evil.db' AS e",
        "PRAGMA writable_schema=ON",
        "PRAGMA journal_mode=DELETE",
        "SELECT load_extension('/tmp/x.so')",
        "VACUUM INTO '/tmp/copy.db'",
        "BEGIN",
        "REPLACE INTO t VALUES (1, 'x')",
    ],
)
async def test_sqlite_write_bypass_blocked(db, sql):
    mcp, _ = _make()
    out = await _call(mcp, "sqlite_query", {"db": str(db), "sql": sql})
    assert out["status"] in ("rejected", "error"), (sql, out)
    assert _count(db) == 20


async def test_sqlite_path_outside_roots_rejected(db, tmp_path):
    other = tmp_path / "secret.db"
    sqlite3.connect(other).close()
    link = db.parent / "link.db"
    link.symlink_to(other)
    mcp, _ = _make()
    for p in (str(other), str(link), str(db.parent / ".." / "secret.db"), "rel.db"):
        out = await _call(mcp, "sqlite_query", {"db": p, "sql": "SELECT 1"})
        assert out["status"] == "rejected", p


async def test_sqlite_runaway_query_is_interrupted(db, monkeypatch):
    from src.tools import readonly

    monkeypatch.setattr(readonly, "_SQL_TIME_LIMIT", 0.3)
    mcp, _ = _make()
    sql = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT count(*) FROM c"
    t0 = time.monotonic()
    out = await _call(mcp, "sqlite_query", {"db": str(db), "sql": sql})
    assert out["status"] == "error" and "시간 제한" in out["error"]
    assert time.monotonic() - t0 < 5


# --- run_unit -----------------------------------------------------------------


async def test_run_unit_requires_confirm():
    runner = FakeRunner()
    mcp, _ = _make(runner)
    out = await _call(mcp, "run_unit", {"name": "sweep", "command": "python x.py"})
    assert out["status"] == "approval_required" and out["risk"] == "high"
    assert runner.calls == []


async def test_run_unit_builds_systemd_run():
    runner = FakeRunner(default=RunResult(0, "", ""))
    runner.responses[("systemctl", "show", "mcp-sweep.service",
                      "--property=" + __import__("src.tools.readonly", fromlist=["x"])._SHOW_PROPS,
                      "--no-pager")] = RunResult(0, "LoadState=not-found\n", "")
    mcp, _ = _make(runner)
    out = await _call(mcp, "run_unit", {
        "name": "sweep", "command": "python -m trading.sweep > /tmp/o.json",
        "workdir": "/opt/hosub-trading", "env": {"PYTHONPATH": "trading"},
        "timeout": 3600, "confirm": True,
    })
    assert out["status"] == "started" and out["unit"] == "mcp-sweep.service"
    argv = runner.calls[-1][0]
    assert argv[:3] == ("sudo", "-n", "systemd-run")
    assert "--unit=mcp-sweep.service" in argv
    assert "--property=RemainAfterExit=yes" in argv
    assert "--property=RuntimeMaxSec=3600" in argv
    assert "--working-directory=/opt/hosub-trading" in argv
    assert "--setenv=PYTHONPATH=trading" in argv
    assert "--collect" not in argv  # 끝난 뒤 상태가 사라지면 안 된다
    i = argv.index("--")
    assert argv[i + 1:] == ("bash", "-lc", "python -m trading.sweep > /tmp/o.json")
    assert runner.calls[-1][2] is False  # systemd-run 자체는 셸 없이


def _show_key(unit):
    from src.tools.readonly import _SHOW_PROPS

    return ("systemctl", "show", unit, f"--property={_SHOW_PROPS}", "--no-pager")


async def test_run_unit_rejects_while_running_and_cleans_finished():
    runner = FakeRunner(default=RunResult(0, "", ""))
    runner.responses[_show_key("mcp-a.service")] = RunResult(
        0, "LoadState=loaded\nActiveState=active\nSubState=running\n", "")
    mcp, _ = _make(runner)
    out = await _call(mcp, "run_unit", {"name": "a", "command": "x", "confirm": True})
    assert out["status"] == "rejected"
    assert not any(c[0][:3] == ("sudo", "-n", "systemd-run") for c in runner.calls)

    runner.responses[_show_key("mcp-a.service")] = RunResult(
        0, "LoadState=loaded\nActiveState=failed\nSubState=failed\n", "")
    out = await _call(mcp, "run_unit", {"name": "a", "command": "x", "confirm": True})
    assert out["status"] == "started"
    cmds = [c[0] for c in runner.calls]
    assert ("sudo", "-n", "systemctl", "stop", "mcp-a.service") in cmds
    assert ("sudo", "-n", "systemctl", "reset-failed", "mcp-a.service") in cmds


@pytest.mark.parametrize(
    "args",
    [
        {"name": "../x", "command": "x"},
        {"name": "-x", "command": "x"},
        {"name": "ok", "command": "x", "uid": "root; id"},
        {"name": "ok", "command": "x", "env": {"A B": "1"}},
        {"name": "ok", "command": "x", "env": {"A": "1\nB=2"}},
        {"name": "ok", "command": "x", "workdir": "rel"},
        {"name": "ok", "command": "  "},
    ],
)
async def test_run_unit_input_validation(args):
    runner = FakeRunner()
    mcp, _ = _make(runner)
    out = await _call(mcp, "run_unit", {**args, "confirm": True})
    assert out["status"] == "rejected", args
    assert runner.calls == []


async def test_run_unit_audit_hides_env_values():
    runner = FakeRunner(default=RunResult(0, "", ""))
    mcp, ctx = _make(runner)
    await _call(mcp, "run_unit", {"name": "s", "command": "x", "env": {"TOKEN": "sekrit"}, "confirm": True})
    assert all("sekrit" not in json.dumps(r) for r in ctx.audit.recent(10))


# --- 배포 시간창 ----------------------------------------------------------------


def _at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=KST)


def test_blackout_windows():
    spec = Registry.from_dict(REG).service("trading").deploy
    assert spec.active_blackout(_at(2026, 9, 28, 10, 0)) is not None  # 월 장중
    assert spec.active_blackout(_at(2026, 9, 28, 8, 49)) is None
    assert spec.active_blackout(_at(2026, 9, 28, 15, 40)) is None  # 끝 경계는 열림
    assert spec.active_blackout(_at(2026, 9, 28, 16, 30)) is None
    assert spec.active_blackout(_at(2026, 9, 28, 18, 0)) is not None  # 야간 배치
    assert spec.active_blackout(_at(2026, 10, 3, 10, 0)) is None  # 토요일


@pytest.mark.parametrize(
    "window",
    [
        {"days": "mon-fri", "from": "20:00", "to": "08:00"},
        {"days": "fri-mon", "from": "01:00", "to": "02:00"},
        {"days": "mon", "from": "8:00", "to": "09:00"},
        {"days": "funday", "from": "01:00", "to": "02:00"},
        {"days": "mon", "from": "01:00", "to": "02:00", "tz": "UTC"},
    ],
)
def test_blackout_validation(window):
    reg = {"services": {"s": {"unit": "s.service", "deploy": {"steps": [["x"]], "blackout": [window]}}}}
    with pytest.raises(RegistryError):
        Registry.from_dict(reg)


async def test_deploy_rejected_in_blackout_and_force_overrides(monkeypatch):
    from src.registry import DeploySpec

    monkeypatch.setattr(DeploySpec, "active_blackout",
                        lambda self, now=None: self.blackout[0] if self.blackout else None)
    runner = FakeRunner(default=RunResult(0, "", ""))
    mcp, ctx = _make(runner)
    out = await _call(mcp, "deploy_service", {"service_name": "trading", "confirm": True})
    assert out["status"] == "rejected" and "blackout" in out
    assert runner.calls == []
    # force 만으로는 안 되고 여전히 승인이 필요하다
    out = await _call(mcp, "deploy_service", {"service_name": "trading", "force": True})
    assert out["status"] == "approval_required" and "blackout" in out["action"]
    out = await _call(mcp, "deploy_service", {"service_name": "trading", "force": True, "confirm": True})
    assert out["status"] == "started"


async def test_deploy_outside_blackout_unchanged(monkeypatch):
    from src.registry import DeploySpec

    monkeypatch.setattr(DeploySpec, "active_blackout", lambda self, now=None: None)
    mcp, _ = _make(FakeRunner(default=RunResult(0, "", "")))
    out = await _call(mcp, "deploy_service", {"service_name": "trading", "confirm": True})
    assert out["status"] == "started"


# --- 잡 런스테이트 · 소실 표시 -----------------------------------------------------


class _SlowRunner:
    def run(self, argv, *, timeout, cwd=None, shell=False):
        time.sleep(0.3)
        return RunResult(0, "done", "")


def _wait(cond, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_runstate_tracks_active_jobs(tmp_path):
    path = tmp_path / "runstate.json"
    mgr = JobManager(_SlowRunner(), state_path=path, rev="a" * 40)
    data = json.loads(path.read_text())
    assert data["rev"] == "a" * 40 and data["active_jobs"] == 0
    job = mgr.submit(kind="k", label="l", steps=[Step(argv=["x"])], timeout=5)
    assert json.loads(path.read_text())["active_jobs"] >= 1
    assert _wait(lambda: mgr.get(job.id).state is JobState.SUCCEEDED)
    assert _wait(lambda: json.loads(path.read_text())["active_jobs"] == 0)


def test_restart_marks_unfinished_jobs_lost(tmp_path):
    path = tmp_path / "runstate.json"
    path.write_text(json.dumps({
        "pid": -1,
        "rev": "b" * 40,
        "active_jobs": 1,
        "jobs": [
            {"id": "done1", "kind": "k", "label": "old", "state": "succeeded",
             "created_at": "2026-09-27T01:00:00+00:00", "exit_code": 0, "output_tail": "ok"},
            {"id": "run1", "kind": "run_command", "label": "sweep", "state": "running",
             "created_at": "2026-09-27T02:00:00+00:00", "started_at": "2026-09-27T02:00:01+00:00"},
        ],
    }))
    audit = AuditLog(tmp_path / "a.db")
    mgr = JobManager(FakeRunner(), audit, state_path=path)
    assert mgr.get("done1").state is JobState.SUCCEEDED
    lost = mgr.get("run1")
    assert lost.state is JobState.LOST and lost.lost_at is not None
    assert mgr.active_count() == 0
    assert any(r["tool"] == "__jobs_lost_on_restart" for r in audit.recent(5))
    # 새 프로세스의 런스테이트에도 이력이 이어진다
    assert {j["id"] for j in json.loads(path.read_text())["jobs"]} == {"done1", "run1"}


async def test_get_job_status_reports_lost_on_restart(tmp_path):
    path = tmp_path / "runstate.json"
    path.write_text(json.dumps({"pid": -1, "jobs": [
        {"id": "run1", "kind": "k", "label": "l", "state": "running",
         "created_at": "2026-09-27T02:00:00+00:00"}]}))
    registry = Registry.from_dict({})
    audit = AuditLog(tmp_path / "a.db")
    runner = FakeRunner()
    ctx = build_context(registry, runner, audit, jobs=JobManager(runner, audit, state_path=path))
    mcp = build_mcp(ctx)
    out = await _call(mcp, "get_job_status", {"job_id": "run1"})
    assert out["status"] == "lost_on_restart" and out["lost_at"]
    unknown = await _call(mcp, "get_job_status", {"job_id": "zzz"})
    assert unknown["status"] == "unknown_job"


def test_corrupt_runstate_does_not_block_startup(tmp_path):
    path = tmp_path / "runstate.json"
    path.write_text("{not json")
    mgr = JobManager(FakeRunner(), state_path=path)
    assert mgr.list() == []
    assert json.loads(path.read_text())["active_jobs"] == 0
