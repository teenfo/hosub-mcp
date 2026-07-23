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
