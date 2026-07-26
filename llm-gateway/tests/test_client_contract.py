"""소비자 자산(클라이언트·목 서버)이 실제 게이트웨이와 어긋나지 않는지 검사한다.

소비 프로젝트는 목 서버로 코드를 짜고 나중에 실서버로 옮긴다. 그 사이에 응답
모양이 달라지면 목에서 잘 돌던 코드가 실서버에서 깨진다 — 여기서 막는다.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


llmgw = _load("llmgw", ROOT / "client" / "llmgw.py")
mock_gateway = _load("mock_gateway", ROOT / "tools" / "mock_gateway.py")


SAMPLE_JOB = {
    "id": "abc123", "service": "roxlogy", "role": "analyze_workout",
    "model": "qwen2.5:14b", "lane": "batch", "status": "queued", "priority": 0,
    "prompt": "p", "system": None, "response": None, "error": None,
    "metadata": {}, "attempts": 1,
    "created_at": "2026-01-01T00:00:00+00:00", "started_at": None,
    "finished_at": None, "prompt_chars": 1,
}


def test_mock_response_shape_matches_real_gateway():
    """목과 실서버의 잡 응답 키가 완전히 같아야 한다."""
    from app.main import _job_response

    real = set(_job_response(SAMPLE_JOB, queue_position=0))
    mock = set(mock_gateway.shape(SAMPLE_JOB))
    assert mock == real, f"목에만: {mock - real} / 실서버에만: {real - mock}"


def test_client_parses_real_gateway_response():
    """클라이언트의 Job 이 실서버 응답을 그대로 받아들인다."""
    from app.main import _job_response

    job = llmgw.Job.from_dict(_job_response(SAMPLE_JOB, queue_position=3))
    assert job.job_id == "abc123"
    assert job.status == "pending" and job.pending and not job.done
    assert job.role == "analyze_workout" and job.model == "qwen2.5:14b"
    assert job.queue_position == 3

    done = dict(SAMPLE_JOB, status="succeeded", response="결과")
    j2 = llmgw.Job.from_dict(_job_response(done))
    assert j2.ok and j2.done and j2.response == "결과"

    bad = dict(SAMPLE_JOB, status="failed", error="백엔드 오류")
    j3 = llmgw.Job.from_dict(_job_response(bad))
    assert not j3.ok and j3.done and j3.error == "백엔드 오류"


def test_client_ignores_unknown_fields():
    """서버가 필드를 추가해도 클라이언트가 죽지 않아야 한다(전방 호환)."""
    job = llmgw.Job.from_dict({"job_id": "x", "status": "ok", "brand_new_field": 1})
    assert job.job_id == "x" and job.ok


def test_mock_role_names_match_real_config():
    """목이 실제 roles.yaml 을 읽으므로 역할 이름이 어긋나지 않는다."""
    import yaml

    raw = yaml.safe_load((ROOT / "config" / "roles.yaml").read_text(encoding="utf-8"))
    assert set(mock_gateway.load_roles()) == set(raw["roles"])


def test_mock_builtin_roles_are_a_safe_fallback():
    """roles.yaml 을 못 읽는 환경에서도 쓸 만한 기본값이 있어야 한다."""
    assert "summarize" in mock_gateway.BUILTIN_ROLES
    for name, (model, lane, timeout) in mock_gateway.BUILTIN_ROLES.items():
        assert model and lane in ("interactive", "batch") and timeout > 0


@pytest.mark.parametrize("status,exc", [
    (401, llmgw.AuthError), (403, llmgw.AuthError),
    (404, llmgw.RoleError), (413, llmgw.RoleError),
    (429, llmgw.GatewayError), (500, llmgw.GatewayError),
])
def test_client_maps_error_codes_to_useful_exceptions(status, exc):
    """호출자가 '설정 문제'와 '일시적 문제'를 구분할 수 있어야 한다."""
    import httpx

    res = httpx.Response(status, json={"error": "x", "detail": "상세"})
    with pytest.raises(exc):
        llmgw._raise_for_status(res)


def test_client_requires_a_token_up_front():
    with pytest.raises(llmgw.AuthError, match="LLMGW_TOKEN"):
        llmgw._settings(None, None)


def test_client_reads_env_settings(monkeypatch):
    monkeypatch.setenv("LLMGW_URL", "http://gw:8603/")
    monkeypatch.setenv("LLMGW_TOKEN", "tok")
    assert llmgw._settings(None, None) == ("http://gw:8603", "tok")
