"""M1 완료판정 — settings: .env 로더·masked·save_keys."""
from app import settings


def test_masked_hides_secrets(monkeypatch):
    monkeypatch.setattr(settings, "DART_API_KEY", "abcd1234efgh5678")
    monkeypatch.setattr(settings, "SLACK_BOT_TOKEN", "xoxb-secret-token-value")
    m = settings.masked()
    assert "abcd1234efgh5678" not in str(m)
    assert m["dart_key"].startswith("abcd") and m["dart_key"].endswith("5678")
    assert "…" in m["slack_token"]


def test_masked_short_value(monkeypatch):
    monkeypatch.setattr(settings, "DART_API_KEY", "short")
    assert settings.masked()["dart_key"] == "설정됨"


def test_naver_enabled_requires_both(monkeypatch):
    monkeypatch.setattr(settings, "NAVER_CLIENT_ID", "id")
    monkeypatch.setattr(settings, "NAVER_CLIENT_SECRET", "")
    assert settings.masked()["naver_enabled"] is False
    monkeypatch.setattr(settings, "NAVER_CLIENT_SECRET", "sec")
    assert settings.masked()["naver_enabled"] is True


def test_save_keys_persists_and_preserves(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TNM_DB_DSN=postgresql://x\nEXTRA=keep\n")
    monkeypatch.setattr(settings, "ENV_FILE", env)
    monkeypatch.setattr(settings, "DART_API_KEY", "")
    settings.save_keys(dart_api_key="new-dart-key-123")
    text = env.read_text()
    assert "DART_API_KEY=new-dart-key-123" in text
    assert "EXTRA=keep" in text                      # 기존 항목 보존
    assert settings.DART_API_KEY == "new-dart-key-123"
    assert (env.stat().st_mode & 0o777) == 0o600     # 권한 600


def test_save_keys_rejects_unknown():
    try:
        settings.save_keys(internal_token="hack")    # 허용 목록 밖
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_save_keys_empty_is_noop(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    monkeypatch.setattr(settings, "ENV_FILE", env)
    monkeypatch.setattr(settings, "DART_API_KEY", "orig")
    settings.save_keys(dart_api_key="")              # 빈값 = 변경 없음
    assert settings.DART_API_KEY == "orig"
