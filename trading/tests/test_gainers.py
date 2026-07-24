"""KOSPI 급등률 상위 필터 + 자동 감시편입 테스트."""
from app import settings
from app.data import watchlist
from app.signals import scanner


def test_filter_gainers_tiers_and_excludes(monkeypatch):
    monkeypatch.setattr(settings, "WATCHLIST", {"000660": "SK하이닉스"})
    items = [
        {"code": "010140", "name": "삼성중공업", "price": 23_000, "change_pct": 8.0, "trade_value": 90_000},
        {"code": "005930", "name": "삼성전자", "price": 270_000, "change_pct": 5.0, "trade_value": 90_000},
        {"code": "069500", "name": "KODEX 200", "price": 30_000, "change_pct": 6.0, "trade_value": 90_000},  # ETF 제외
        {"code": "000001", "name": "동전주", "price": 500, "change_pct": 12.0, "trade_value": 90_000},        # min_price 미달
        {"code": "000660", "name": "SK하이닉스", "price": 20_000, "change_pct": 9.0, "trade_value": 90_000},  # 이미 감시중
        {"code": "111111", "name": "하락주", "price": 10_000, "change_pct": -2.0, "trade_value": 90_000},     # 하락 제외
    ]
    cfg = {"min_price": 1000, "min_trade_value": 5000, "trade_max_price": 30_000, "top_n": 15}
    out = scanner.filter_gainers(items, cfg)
    codes = [g["code"] for g in out]
    assert codes == ["010140", "005930"]                 # 급등률 순, 필터 통과분만
    tier = {g["code"]: g["collect_only"] for g in out}
    assert tier["010140"] is False                        # 저가주 → 매매
    assert tier["005930"] is True                         # 고가주 → 수집전용


def test_replace_gainers_rotates_and_skips_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(watchlist, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(settings, "WATCHLIST", {})
    monkeypatch.setattr(settings, "COLLECT_ONLY", set())
    watchlist.add("010140", "삼성중공업", source="manual")  # 수동 종목(보호돼야 함)

    watchlist.replace_gainers([
        {"code": "010140", "name": "삼성중공업", "collect_only": True},   # 이미 manual → 스킵
        {"code": "011200", "name": "HMM", "collect_only": False},
        {"code": "005930", "name": "삼성전자", "collect_only": True},
    ])
    src = {e["code"]: (e["source"], e["collect_only"]) for e in watchlist.entries()}
    assert src["010140"][0] == "manual"                   # 수동 종목은 그대로
    assert src["011200"] == ("gainer", 0)                 # 신규 급등주 매매
    assert src["005930"] == ("gainer", 1)                 # 신규 급등주 수집전용

    # 다음 스캔에서 011200 이 빠지면 gainer 소스만 교체된다
    watchlist.replace_gainers([{"code": "005930", "name": "삼성전자", "collect_only": True}])
    codes = {e["code"] for e in watchlist.entries()}
    assert codes == {"010140", "005930"}                  # 011200(gainer) 제거, manual 유지
