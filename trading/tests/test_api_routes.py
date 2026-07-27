"""API 경로 스모크 — 라우트가 실제로 200 을 내는가.

단위 테스트가 다 통과해도 라우트 안의 한 줄이 틀리면 배포하고서야 안다.
실측 2026-07-27: `/api/scout` 이 `scout.snapshot_current()` 를 불렀는데 그건
Engine 의 메서드가 아니라 모듈 함수라 500 이 났다. 단위 테스트는 전부 초록이었다.

여기서 잰다: **인증이 걸리는가 · 라우트가 200 을 내는가 · 응답 스키마의 키가
화면이 기대하는 것과 같은가.** 매매를 일으키는 경로는 건드리지 않는다.
"""
import pytest
from fastapi.testclient import TestClient

from app import settings

TOKEN = "test-internal-token"


@pytest.fixture
def client(monkeypatch, tmp_path):
    """lifespan 을 띄우지 않는다 — 실제 루프·키움 연결을 시작하지 않기 위해서다."""
    monkeypatch.setattr(settings, "INTERNAL_TOKEN", TOKEN)
    from app import main
    from app.scout import engine as scout_eng
    from app.scout import store as scout_store

    monkeypatch.setattr(main.settings, "INTERNAL_TOKEN", TOKEN)
    monkeypatch.setattr(scout_store, "DB_PATH", tmp_path / "scout.db")
    monkeypatch.setattr(scout_eng, "STATE_FILE", tmp_path / "engine.json")
    return TestClient(main.app)


def _get(client, path):
    return client.get(path, headers={"X-Internal-Token": TOKEN})


def test_scout_requires_auth(client):
    assert client.get("/api/scout").status_code == 401


def test_scout_returns_the_shape_the_screen_expects(client):
    r = _get(client, "/api/scout")
    assert r.status_code == 200, r.text
    d = r.json()
    assert {"status", "candidates", "pending", "watchlist", "decisions"} <= set(d)
    st = d["status"]
    assert st["mode"] == "shadow"
    assert {"frozen", "ready", "sources", "thresholds", "max_score"} <= set(st)
    assert {s["name"] for s in st["sources"]}      # 소스 목록이 비어 있지 않다


def test_scout_mode_switch_and_freeze(client):
    r = client.post("/api/scout/mode", json={"mode": "collect"},
                    headers={"X-Internal-Token": TOKEN})
    assert r.status_code == 200 and r.json()["state"]["mode"] == "collect"
    r = client.post("/api/scout/mode", json={"frozen": True},
                    headers={"X-Internal-Token": TOKEN})
    assert r.json()["state"]["frozen"] is True
    assert _get(client, "/api/scout").json()["status"]["frozen"] is True


def test_scout_mode_rejects_unknown(client):
    r = client.post("/api/scout/mode", json={"mode": "없는모드"},
                    headers={"X-Internal-Token": TOKEN})
    assert r.status_code == 400 and r.json()["ok"] is False


@pytest.mark.parametrize("path", [
    "/api/research/event-study", "/api/research/ranking",
    "/api/backtest/report/latest", "/api/backtest/sweep/latest",
    "/api/regime/history",
])
def test_research_routes_respond(client, path):
    """결과 파일이 없어도 예외가 아니라 사유를 돌려줘야 한다."""
    assert _get(client, path).status_code == 200


def test_regime_history_shape(client, monkeypatch, tmp_path):
    """이력이 아직 비어 있어도 화면이 기대하는 키가 다 있어야 한다."""
    from app.data import regime_log

    monkeypatch.setattr(regime_log, "DB_PATH", tmp_path / "r.db")
    d = _get(client, "/api/regime/history").json()
    assert {"daily", "recent", "score", "signals", "min_days"} <= set(d)
    assert set(d["signals"]) == set(regime_log.SIGNALS)
