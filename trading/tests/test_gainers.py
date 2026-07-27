"""KOSPI 급등률 상위 필터 + 자동 감시편입 테스트."""
from app import settings
from app.data import watchlist
from app.signals import scanner


def test_filter_gainers_tiers_and_excludes(monkeypatch):
    monkeypatch.setattr(settings, "WATCHLIST", {"000660": "SK하이닉스"})
    V = 100_000   # 넉넉한 거래량(거래대금 근사 통과용)
    items = [
        {"code": "010140", "name": "삼성중공업", "price": 23_000, "change_pct": 8.0, "volume": V},
        {"code": "005930", "name": "삼성전자", "price": 270_000, "change_pct": 5.0, "volume": V},
        {"code": "069500", "name": "KODEX 200", "price": 30_000, "change_pct": 6.0, "volume": V},   # ETF 제외
        {"code": "011155", "name": "CJ씨푸드1우", "price": 6_780, "change_pct": 29.9, "volume": V},  # 우선주 제외
        {"code": "000001", "name": "동전주", "price": 500, "change_pct": 12.0, "volume": V},         # min_price 미달
        {"code": "222222", "name": "저유동주", "price": 10_000, "change_pct": 15.0, "volume": 10},   # 거래대금 근사 미달
        {"code": "000660", "name": "SK하이닉스", "price": 20_000, "change_pct": 9.0, "volume": V},   # 이미 감시중
        {"code": "111111", "name": "하락주", "price": 10_000, "change_pct": -2.0, "volume": V},      # 하락 제외
    ]
    cfg = {"min_price": 1000, "min_trade_value_krw": 100_000_000, "trade_max_price": 30_000, "top_n": 15}
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


def test_liquidity_floor_drops_thin_names(monkeypatch):
    """유동성 하한 — 거래대금이 얇으면 소액 주문에도 시장 충격이 난다.
    (실측 2026-07-27: 3~10억짜리가 감시목록에 들어와 실제로 매매·손절됨)"""
    monkeypatch.setattr(settings, "WATCHLIST", {})
    items = [
        {"code": "000001", "name": "두꺼운주", "price": 10_000,
         "volume": 500_000, "change_pct": 5.0},        # 50억
        {"code": "000002", "name": "얇은주", "price": 10_000,
         "volume": 50_000, "change_pct": 9.0},         # 5억 — 등락률이 더 높아도 탈락
    ]
    cfg = {"min_price": 1_000, "min_trade_value_krw": 3_000_000_000,
           "trade_max_price": 30_000, "top_n": 15}
    assert [x["code"] for x in scanner.filter_gainers(items, cfg)] == ["000001"]
    # 하한을 낮추면 둘 다 통과 — 필터가 실제로 그 값을 쓴다는 확인
    cfg["min_trade_value_krw"] = 100_000_000
    assert len(scanner.filter_gainers(items, cfg)) == 2


def test_config_liquidity_floor_is_raised():
    """운영 config 의 하한이 30억 이상인지 — 값이 되돌아가면 잡는다."""
    assert settings.CONFIG["gainers"]["min_trade_value_krw"] >= 3_000_000_000


def test_config_stop_band():
    """손절폭 대역이 비용 구조와 맞는지 — 왕복 비용 0.28% 를 이겨야 한다."""
    r = settings.CONFIG["rules"]
    costs = settings.CONFIG["costs"]
    round_trip = (costs["commission_pct"] * 2 + costs["sell_tax_pct"]
                  + costs["slippage_bp"] / 100 * 2)
    target_r = r["orb"]["target_r"]
    # 하한 × target_r 이 왕복 비용보다 충분히 커야 '이겨도 본전'이 안 된다
    assert r["min_stop_pct"] * target_r > round_trip * 1.5
    assert r["min_stop_pct"] < r["max_stop_pct"] <= 3.0
