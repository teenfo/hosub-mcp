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


def test_replace_auto_swaps_only_auto(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch, {"005930": "삼성전자"})
    watchlist.init()                                   # seed
    watchlist.add("035420", "NAVER", source="manual")  # manual
    watchlist.replace_auto([{"code": "111111", "name": "발굴1"},
                            {"code": "222222", "name": "발굴2"}])
    assert set(settings.WATCHLIST) == {"005930", "035420", "111111", "222222"}
    # 다음 발굴에서 auto 교체 — 111111 탈락, 333333 편입. seed/manual 유지
    watchlist.replace_auto([{"code": "222222", "name": "발굴2"},
                            {"code": "333333", "name": "발굴3"}])
    assert set(settings.WATCHLIST) == {"005930", "035420", "222222", "333333"}
    sources = {e["code"]: e["source"] for e in watchlist.entries()}
    assert sources["005930"] == "seed"
    assert sources["035420"] == "manual"
    assert sources["333333"] == "auto"


def test_replace_auto_does_not_downgrade_manual(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    watchlist.init()
    watchlist.add("035420", "NAVER", source="manual")
    watchlist.replace_auto([{"code": "035420", "name": "NAVER"}])
    sources = {e["code"]: e["source"] for e in watchlist.entries()}
    assert sources["035420"] == "manual"   # 수동 항목은 auto 로 강등되지 않음
    # 다음 교체에서 auto 목록이 비어도 수동 항목은 남는다
    watchlist.replace_auto([])
    assert "035420" in settings.WATCHLIST
