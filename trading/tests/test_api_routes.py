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


# --- 계좌 실현손익 대조 ---

def test_realized_requires_auth(client):
    assert client.get("/api/account/realized").status_code == 401


def test_realized_shape_without_api_key(client, monkeypatch, tmp_path):
    """키가 없어도 500 이 아니라 사유를 돌려주고, 원장 쪽은 그대로 낸다."""
    from app import main
    from app.trade import ledger

    monkeypatch.setattr(main.settings, "KIWOOM_APP_KEY", "")
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "t.db")
    d = _get(client, "/api/account/realized").json()
    assert {"period", "broker", "engine", "untracked", "unconfirmed"} <= set(d)
    assert d["broker"]["ok"] is False
    assert d["untracked"] is None          # 계좌 값이 없으면 차이도 없다
    assert d["engine"]["realized"] == 0


def test_realized_puts_broker_and_engine_side_by_side(client, monkeypatch, tmp_path):
    """이 라우트의 존재 이유 — 둘을 합치지 않고 차이를 이름 붙여 낸다."""
    from app import main
    from app.kiwoom.client import client as kc
    from app.trade import ledger

    monkeypatch.setattr(main.settings, "KIWOOM_APP_KEY", "K")
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "t.db")
    main._realized_cache.update(ts=0.0, data=None)

    async def fake(start, end):
        return {"return_code": 0, "rlzt_pl": "181019", "trde_cmsn": "890",
                "trde_tax": "3497", "dt_rlzt_pl": []}

    monkeypatch.setattr(kc, "daily_realized", fake)
    ledger.open_position({"id": "p1", "symbol": "477850", "rule": "orb",
                          "side": "long", "qty": 4, "entry": 26_850,
                          "stop": 26_000, "target": 28_000}, fill=26_850)
    ledger.close_position("p1", 27_750, "target")

    d = _get(client, "/api/account/realized").json()
    assert d["broker"]["realized"] == 181_019
    # 차이 = 계좌 − 엔진. 실측에서 이 값의 정체가 '원장에 없는 수동매매' 였다
    assert d["untracked"] == round(181_019 - d["engine"]["realized"], 0)
    assert d["engine"]["trades"] == 1


def test_realized_gives_today_row_for_the_screen(client, monkeypatch, tmp_path):
    """화면의 '오늘 실현손익' 은 이 값을 쓴다 — 원장 합산이 아니라 증권사 실측.

    2026-07-28 실측: 모델 비용이 손실을 1,796원 과대계상했고 가드가 그 값으로
    판정했다. 금액은 증권사 값을 보여주고 원장값은 대조로 나란히 둔다.
    """
    from datetime import datetime

    from app import main
    from app.kiwoom.client import client as kc
    from app.trade import ledger

    monkeypatch.setattr(main.settings, "KIWOOM_APP_KEY", "K")
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "t.db")
    main._realized_cache.update(ts=0.0, data=None)
    ymd = datetime.now(main.KST).strftime("%Y%m%d")

    async def fake(start, end):
        return {"return_code": 0, "rlzt_pl": "-10442", "dt_rlzt_pl": [
            {"dt": ymd, "tdy_sel_pl": "-10442", "tdy_trde_cmsn": "230"},
            {"dt": "20260728", "tdy_sel_pl": "1234"}]}

    monkeypatch.setattr(kc, "daily_realized", fake)
    d = _get(client, "/api/account/realized").json()
    assert d["today"]["realized"] == -10_442, "당일 행을 골라내야 화면이 쓴다"
    assert d["today"]["date"] == ymd


def test_realized_today_is_none_when_broker_is_unreachable(client, monkeypatch, tmp_path):
    """조회 실패면 None — 화면은 그때만 원장값으로 폴백하고 그 사실을 표시한다."""
    from app import main
    from app.trade import ledger

    monkeypatch.setattr(main.settings, "KIWOOM_APP_KEY", "")
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "t.db")
    main._realized_cache.update(ts=0.0, data=None)
    assert _get(client, "/api/account/realized").json()["today"] is None


def test_status_exposes_desk_metrics(client):
    """WS 대비 REST 비율이 이 설계의 성적표다 — 화면에 보이지 않으면 못 잰다."""
    d = _get(client, "/api/status").json()
    assert {"enabled", "ws", "rest", "cycles", "interval_sec"} <= set(d["desk"])


def test_desk_can_be_switched_off_without_a_deploy(client, monkeypatch, tmp_path):
    """초 단위로 실거래 청산을 내는 루프다 — 배포를 기다려서 끌 수는 없다."""
    from app.trade import desk

    monkeypatch.setattr(desk, "STATE_FILE", tmp_path / "desk.json")
    monkeypatch.setitem(desk.settings.CONFIG, "execution", {"desk": {"enabled": True}})

    r = client.post("/api/desk", json={"enabled": False},
                    headers={"X-Internal-Token": TOKEN})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["desk"]["enabled"] is False
    assert _get(client, "/api/status").json()["desk"]["enabled"] is False


def test_desk_requires_auth(client):
    assert client.post("/api/desk", json={"enabled": True}).status_code == 401


def test_reconcile_without_api_key_is_not_an_error_page(client, monkeypatch):
    from app import main

    monkeypatch.setattr(main.settings, "KIWOOM_APP_KEY", "")
    r = client.post("/api/account/reconcile", headers={"X-Internal-Token": TOKEN})
    assert r.status_code == 200 and r.json()["ok"] is False
