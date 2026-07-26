import pytest

from app.config import ConfigError, RoleConfig, ServiceConfig
from tests.conftest import ROLES_RAW, SERVICES_RAW


def test_roles_load(roles):
    assert roles.role_names == ["bulk", "chat", "fast", "heavy", "tiny"]
    assert roles.role("fast").lane == "interactive"
    assert roles.role("heavy").lane == "batch"
    assert roles.backend.base_url == "http://mac.test:11434"
    assert roles.mem_budget_gb == 40


def test_backend_url_from_env(monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", "http://192.168.0.9:11434")
    raw = dict(ROLES_RAW, backend={"base_url": "${OLLAMA_URL}"})
    cfg = RoleConfig.from_dict(raw)
    assert cfg.backend.base_url == "http://192.168.0.9:11434"


def test_missing_backend_url_rejected():
    raw = dict(ROLES_RAW, backend={})
    with pytest.raises(ConfigError):
        RoleConfig.from_dict(raw)


def test_bad_lane_rejected():
    raw = {**ROLES_RAW, "roles": {"x": {"model": "m", "lane": "nope"}}}
    with pytest.raises(ConfigError):
        RoleConfig.from_dict(raw)


def test_missing_model_rejected():
    raw = {**ROLES_RAW, "roles": {"x": {"lane": "batch"}}}
    with pytest.raises(ConfigError):
        RoleConfig.from_dict(raw)


def test_model_size_lookup_and_estimate(roles):
    assert roles.model_size_gb("small") == 5          # 설정 테이블
    # 미상 모델은 태그의 파라미터 수로 추정
    assert 5 < roles.model_size_gb("qwen2.5:14b") < 15
    # 완전 미상이면 보수적으로 큰 값
    assert roles.model_size_gb("mystery-model") == 20.0


def test_services_load(services):
    assert services.names == ["alpha", "beta", "slow"]
    assert services.get("beta").may_use("fast") is True
    assert services.get("beta").may_use("heavy") is False
    assert services.get("alpha").may_use("heavy") is True   # "*"


def test_service_without_token_env_rejected():
    with pytest.raises(ConfigError):
        ServiceConfig.from_dict({"services": {"x": {"allow_roles": ["*"]}}})


def test_shipped_config_is_valid(monkeypatch):
    """저장소에 커밋된 실제 설정이 유효해야 한다."""
    monkeypatch.setenv("OLLAMA_URL", "http://192.168.0.9:11434")
    monkeypatch.setenv("MEM_BUDGET_GB", "40")
    cfg = RoleConfig.load("config/roles.yaml")
    assert "summarize" in cfg.role_names
    assert "analyze_workout" in cfg.role_names
    # roxlogy 역할은 프롬프트를 호출자가 소유 → 기본 system 없음
    assert cfg.role("analyze_workout").system is None
    # hosub 운영 역할은 기본 system 보유
    assert cfg.role("summarize").system

    svc = ServiceConfig.load("config/services.yaml")
    assert "roxlogy" in svc.names
    assert svc.get("roxlogy").may_use("analyze_workout") is True
    assert svc.get("roxlogy").may_use("code") is False
