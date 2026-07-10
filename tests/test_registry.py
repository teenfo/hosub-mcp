import pytest

from src.registry import Registry, RegistryError

VALID = {
    "services": {
        "bcl-portal": {
            "unit": "bcl-portal.service",
            "description": "웹",
            "deploy": {
                "workdir": "/opt/bcl",
                "steps": [["git", "pull"], ["docker", "compose", "up", "-d"]],
                "restart_after": True,
                "timeout_seconds": 100,
            },
        },
        "ollama": {"unit": "ollama.service"},
    },
    "scripts": {
        "daily_backup": {"path": "/opt/scripts/backup.sh", "timeout_seconds": 60},
    },
    "backup_script": "daily_backup",
}


def test_valid_load():
    reg = Registry.from_dict(VALID)
    assert reg.service_names == ["bcl-portal", "ollama"]
    assert reg.script("daily_backup").timeout_seconds == 60
    assert reg.backup().name == "daily_backup"
    svc = reg.service("bcl-portal")
    assert svc.deploy.restart_after is True
    assert svc.deploy.steps[0] == ("git", "pull")


def test_unknown_top_key():
    with pytest.raises(RegistryError):
        Registry.from_dict({"servces": {}})


def test_relative_script_path_rejected():
    with pytest.raises(RegistryError):
        Registry.from_dict({"scripts": {"x": {"path": "relative/path.sh"}}})


def test_bad_unit_name():
    with pytest.raises(RegistryError):
        Registry.from_dict({"services": {"x": {"unit": "notaservice"}}})


def test_deploy_shell_string_rejected():
    bad = {
        "services": {
            "x": {"unit": "x.service", "deploy": {"steps": ["git pull && make"]}}
        }
    }
    with pytest.raises(RegistryError):
        Registry.from_dict(bad)


def test_deploy_empty_steps_rejected():
    bad = {"services": {"x": {"unit": "x.service", "deploy": {"steps": []}}}}
    with pytest.raises(RegistryError):
        Registry.from_dict(bad)


def test_backup_script_must_exist():
    bad = {"scripts": {"a": {"path": "/x.sh"}}, "backup_script": "missing"}
    with pytest.raises(RegistryError):
        Registry.from_dict(bad)


def test_unknown_service_returns_none():
    reg = Registry.from_dict(VALID)
    assert reg.service("nope") is None
    assert reg.script("nope") is None


def test_strict_mode_missing_path(tmp_path):
    reg_dict = {"scripts": {"a": {"path": str(tmp_path / "nope.sh")}}}
    with pytest.raises(RegistryError):
        Registry.from_dict(reg_dict, strict=True)
