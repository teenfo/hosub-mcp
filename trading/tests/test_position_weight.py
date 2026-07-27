"""종목당 비중 상한 — 첫 진입이 예수금을 독식하지 않게 하는 사이징 캡.

회귀 배경: 상한이 없던 시절 실측 로그에서 ORB 첫 2건이 예수금의 85%를 소진해
이후 16건이 전부 '1주도 매수 불가'로 밀렸다.
"""
import pytest

from app import settings
from app.signals.engine import SignalEngine
from app.trade import risk


def test_weight_cap_limits_qty():
    """손절폭이 좁아 리스크 기준 수량이 커도 비중 상한이 자른다."""
    # 자산 560,000 / 진입 23,600 / 손절 23,500(폭 100원)
    # 리스크 기준: 560,000×0.5%/100 = 28주 → 매수여력 23주로 잘림(= 전액 투입)
    assert risk.position_size(560_000, 0.5, 23_600, 23_500) == 23
    # 비중 20% 상한: 112,000 / 23,600 = 4주
    assert risk.position_size(560_000, 0.5, 23_600, 23_500,
                              max_weight_pct=20) == 4


def test_weight_cap_off_by_zero():
    """0 이면 상한 미적용 — 종전 동작 유지."""
    a = risk.position_size(560_000, 0.5, 23_600, 23_500)
    assert risk.position_size(560_000, 0.5, 23_600, 23_500, max_weight_pct=0) == a


def test_risk_limit_still_wins_when_tighter():
    """리스크 한도가 비중 상한보다 작으면 리스크 한도가 이긴다."""
    # 손절폭 5,000원 → 560,000×0.5%/5,000 = 0주 (비중 상한과 무관하게 0)
    assert risk.position_size(560_000, 0.5, 23_600, 18_600, max_weight_pct=50) == 0


def test_available_cash_caps_qty():
    """같은 사이클에서 앞선 발주가 현금을 쓰면 뒤 신호는 남은 현금까지만."""
    # 비중 상한 20%(=4주)이지만 남은 현금이 50,000원이면 2주
    assert risk.position_size(560_000, 0.5, 23_600, 23_500,
                              max_weight_pct=20, available=50_000) == 2
    assert risk.position_size(560_000, 0.5, 23_600, 23_500,
                              max_weight_pct=20, available=0) == 0
    # 음수 가용현금(과다 차감 방어)도 0
    assert risk.position_size(560_000, 0.5, 23_600, 23_500,
                              max_weight_pct=20, available=-1) == 0


def test_weight_cap_qty_helper():
    assert risk.weight_cap_qty(560_000, 23_600, 20) == 4
    assert risk.weight_cap_qty(560_000, 23_600, 0) == 0      # 미적용
    assert risk.weight_cap_qty(560_000, 200_000, 20) == 0    # 1주도 상한 초과
    assert risk.weight_cap_qty(560_000, 0, 20) == 0          # 0원 방어


def test_invalid_inputs_return_zero():
    assert risk.position_size(560_000, 0.5, 23_600, 23_600) == 0   # 손절폭 0
    assert risk.position_size(560_000, 0.5, 0, 100) == 0           # 진입가 0


def test_expensive_stock_blocked_by_cap():
    """1주 값이 상한을 넘으면 0주 — 상한이 켜져 있으면 결과가 0이라도 적용된다.
    (회귀: 상한 수량 0 을 '상한 미설정'으로 오해해 캡이 무시되던 버그)"""
    assert risk.position_size(560_000, 5.0, 150_000, 148_000,
                              max_weight_pct=20) == 0
    assert risk.position_size(560_000, 5.0, 150_000, 148_000) == 3   # 상한 없으면 3주


# --- 설정 영속화 ---

def test_save_risk_persists_new_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RISK_FILE", tmp_path / "risk.json")
    monkeypatch.setattr(settings, "RISK", {})
    settings.save_risk(max_position_weight_pct=25, confirm_on_close=True)
    assert settings.RISK["max_position_weight_pct"] == 25.0
    assert settings.RISK["confirm_on_close"] is True
    settings.RISK.clear()
    settings._load_risk_overrides()
    assert settings.RISK["max_position_weight_pct"] == 25.0   # 재기동 후에도 유지
    assert settings.RISK["confirm_on_close"] is True


def test_save_risk_persists_modal_fields(tmp_path, monkeypatch):
    """기어 모달에서 바꾸는 나머지 값들도 재기동 후 유지된다."""
    monkeypatch.setattr(settings, "RISK_FILE", tmp_path / "risk.json")
    monkeypatch.setattr(settings, "RISK", {})
    settings.save_risk(max_positions=5, signal_ttl_min=30, long_only=False,
                       auto_approve=True)
    settings.RISK.clear()
    settings._load_risk_overrides()
    assert settings.RISK["max_positions"] == 5
    assert settings.RISK["signal_ttl_min"] == 30
    assert settings.RISK["long_only"] is False
    assert settings.RISK["auto_approve"] is True


def test_save_risk_rejects_modal_fields_out_of_range(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RISK_FILE", tmp_path / "risk.json")
    monkeypatch.setattr(settings, "RISK", {})
    for kw in ({"max_positions": 0}, {"max_positions": 21},
               {"signal_ttl_min": 0}, {"signal_ttl_min": 999}):
        with pytest.raises(ValueError):
            settings.save_risk(**kw)


def test_save_risk_rejects_weight_out_of_range(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RISK_FILE", tmp_path / "risk.json")
    monkeypatch.setattr(settings, "RISK", {})
    for bad in (-1, 101, 500):
        with pytest.raises(ValueError):
            settings.save_risk(max_position_weight_pct=bad)


def test_engine_weight_pct_clamps_bad_values(monkeypatch):
    eng = SignalEngine()
    for raw, want in [(20, 20.0), (0, 0.0), ("bad", 0.0), (None, 0.0),
                      (-5, 0.0), (150, 0.0), (100, 100.0)]:
        monkeypatch.setattr(settings, "RISK", {"max_position_weight_pct": raw})
        assert eng.max_position_weight_pct() == want, raw
    monkeypatch.setattr(settings, "RISK", {})            # 미설정 → 상한 없음
    assert eng.max_position_weight_pct() == 0.0


def test_rules_for_injects_confirm_override(monkeypatch):
    """UI 설정(risk.json)이 config.yaml 의 rules 기본값을 덮어쓴다."""
    eng = SignalEngine()
    monkeypatch.setattr(settings, "RULES", {"confirm_on_close": True,
                                            "orb": {"enabled": True}})
    monkeypatch.setattr(settings, "RISK", {})
    assert eng._rules_for("005930")["confirm_on_close"] is True   # config 기본값

    monkeypatch.setattr(settings, "RISK", {"confirm_on_close": False})
    assert eng._rules_for("005930")["confirm_on_close"] is False  # UI 가 이김
    assert settings.RULES["confirm_on_close"] is True             # 원본 불변
