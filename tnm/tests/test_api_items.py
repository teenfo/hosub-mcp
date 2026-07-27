"""M6 완료판정 — 조회·라벨 API: 필터 위임·404·verdict 검증."""
import pytest
from fastapi.testclient import TestClient

from app import db, main, settings


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(settings, "INTERNAL_TOKEN", "test-token")
    monkeypatch.setattr(db, "ready", True)
    return TestClient(main.app)


AUTH = {"X-Internal-Token": "test-token"}


def test_items_filters_passed_through(client, monkeypatch):
    captured = {}

    async def fake_list(date, ticker, min_score, status, novelty, limit, offset):
        captured.update(date=date, ticker=ticker, min_score=min_score,
                        status=status, novelty=novelty, limit=limit, offset=offset)
        return [{"id": 1, "title": "t"}]

    monkeypatch.setattr(db, "list_items", fake_list)
    r = client.get("/api/items?date=2026-07-26&ticker=005930&min_score=60"
                   "&status=ok&limit=50", headers=AUTH)
    assert r.status_code == 200 and len(r.json()["items"]) == 1
    assert captured == {"date": "2026-07-26", "ticker": "005930", "min_score": 60,
                        "status": "ok", "novelty": None, "limit": 50, "offset": 0}


def test_items_offset_is_passed_through(client, monkeypatch):
    """소급 측정이 전량을 읽으려면 페이지를 넘겨야 한다 — limit 상한이 500이다."""
    captured = {}

    async def fake_list(date, ticker, min_score, status, novelty, limit, offset):
        captured.update(limit=limit, offset=offset)
        return []

    monkeypatch.setattr(db, "list_items", fake_list)
    assert client.get("/api/items?limit=500&offset=1000",
                      headers=AUTH).status_code == 200
    assert captured == {"limit": 500, "offset": 1000}


def test_item_detail_404(client, monkeypatch):
    async def fake_get(analysis_id):
        return None

    monkeypatch.setattr(db, "get_item", fake_get)
    assert client.get("/api/items/999", headers=AUTH).status_code == 404


def test_label_validates_verdict(client, monkeypatch):
    calls = []

    async def fake_label(analysis_id, verdict, note):
        calls.append((analysis_id, verdict, note))
        return True

    monkeypatch.setattr(db, "upsert_label", fake_label)
    ok = client.post("/api/items/7/label",
                     json={"verdict": "important", "note": "핵심 공시"}, headers=AUTH)
    assert ok.status_code == 200
    bad = client.post("/api/items/7/label", json={"verdict": "buy"}, headers=AUTH)
    assert bad.status_code == 400                       # P2: 매매 판단 라벨 없음
    assert calls == [(7, "important", "핵심 공시")]


def test_label_missing_item_404(client, monkeypatch):
    async def fake_label(analysis_id, verdict, note):
        return False

    monkeypatch.setattr(db, "upsert_label", fake_label)
    r = client.post("/api/items/999/label", json={"verdict": "noise"}, headers=AUTH)
    assert r.status_code == 404


def test_items_require_auth(client):
    assert client.get("/api/items").status_code == 401
