"""실현손익을 증권사 값과 맞춘다.

## 왜 필요했나 (2026-07-28 실측)

계좌 실현손익 **+181,019** vs 원장 **−23,465**. 원인이 둘로 갈렸다.

**① 원장에 없는 거래** — 차이의 99.9%가 7/16 한 건(+204,645, 거래세 0 → ETF).
원장은 *"승인·발주된 주문"* 만 기록하므로 수동매매는 원리적으로 안 들어온다.
버그가 아니라 범위의 문제인데, 화면이 그걸 "실현손익" 이라 부르면 계좌와
안 맞는 게 당연하다. → 합치지 않고 **나란히** 두고 차이를 이름 붙인다.

**② 비용 모델이 세 군데 틀렸다** — 같은 거래를 나란히 놓으면 진입가·청산가는
정확히 일치하고 비용만 어긋난다(마키나락스 3건, 건당 59~72원 과대 손실).

  수수료   0.015%/편도(16원) vs 실제 10원 고정          → 과대
  거래세   0.15%             vs 실제 약 0.20%           → 과소
  슬리피지 0.05%×2 를 차감    vs 이미 체결가에 들어 있다 → 이중계상

셋이 상쇄돼 **합계는 비슷해 보였다**. 그래서 개별 대조 없이는 안 잡혔다.
"""
import pytest

from app.kiwoom import realized
from app.trade import ledger


@pytest.fixture(autouse=True)
def _tmp_db(monkeypatch, tmp_path):
    """실 DB 를 건드리지 않는다 — 실거래 중인 서비스와 같은 디스크다."""
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "trading.db")


# --- ① 파싱 — 실응답 필드명으로 고정 ---

def _daily_raw():
    """2026-07-28 실응답 구조."""
    return {
        "return_code": 0,
        "tot_buy_amt": "3287279", "tot_sell_amt": "2776025",
        "rlzt_pl": "181019", "trde_cmsn": "890", "trde_tax": "3497",
        "dt_rlzt_pl": [
            {"dt": "20260728", "buy_amt": "829230", "sell_amt": "425430",
             "tdy_sel_pl": "-432", "tdy_trde_cmsn": "180", "tdy_trde_tax": "632"},
            {"dt": "20260716", "buy_amt": "984000", "sell_amt": "896445",
             "tdy_sel_pl": "204645", "tdy_trde_cmsn": "280", "tdy_trde_tax": "0"},
        ],
    }


def test_daily_realized_parses_totals_and_days():
    d = realized.parse_daily(_daily_raw())
    assert d["ok"] and d["realized"] == 181_019
    assert d["commission"] == 890 and d["tax"] == 3_497
    assert [x["date"] for x in d["days"]] == ["20260728", "20260716"]   # 최신 먼저
    assert d["days"][1]["realized"] == 204_645


def test_daily_realized_reports_failure():
    out = realized.parse_daily({"return_code": "1", "return_msg": "조회 실패"})
    assert out["ok"] is False and "실패" in out["error"]


def test_executions_parse_real_field_names():
    """실응답 구조 — 매수/매도는 부호가 아니라 '+매수'/'-매도' 글자로 온다."""
    rows = realized.parse_executions({"return_code": 0, "cntr": [
        {"ord_no": "0279260", "stk_cd": "477850", "stk_nm": "마키나락스",
         "io_tp_nm": "-매도", "cntr_pric": "26350", "cntr_qty": "4",
         "tdy_trde_cmsn": "10", "tdy_trde_tax": "210", "ord_tm": "095816"},
        {"ord_no": "0269120", "stk_cd": "477850", "stk_nm": "마키나락스",
         "io_tp_nm": "+매수", "cntr_pric": "27000", "cntr_qty": "4",
         "tdy_trde_cmsn": "10", "tdy_trde_tax": "0", "ord_tm": "095305"},
    ]})
    assert [r["side"] for r in rows] == ["sell", "buy"]
    assert rows[0]["price"] == 26_350 and rows[0]["tax"] == 210
    assert rows[1]["code"] == "477850"


def test_unfilled_orders_are_dropped():
    """주문은 있었지만 체결 0 — 손익에 기여하지 않는다."""
    assert realized.parse_executions({"return_code": 0, "cntr": [
        {"ord_no": "1", "stk_cd": "005930", "io_tp_nm": "+매수",
         "cntr_pric": "0", "cntr_qty": "0"}]}) == []


def test_unknown_side_label_is_not_silently_a_buy():
    """표기가 바뀌면 빈 값으로 남긴다 — 조용히 전부 매수로 읽히면 안 된다."""
    rows = realized.parse_executions({"return_code": 0, "cntr": [
        {"ord_no": "1", "stk_cd": "005930", "io_tp_nm": "???",
         "cntr_pric": "100", "cntr_qty": "1"}]})
    assert rows[0]["side"] == ""


# --- ② 실비용 손익 — 증권사 tdy_sel_pl 과 같은 정의여야 한다 ---

def test_actual_pnl_matches_the_brokers_number():
    """실측 대조: 마키나락스 26,850 → 27,750 × 4주.

    총액 3,600 − (수수료 20 + 거래세 222) = 3,358 = 증권사 tdy_sel_pl.
    """
    assert ledger.actual_pnl_krw("long", 26_850, 27_750, 4, 20, 222) == 3_358


@pytest.mark.parametrize("entry,exit_px,qty,fee,tax,want", [
    (27_000, 26_350, 4, 20, 210, -2_830),      # 실측 2건차
    (26_900, 26_450, 4, 20, 211, -2_031),      # 실측 3건차
])
def test_actual_pnl_reproduces_every_measured_trade(entry, exit_px, qty, fee, tax, want):
    assert ledger.actual_pnl_krw("long", entry, exit_px, qty, fee, tax) == want


def test_actual_pnl_does_not_subtract_slippage():
    """슬리피지는 이미 체결가 안에 있다 — 또 빼면 이중계상이다."""
    assert ledger.actual_pnl_krw("long", 1_000, 1_100, 10, 0, 0) == 1_000


def test_short_pnl_flips_the_gross_but_not_the_costs():
    """비용은 방향과 무관하게 나간다."""
    assert ledger.actual_pnl_krw("short", 1_000, 900, 10, 30, 20) == 950


# --- ③ 대사(reconcile) — ord_no 가 조인 키다 ---

def _pos(pid="p1", ord_no="A1", exit_ord_no="B1", **kw):
    o = {"id": pid, "symbol": "477850", "rule": "orb", "side": "long", "qty": 4,
         "entry": 26_800, "stop": 26_000, "target": 28_000} | kw
    ledger.open_position(o, fill=o["entry"], ord_no=ord_no)
    return o


def _exec(ord_no, price, qty=4, fee=10, tax=0):
    return {"ord_no": ord_no, "code": "477850", "name": "마키나락스",
            "side": "buy", "price": price, "qty": qty,
            "commission": fee, "tax": tax}


def test_reconcile_replaces_the_estimated_entry_with_the_real_fill():
    """실시간 체결을 놓치면 진입가가 1분봉 종가 근사로 남는다(실측 8건)."""
    _pos()
    got = ledger.reconcile_executions([_exec("A1", 27_000)])
    assert got["entries"] == 1
    p = ledger.positions()[0]
    assert p["entry"] == 27_000 and p["fill_confirmed"] == 1


def test_reconcile_averages_split_fills_by_quantity():
    """분할 체결을 한 건만 보고 확정하면 나머지가 통째로 사라진다."""
    _pos()
    ledger.reconcile_executions([_exec("A1", 27_000, qty=1),
                                 _exec("A1", 26_000, qty=3)])
    p = ledger.positions()[0]
    assert p["entry"] == 26_250        # (27000×1 + 26000×3) / 4
    assert p["qty"] == 4


def test_reconcile_is_idempotent_on_costs():
    """두 번 훑어도 비용이 두 배가 되면 안 된다 — 진입·청산 칸을 나눈 이유다."""
    _pos()
    ledger.close_position("p1", 27_750, "target", ord_no="B1")
    execs = [_exec("A1", 26_850, fee=10), _exec("B1", 27_750, fee=10, tax=222)]
    ledger.reconcile_executions(execs)
    first = ledger.positions()[0]["pnl_krw"]
    ledger.reconcile_executions(execs)
    assert ledger.positions()[0]["pnl_krw"] == first == 3_358


def test_reconciled_position_matches_the_broker_exactly():
    """이 테스트가 이번 작업의 목적이다 — 원장 = 증권사."""
    _pos()
    ledger.close_position("p1", 27_000, "target", ord_no="B1")
    ledger.reconcile_executions([_exec("A1", 26_850, fee=10),
                                 _exec("B1", 27_750, fee=10, tax=222)])
    p = ledger.positions()[0]
    assert p["entry"] == 26_850 and p["exit"] == 27_750
    assert p["pnl_krw"] == 3_358              # 증권사 tdy_sel_pl 과 동일


def test_half_confirmed_position_keeps_the_model_number():
    """한쪽만 확정된 것은 비용이 반쪽이라 모델보다 더 틀린다 — 건드리지 않는다."""
    _pos()
    ledger.close_position("p1", 27_750, "target", ord_no="B1")
    before = ledger.positions()[0]["pnl_krw"]
    ledger.reconcile_executions([_exec("A1", 26_850, fee=10)])   # 진입만
    assert ledger.positions()[0]["pnl_krw"] == before


def test_reconcile_ignores_orders_the_ledger_never_placed():
    """수동매매 체결은 원장에 붙을 자리가 없다 — 조용히 지나간다."""
    _pos()
    assert ledger.reconcile_executions([_exec("ZZZ", 9_999)]) == \
           {"entries": 0, "exits": 0}


def test_reconcile_empty_is_safe():
    assert ledger.reconcile_executions([]) == {"entries": 0, "exits": 0}


# --- ④ 인버스 ETF 대체 주문 — 체결가는 ETF 것이고 기준은 원 종목이다 ---

def test_reconcile_does_not_overwrite_entry_when_the_order_went_to_another_symbol():
    """숏 신호는 현물 계좌에서 공매도가 안 되므로 인버스 ETF 매수로 나간다.

    실측 2026-07-29: LG디스플레이 숏(기준가 9,020)에 KODEX 인버스 체결가
    1,235 가 들어가 슬리피지가 **−86.3%** 로 기록됐다. 손절·목표·손익은 원
    종목 기준이라 가격만 다른 종목 것으로 바뀌면 기준 자체가 무너진다.
    """
    ledger.open_position(
        {"id": "s1", "symbol": "034220", "rule": "orb", "side": "short", "qty": 11,
         "entry": 9_020, "stop": 9_200, "target": 8_700,
         "exec_symbol": "114800"}, fill=9_020, ord_no="A9")
    got = ledger.reconcile_executions(
        [{"ord_no": "A9", "code": "114800", "name": "KODEX 인버스", "side": "buy",
          "price": 1_235, "qty": 11, "commission": 0, "tax": 0}])

    p = ledger.positions()[0]
    assert got["entries"] == 1
    assert p["entry"] == 9_020, "ETF 체결가가 원 종목 진입가를 덮으면 안 된다"
    assert p["fill_confirmed"] == 1, "체결 자체는 확정된 것이 맞다"
    assert abs(p["slippage_pct"]) < 1


def test_reconcile_does_not_overwrite_exit_when_the_order_went_to_another_symbol():
    ledger.open_position(
        {"id": "s1", "symbol": "034220", "rule": "orb", "side": "short", "qty": 11,
         "entry": 9_020, "stop": 9_200, "target": 8_700,
         "exec_symbol": "114800"}, fill=9_020, ord_no="A9")
    ledger.close_position("s1", 8_900, "target", ord_no="B9")
    ledger.reconcile_executions(
        [{"ord_no": "B9", "code": "114800", "name": "KODEX 인버스", "side": "sell",
          "price": 1_250, "qty": 11, "commission": 10, "tax": 3}])
    p = ledger.positions(status="closed")[0]
    assert p["exit"] == 8_900 and p["exit_fill_confirmed"] == 1


def test_same_symbol_orders_are_still_reconciled_normally():
    """대체 예외가 '전부 안 덮는다' 가 되면 대사 자체가 무의미해진다."""
    _pos()
    ledger.reconcile_executions([_exec("A1", 27_000)])
    assert ledger.positions()[0]["entry"] == 27_000


def test_executed_symbol_falls_back_to_the_position_symbol():
    assert ledger.executed_symbol({"symbol": "005930"}) == "005930"
    assert ledger.executed_symbol(
        {"symbol": "034220", "exec_symbol": "114800"}) == "114800"
    assert ledger.executed_symbol(
        {"symbol": "034220", "exec_symbol": None}) == "034220"
