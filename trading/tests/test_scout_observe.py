"""관측 전용 TR — 신호를 내지 않고 원장에만 쌓는다.

경로와 필수 파라미터는 실호출로 확정했다(2026-07-31):
  ka10063  /api/dostk/mrkcond   `opmr_invsr_trde`  800행  · 필수 파라미터 `invsr`
  ka10054  /api/dostk/stkinfo   `motn_stk`         100행
"""
import asyncio

import pytest

from app.kiwoom.observe import parse_investor, parse_vi, signed
from app.scout import observe, store


# 실응답에서 그대로 가져온 행 (2026-07-31 장중)
INV = {
    "stk_cd": "900290", "stk_nm": "GRT", "cur_prc": "+3355", "flu_rt": "+22.89",
    "acc_trde_qty": "736822", "netprps_amt": "+191", "netprps_amt_irds": "+45",
    "buy_amt": "+430", "sell_amt": "--239", "netprps_qty": "+57000",
}
VI = {
    "stk_cd": "289220", "stk_nm": "자이언트스텝", "motn_pric": "1022",
    "dynm_dispty_rt": "-8.59", "trde_cntr_proc_time": "180030",
    "virelis_time": "180220", "viaplc_tp": "동적", "vimotn_cnt": "3",
}


def test_부호가_두_번_붙어_온다():
    """실응답의 `--239`. float() 이 예외라 종전 파서는 조용히 0 을 돌려준다 —
    순매도 239백만원이 '거래 없음' 이 된다."""
    assert signed("--239") == -239
    assert signed("+53") == 53
    assert signed("-8.59") == -8.59
    assert signed("") == 0.0 and signed(None) == 0.0


def test_투자자별_순매수_파싱():
    (r,) = parse_investor({"return_code": 0, "opmr_invsr_trde": [INV]})
    assert r["code"] == "900290" and r["price"] == 3355
    assert r["net_amt"] == 191 and r["net_amt_delta"] == 45
    assert r["sell_amt"] == -239          # 부호가 정보다 — 절댓값으로 뭉개지 않는다
    assert r["change_pct"] == 22.89


def test_VI_파싱():
    (r,) = parse_vi({"return_code": 0, "motn_stk": [VI]})
    assert r["code"] == "289220" and r["kind"] == "동적"
    assert r["motn_time"] == "180030" and r["motn_cnt"] == 3
    assert r["dispty_rt"] == -8.59


@pytest.mark.parametrize("parser, key", [(parse_investor, "opmr_invsr_trde"),
                                         (parse_vi, "motn_stk")])
def test_실패_응답은_빈_목록(parser, key):
    assert parser({"return_code": 1, key: [INV]}) == []
    assert parser({"return_code": 0}) == []


# --- 적재 --------------------------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "scout.db")


def test_투자자_관측은_시점별로_쌓인다(db):
    """순매수는 하루 동안 방향이 바뀌는 값이라 접으면 '언제' 가 사라진다."""
    from datetime import UTC, datetime

    rows = parse_investor({"return_code": 0, "opmr_invsr_trde": [INV]})
    t1 = datetime(2026, 8, 3, 1, 0, tzinfo=UTC)     # KST 10:00
    t2 = datetime(2026, 8, 3, 2, 0, tzinfo=UTC)     # KST 11:00
    assert store.record_investor(rows, "6", t1) == 1
    assert store.record_investor(rows, "6", t2) == 1
    with store._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM investor_obs").fetchone()[0] == 2


def test_VI_는_발동시각까지_키다(db):
    """같은 종목이 하루에 여러 번 발동한다."""
    store.record_vi(parse_vi({"return_code": 0, "motn_stk": [VI]}))
    store.record_vi(parse_vi({"return_code": 0, "motn_stk": [VI]}))       # 같은 발동
    store.record_vi(parse_vi({"return_code": 0, "motn_stk": [
        {**VI, "trde_cntr_proc_time": "181500"}]}))                      # 다른 발동
    with store._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM vi_obs").fetchone()[0] == 2


def test_우리_거래와_VI_의_겹침을_센다(db):
    store.record_vi(parse_vi({"return_code": 0, "motn_stk": [VI]}))
    from datetime import datetime

    from app.scout.promote import KST
    now = datetime.now(KST)
    if now.strftime("%Y-%m-%d") == datetime.now(KST).strftime("%Y-%m-%d"):
        assert observe.overlap_today({"289220", "005930"}) == {"289220"}


def test_한쪽이_실패해도_다른_쪽은_쌓인다(db, monkeypatch):
    """관측이 서로를 막을 이유가 없다."""
    class Fake:
        async def intraday_investor(self, **kw):
            raise RuntimeError("서버 오류")

        async def vi_stocks(self, **kw):
            return {"return_code": 0, "motn_stk": [VI]}

    import app.kiwoom.client as kc
    monkeypatch.setattr(kc, "client", Fake(), raising=False)
    got = asyncio.run(observe.collect_once())
    assert got["investor"] == 0 and got["vi"] == 1


def test_순매수_항등식이_맞아야_한다():
    """실응답 검산 — 매수 430 · 매도 -239 · 순매수 191.
    부호를 토글로 읽으면 매도가 +239 가 되어 순매수가 669 로 부푼다."""
    (r,) = parse_investor({"return_code": 0, "opmr_invsr_trde": [INV]})
    assert r["buy_amt"] + r["sell_amt"] == r["net_amt"]
