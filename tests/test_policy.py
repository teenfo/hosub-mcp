from src.policy import Risk, check_confirm, risk_of


def test_low_passes_without_confirm():
    assert check_confirm("get_system_status", False, "조회") is None
    assert check_confirm("read_file", False, "읽기") is None


def test_medium_blocked_without_confirm():
    payload = check_confirm("restart_service", False, "systemctl restart x")
    assert payload is not None
    assert payload["status"] == "approval_required"
    assert payload["risk"] == "medium"
    assert payload["action"] == "systemctl restart x"
    assert "confirm=true" in payload["message"]


def test_high_blocked_without_confirm():
    payload = check_confirm("run_command", False, "rm -rf /tmp/x")
    assert payload is not None
    assert payload["risk"] == "high"


def test_confirm_true_passes():
    assert check_confirm("run_command", True, "무엇이든") is None
    assert check_confirm("write_file", True, "쓰기") is None


def test_unknown_tool_defaults_high():
    assert risk_of("mystery_tool") is Risk.HIGH
    assert check_confirm("mystery_tool", False, "x") is not None
