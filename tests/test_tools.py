"""서버 전체 제어 도구(shell/files)의 confirm 게이트와 동작 검증.

FastMCP 등록 도구를 직접 호출하기 위해 register 로 등록된 함수를
mcp._tool_manager 에서 꺼내 실행한다.
"""

from __future__ import annotations

import json
import tempfile
import time

import pytest

from src.audit import AuditLog
from src.jobs import JobState
from src.registry import Registry
from src.runner import RunResult
from src.server import build_context, build_mcp
from tests.conftest import FakeRunner

REG = {"scripts": {"daily_backup": {"path": "/opt/x.sh"}}, "backup_script": "daily_backup"}


def _make(runner=None):
    reg = Registry.from_dict(REG)
    audit = AuditLog(tempfile.mktemp(suffix=".db"))
    ctx = build_context(reg, runner or FakeRunner(), audit)
    mcp = build_mcp(ctx)
    return mcp, ctx


async def _call(mcp, name, args):
    result = await mcp.call_tool(name, args)
    # dict 반환 도구는 TextContent(JSON) 리스트로 변환되어 온다
    if isinstance(result, list) and result and hasattr(result[0], "text"):
        return json.loads(result[0].text)
    if isinstance(result, dict):
        return result
    raise AssertionError(f"unexpected tool result: {result!r}")


@pytest.mark.asyncio
async def test_run_command_requires_confirm():
    mcp, _ = _make()
    out = await _call(mcp, "run_command", {"command": "ls"})
    assert out["status"] == "approval_required"
    assert out["risk"] == "high"


@pytest.mark.asyncio
async def test_run_command_sync_executes_with_confirm():
    runner = FakeRunner(default=RunResult(0, "hello", ""))
    mcp, ctx = _make(runner)
    out = await _call(mcp, "run_command", {"command": "echo hello", "confirm": True})
    assert out["status"] == "ok"
    assert out["exit_code"] == 0
    assert "hello" in out["output"]
    # shell=True 로 bash -lc 경유
    assert runner.calls[-1][2] is True


@pytest.mark.asyncio
async def test_run_command_background_returns_job():
    runner = FakeRunner(default=RunResult(0, "done", ""))
    mcp, ctx = _make(runner)
    out = await _call(
        mcp, "run_command", {"command": "sleep 0", "confirm": True, "background": True}
    )
    assert out["status"] == "started"
    job_id = out["job_id"]
    # 잡 완료 대기
    for _ in range(200):
        job = ctx.jobs.get(job_id)
        if job and job.state in (JobState.SUCCEEDED, JobState.FAILED, JobState.TIMEOUT):
            break
        time.sleep(0.01)
    assert ctx.jobs.get(job_id).state is JobState.SUCCEEDED


@pytest.mark.asyncio
async def test_write_file_requires_confirm(tmp_path):
    mcp, _ = _make()
    target = tmp_path / "f.txt"
    out = await _call(mcp, "write_file", {"path": str(target), "content": "x"})
    assert out["status"] == "approval_required"
    assert not target.exists()


@pytest.mark.asyncio
async def test_write_file_creates_backup(tmp_path):
    mcp, _ = _make()
    target = tmp_path / "f.txt"
    target.write_text("original")
    out = await _call(
        mcp, "write_file", {"path": str(target), "content": "new", "confirm": True}
    )
    assert out["status"] == "ok"
    assert target.read_text() == "new"
    assert (tmp_path / "f.txt.bak").read_text() == "original"


@pytest.mark.asyncio
async def test_write_file_append(tmp_path):
    mcp, _ = _make()
    target = tmp_path / "f.txt"
    target.write_text("a")
    await _call(
        mcp,
        "write_file",
        {"path": str(target), "content": "b", "mode": "append", "confirm": True},
    )
    assert target.read_text() == "ab"


@pytest.mark.asyncio
async def test_read_file_binary(tmp_path):
    mcp, _ = _make()
    target = tmp_path / "b.bin"
    target.write_bytes(b"\xff\xfe\x00\x01")
    out = await _call(mcp, "read_file", {"path": str(target)})
    assert out["status"] == "ok"
    assert out["binary"] is True


@pytest.mark.asyncio
async def test_read_file_relative_rejected():
    mcp, _ = _make()
    out = await _call(mcp, "read_file", {"path": "relative.txt"})
    assert out["status"] == "rejected"


@pytest.mark.asyncio
async def test_list_directory(tmp_path):
    mcp, _ = _make()
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "sub").mkdir()
    out = await _call(mcp, "list_directory", {"path": str(tmp_path)})
    assert out["status"] == "ok"
    names = {e["name"]: e["type"] for e in out["entries"]}
    assert names["a.txt"] == "file"
    assert names["sub"] == "dir"


@pytest.mark.asyncio
async def test_run_script_rejects_unknown():
    mcp, _ = _make()
    out = await _call(mcp, "run_script", {"script_name": "nope", "confirm": True})
    assert out["status"] == "rejected"
    assert "daily_backup" in out["known_scripts"]
