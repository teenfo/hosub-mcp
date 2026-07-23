from app import settings


def test_masked_hides_secret(monkeypatch):
    monkeypatch.setattr(settings, "KIWOOM_APP_KEY", "ABCDEFGHIJKLMNOP")
    monkeypatch.setattr(settings, "KIWOOM_SECRET_KEY", "supersecret-value")
    monkeypatch.setattr(settings, "KIWOOM_ACCOUNT", "12345678")
    m = settings.masked()
    assert m["app_key_masked"] == "ABCD…MNOP"
    assert "supersecret" not in str(m)
    assert m["has_secret"] is True
    assert m["account_masked"] == "설정됨"  # 10자 미만은 부분 노출도 하지 않음


def test_apply_keys_switches_env(monkeypatch):
    monkeypatch.setattr(settings, "KIWOOM_ENV", "mock")
    settings.apply_keys(env="real")
    assert settings.KIWOOM_ENV == "real"
    assert settings.REST_BASE == "https://api.kiwoom.com"
    settings.apply_keys(env="mock")
    assert settings.REST_BASE == "https://mockapi.kiwoom.com"


def test_apply_keys_rejects_bad_env():
    try:
        settings.apply_keys(env="prod")
        raise AssertionError("ValueError 가 나야 함")
    except ValueError:
        pass


def test_save_keys_persists_and_keeps_other_vars(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("DASH_PASSWORD=keep-me\nKIWOOM_ENV=mock\n")
    monkeypatch.setattr(settings, "ENV_FILE", env_file)
    settings.save_keys(env="mock", app_key="new-app-key", secret_key="new-secret")
    text = env_file.read_text()
    assert "DASH_PASSWORD=keep-me" in text          # 기존 항목 유지
    assert "KIWOOM_APP_KEY=new-app-key" in text
    assert "KIWOOM_SECRET_KEY=new-secret" in text
    assert oct(env_file.stat().st_mode & 0o777) == "0o600"


def test_empty_values_mean_no_change(monkeypatch):
    monkeypatch.setattr(settings, "KIWOOM_APP_KEY", "existing")
    settings.apply_keys(app_key=None)
    assert settings.KIWOOM_APP_KEY == "existing"
