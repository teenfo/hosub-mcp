import numpy as np
import pandas as pd

from app import discovery
from app.signals import scanner

CFG = {"vol_surge_ratio": 3.0, "near_high_ratio": 0.97, "min_price": 1_000,
       "min_trade_value_krw": 1_000_000_000, "min_score": 2, "top_n": 20}


def _daily(closes, volumes):
    idx = pd.date_range("2026-04-01", periods=len(closes), freq="B")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame(
        {"open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
         "volume": volumes}, index=idx,
    )


def test_screen_all_three_rules_fire():
    # 60일 완만 상승 → 마지막 날 신고가 + 거래량 5배
    closes = list(np.linspace(9000, 10000, 69)) + [10500]
    volumes = [200_000] * 69 + [1_000_000]
    score, reasons = discovery.screen_daily(_daily(closes, volumes), CFG)
    assert score >= 2
    assert any("거래량" in r for r in reasons)
    assert any("고가" in r for r in reasons)


def test_screen_flat_stock_scores_zero():
    df = _daily([10_000] * 70, [500_000] * 70)
    score, reasons = discovery.screen_daily(df, CFG)
    assert score < 2  # 급증도 정배열 전환도 없음


def test_screen_filters_low_liquidity():
    # 거래대금 미달 (1000원 × 10만주 = 1억)
    df = _daily([1_000] * 70, [100_000] * 70)
    assert discovery.screen_daily(df, CFG) == (0.0, [])


def test_screen_needs_60_bars():
    df = _daily([10_000] * 30, [500_000] * 30)
    assert discovery.screen_daily(df, CFG) == (0.0, [])


def test_parse_stock_list_real_format():
    # 실제 ka10099 응답: 배열 키 'list', 필드 code/name
    raw = {"return_code": 0, "list": [
        {"code": "000020", "name": "동화약품", "lastPrice": "00004910"},
        {"code": "900110", "name": "딥커머스"},
        {"code": "BAD", "name": "이상한코드"},   # 6자리 숫자 아님 → 제외
    ]}
    out = discovery.parse_stock_list(raw)
    assert out == [{"code": "000020", "name": "동화약품"},
                   {"code": "900110", "name": "딥커머스"}]


def test_parse_stock_list_legacy_format():
    # 문서상 형식(stk_cd/stk_nm)도 하위호환 수용
    raw = {"return_code": 0, "stk_infr": [{"stk_cd": "A005930", "stk_nm": "삼성전자"}]}
    assert discovery.parse_stock_list(raw) == [{"code": "005930", "name": "삼성전자"}]


def test_parse_stock_list_null_list():
    # mrkt_tp 잘못된 값 → list=null → 빈 결과 (예외 없이)
    assert discovery.parse_stock_list({"return_code": 0, "list": None}) == []


# --- 급등 조짐(presurge) 필터 ---

SURGE_RAW = {"trde_qty_sdnin": [
    {"stk_cd": "A111111", "stk_nm": "조짐주", "cur_prc": "+005000",
     "flu_rt": "+1.2", "sdnin_rt": "450.0", "now_trde_qty": "500000"},
    {"stk_cd": "A222222", "stk_nm": "이미급등", "cur_prc": "+008000",
     "flu_rt": "+7.5", "sdnin_rt": "600.0", "now_trde_qty": "900000"},
    {"stk_cd": "A333333", "stk_nm": "급증미달", "cur_prc": "+003000",
     "flu_rt": "+0.5", "sdnin_rt": "120.0", "now_trde_qty": "100000"},
]}
PRESURGE_CFG = {"min_volume_surge_pct": 300.0, "change_pct_min": -1.0,
                "change_pct_max": 3.0, "min_price": 1_000, "top_n": 10}


def test_presurge_picks_volume_first_price_later():
    picked = scanner.filter_presurge(scanner.parse_surge(SURGE_RAW), PRESURGE_CFG)
    codes = [p["code"] for p in picked]
    assert codes == ["111111"]      # 거래량 급증 + 등락률 아직 낮음
    # 이미 +7.5% 오른 종목은 조짐이 아니라 기존 스캐너 대상
    assert "222222" not in codes
    assert "333333" not in codes    # 급증률 미달


def test_parse_surge_fields():
    items = scanner.parse_surge(SURGE_RAW)
    assert items[0]["surge_pct"] == 450.0
    assert items[0]["price"] == 5_000


def test_is_excluded_etf_etn_reit():
    cfg = {}
    # 실제 발굴에 섞였던 잡주
    assert discovery.is_excluded("PLUS 단기채권액티브", cfg)
    assert discovery.is_excluded("RISE 단기채권알파액티브", cfg)
    assert discovery.is_excluded("코람코더원리츠", cfg)
    assert discovery.is_excluded("키움 CD금리투자 ETN", cfg)
    assert discovery.is_excluded("KODEX 200", cfg)
    assert discovery.is_excluded("TIGER 미국나스닥100", cfg)


def test_is_excluded_keeps_common_stocks():
    cfg = {}
    for name in ["가비아", "지엔씨에너지", "SK이터닉스", "다날", "한울반도체",
                 "삼성전자", "메리츠금융지주", "GS"]:
        assert not discovery.is_excluded(name, cfg), name


def test_is_excluded_custom_keywords():
    cfg = {"exclude_keywords": ["테스트제외"], "exclude_suffixes": [], "exclude_prefixes": []}
    assert discovery.is_excluded("무언가 테스트제외 종목", cfg)
    assert not discovery.is_excluded("코람코더원리츠", cfg)  # 커스텀 목록엔 리츠 없음


def test_is_excluded_reit_suffix_not_substring():
    cfg = {}
    assert discovery.is_excluded("롯데리츠", cfg)          # 접미사 리츠 → 제외
    assert not discovery.is_excluded("메리츠금융지주", cfg)  # 중간 '리츠' → 유지
    assert not discovery.is_excluded("메리츠증권", cfg)


def test_compute_features_extended_keys():
    import numpy as np
    from app.features import compute_features
    closes = list(np.linspace(9000, 10000, 69)) + [10500]
    volumes = [200_000] * 69 + [1_000_000]
    f = compute_features(_daily(closes, volumes), CFG)
    for k in ("atr_pct", "disparity20", "range20_pct", "up_streak",
              "above_ma20", "above_ma60", "bearish_align", "near_low60_pct",
              "vcp", "ret_120d"):
        assert k in f, k
    assert f["above_ma60"] == 1 and f["bearish_align"] == 0   # 상승 추세


def test_compute_market_regime_and_rs():
    # 3종목: 두 개 강세(60이평 상회), 하나 역배열 약세
    rows = [
        {"code": "1", "name": "강1", "liquid": 1, "etf_etn": 0, "ret_20d": 10,
         "above_ma60": 1, "above_ma20": 1, "close": 100, "ma60": 90,
         "bearish_align": 0, "near_low60_pct": 130},
        {"code": "2", "name": "강2", "liquid": 1, "etf_etn": 0, "ret_20d": 5,
         "above_ma60": 1, "above_ma20": 1, "close": 100, "ma60": 95,
         "bearish_align": 0, "near_low60_pct": 120},
        {"code": "3", "name": "약1", "liquid": 1, "etf_etn": 0, "ret_20d": -8,
         "above_ma60": 0, "above_ma20": 0, "close": 80, "ma60": 100,
         "bearish_align": 1, "near_low60_pct": 102},
    ]
    m = discovery.compute_market(rows, {"bearish_min_score": 2})
    assert m["breadth_ma60"] == round(200 / 3, 1)     # 3중 2개 상회
    assert m["regime"] in ("강세", "중립", "약세")
    # RS = 개별 ret - 중앙값(5)
    assert rows[0]["rs_20"] == 5.0 and rows[2]["rs_20"] == -13.0
    # 약세 종목은 bearish_score 3 (역배열+60이평하회+저점근접)
    assert rows[2]["bearish_score"] == 3
    assert m["bearish_count"] == 1 and m["bearish_top"][0]["code"] == "3"
