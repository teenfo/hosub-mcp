from src.audit import AuditLog


def test_log_and_recent(tmp_path):
    a = AuditLog(tmp_path / "a.db")
    a.log(tool="get_system_status", outcome="ok", risk="low")
    a.log(
        tool="run_command",
        params={"command": "ls"},
        confirm=True,
        risk="high",
        outcome="succeeded",
        result_summary="파일 목록",
        job_id="abc123",
    )
    rows = a.recent(10)
    assert len(rows) == 2
    # 최신순
    assert rows[0]["tool"] == "run_command"
    assert rows[0]["confirm"] == 1
    assert rows[0]["job_id"] == "abc123"
    assert rows[1]["tool"] == "get_system_status"


def test_summary_truncated(tmp_path):
    a = AuditLog(tmp_path / "a.db")
    a.log(tool="run_command", outcome="ok", result_summary="x" * 2000)
    row = a.recent(1)[0]
    assert len(row["result_summary"]) <= 601


def test_unserializable_params(tmp_path):
    a = AuditLog(tmp_path / "a.db")
    a.log(tool="x", params={"f": object()}, outcome="ok")  # default=str 로 처리
    assert a.recent(1)[0]["tool"] == "x"
