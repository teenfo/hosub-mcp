"""수동 편입 게이트 — 편입은 공짜가 아니다.

감시목록에 들어가면 분봉 백필·WS 구독·신호 평가가 따라붙는다. 그런데 종전
`POST /api/watchlist` 는 코드 형식과 종목명 해석만 보고 아무 조건도 걸지
않았다. 여기서 지키는 것:

  - 실재하지 않는 코드는 **force 로도** 못 넣는다 (죽은 코드가 남으면 백필이 계속 실패)
  - 기준 미달은 막되 **이유를 숫자로** 돌려주고, 사용자가 force 로 넘길 수 있다
  - 장 시작 전·휴장일(거래대금 0)에 유동성으로 막지 않는다
  - 자동 편입(news)은 이 문을 지나지 않는다
"""
from __future__ import annotations

import pytest

from app.data import admit


def _row(**over):
    return {"stk_cd": "005930", "stk_nm": "삼성전자", "cur_prc": "-208500",
            "trde_prica": "13726155", "trde_qty": "65555523", "cntr_str": "107.0",
            "mac": "12189491", "upl_pric": "+286000", "lst_pric": "-154000"} | over


CONF = {"min_trde_prica": 1_000, "exclude_kinds": True}


def test_liquid_stock_passes():
    v = admit.evaluate(_row(), "005930", CONF)
    assert v["ok"] and v["reasons"] == []
    assert v["metrics"]["price"] == 208500          # 부호는 방향 표시다
    assert v["metrics"]["trde_prica"] == 13726155


def test_thin_stock_is_blocked_with_numbers():
    """왜 막혔는지 숫자로 알려줘야 사용자가 force 여부를 판단한다."""
    v = admit.evaluate(_row(trde_prica="120"), "005930", CONF)
    assert not v["ok"] and not v["fatal"]
    assert "거래대금" in v["reasons"][0] and "120" in v["reasons"][0]


def test_zero_volume_is_not_a_liquidity_failure():
    """장 시작 전·휴장일은 거래대금이 0이다 — 그때 막으면 아무것도 못 넣는다.

    거래가 아예 없는 것은 '미달' 이 아니라 '판정 불가' 다.
    """
    assert admit.evaluate(_row(trde_prica="0", trde_qty="0"), "005930", CONF)["ok"]


def test_unknown_code_is_fatal():
    """오타를 통과시키면 감시목록에 죽은 코드가 남아 백필·구독이 계속 실패한다."""
    v = admit.evaluate(None, "999999", CONF)
    assert not v["ok"] and v["fatal"] is True
    v2 = admit.evaluate({"stk_cd": "999999", "stk_nm": ""}, "999999", CONF)
    assert v2["fatal"] is True


@pytest.mark.parametrize("name", [
    "KODEX 인버스", "TIGER 200", "KODEX 코스닥150선물인버스",
    "KoAct 바이오헬스케어액티브", "미래에셋제7호스팩",
])
def test_fund_like_names_are_blocked(name):
    """실측: 매매 tier 에 KODEX 인버스·코스닥150선물인버스가 섞여 있었다."""
    v = admit.evaluate(_row(stk_nm=name), "114800", CONF)
    assert not v["ok"]
    assert any("ETF" in r for r in v["reasons"])


def test_normal_names_are_not_fund_like():
    for name in ("삼성전자", "한국선재", "대한항공", "코오롱티슈진"):
        assert not admit.is_fund_like(name), name


def test_all_reasons_are_collected():
    """첫 번째에서 멈추면 force 를 누른 뒤 두 번째 이유에 또 막힌다."""
    v = admit.evaluate(_row(stk_nm="KODEX 인버스", trde_prica="50"), "114800", CONF)
    assert len(v["reasons"]) == 2


def test_row_of_picks_the_right_stock():
    payload = {"atn_stk_infr": [_row(stk_cd="000660", stk_nm="SK하이닉스"), _row()]}
    assert admit.row_of(payload, "005930")["stk_nm"] == "삼성전자"
    assert admit.row_of(payload, "111111") is None
    assert admit.row_of({}, "005930") is None


def test_config_override(monkeypatch):
    from app import settings

    monkeypatch.setitem(settings.CONFIG, "admit", {"min_trde_prica": 100_000})
    assert admit.cfg()["min_trde_prica"] == 100_000
    assert not admit.evaluate(_row(trde_prica="50000"), "005930")["ok"]
