"""M1 완료판정 — API: 인증, 종목 등록→조회 정상 반환, 편집·검증."""
import pytest
from fastapi.testclient import TestClient

from app import db, main, settings


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(settings, "INTERNAL_TOKEN", "test-token")
    monkeypatch.setattr(db, "ready", True)
    # lifespan 미실행 TestClient — DB 루프 없이 API 만 검증
    return TestClient(main.app)


AUTH = {"X-Internal-Token": "test-token"}


def test_requires_token(client):
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/watch", headers={"X-Internal-Token": "wrong"}).status_code == 401


def test_status_ok(client, monkeypatch):
    async def fake_stats():
        return {"embed_pending": 0, "classify_pending": 0, "llm_failed": 0, "raw_total": 0}

    monkeypatch.setattr(db, "queue_stats", fake_stats)
    r = client.get("/api/status", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["db_ready"] is True and body["shadow_mode"] is True
    assert body["queue"]["raw_total"] == 0


def test_watch_register_then_list(client, monkeypatch):
    store: dict[str, dict] = {}

    async def fake_add(ticker, name):
        row = {"ticker": ticker, "name": name, "origin": "manual",
               "is_active": True, "is_excluded": False}
        store[ticker] = row
        return row

    async def fake_list(include_inactive=True):
        return list(store.values())

    monkeypatch.setattr(db, "add_manual", fake_add)
    monkeypatch.setattr(db, "list_watch", fake_list)

    r = client.post("/api/watch", json={"ticker": "005930", "name": "삼성전자"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["entry"]["origin"] == "manual"
    r = client.get("/api/watch", headers=AUTH)
    assert [e["ticker"] for e in r.json()["entries"]] == ["005930"]   # 등록 후 조회 반환


def test_watch_add_validates_ticker(client):
    assert client.post("/api/watch", json={"ticker": "12ab"}, headers=AUTH).status_code == 400
    assert client.post("/api/watch", json={}, headers=AUTH).status_code == 400


def test_watch_settings_validation(client, monkeypatch):
    async def fake_set(ticker, threshold, cap):
        return True

    monkeypatch.setattr(db, "set_alert_settings", fake_set)
    ok = client.post("/api/watch/005930/settings",
                     json={"score_threshold": 70}, headers=AUTH)
    assert ok.status_code == 200
    bad = client.post("/api/watch/005930/settings",
                      json={"score_threshold": 150}, headers=AUTH)
    assert bad.status_code == 400


def test_watch_exclude_include_roundtrip(client, monkeypatch):
    """exclude/include 위임·404. 참고: 복원(include)된 auto 행이 소스에 없으면
    다음 동기화가 다시 비활성화한다 — 영구 유지가 필요하면 수동 등록(manual 승격)."""
    calls = []

    async def fake_set(ticker, excluded):
        calls.append((ticker, excluded))
        return ticker == "005930"

    monkeypatch.setattr(db, "set_excluded", fake_set)
    assert client.post("/api/watch/005930/exclude", headers=AUTH).status_code == 200
    assert client.post("/api/watch/005930/include", headers=AUTH).status_code == 200
    assert client.post("/api/watch/999999/exclude", headers=AUTH).status_code == 404
    assert calls == [("005930", True), ("005930", False), ("999999", True)]


def test_watch_db_not_ready(client, monkeypatch):
    monkeypatch.setattr(db, "ready", False)
    assert client.get("/api/watch", headers=AUTH).status_code == 503


def test_settings_masked_no_secret_leak(client, monkeypatch):
    monkeypatch.setattr(settings, "DART_API_KEY", "super-secret-dart-key")
    r = client.get("/api/settings", headers=AUTH)
    assert r.status_code == 200
    assert "super-secret-dart-key" not in r.text


def test_settings_save_rejects_unknown_keys(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ENV_FILE", tmp_path / ".env")
    r = client.post("/api/settings",
                    json={"dart_api_key": "new-key-12345", "internal_token": "hack"},
                    headers=AUTH)
    assert r.status_code == 200
    assert settings.DART_API_KEY == "new-key-12345"
    env_text = (tmp_path / ".env").read_text()
    assert "hack" not in env_text                      # 허용 목록 밖 키는 무시됨
