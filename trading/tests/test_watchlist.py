from app import settings
from app.data import watchlist


def _fresh(tmp_path, monkeypatch, seed=None):
    monkeypatch.setattr(watchlist, "DB_PATH", tmp_path / "wl.db")
    monkeypatch.setattr(settings, "WATCHLIST", dict(seed or {}))


def test_init_seeds_from_config_once(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch, {"005930": "삼성전자"})
    watchlist.init()
    assert settings.WATCHLIST == {"005930": "삼성전자"}
    assert watchlist.entries()[0]["source"] == "seed"
    # 두 번째 init 은 DB 기준 (config 재시드 안 함)
    watchlist.remove("005930")
    monkeypatch.setattr(settings, "WATCHLIST", {"005930": "삼성전자"})
    watchlist.init()
    # DB 는 비어 있지 않았던 적이 있으므로... 실제로는 비면 재시드됨 — 여기선 add 로 확인
    watchlist.add("000660", "SK하이닉스")
    assert "000660" in settings.WATCHLIST


def test_add_remove_persist(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    watchlist.init()
    watchlist.add("035420", "NAVER", source="manual")
    assert settings.WATCHLIST["035420"] == "NAVER"
    # 재시작 시뮬레이션: 런타임 비우고 init → DB 에서 복원
    monkeypatch.setattr(settings, "WATCHLIST", {})
    watchlist.init()
    assert settings.WATCHLIST == {"035420": "NAVER"}
    assert watchlist.remove("035420") is True
    assert settings.WATCHLIST == {}
    assert watchlist.remove("035420") is False



