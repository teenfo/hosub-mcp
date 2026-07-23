from app import settings
from app.trade import ledger


def _order(oid, symbol="005930", side="long", entry=10_000, stop=9_800,
           target=10_400, rule="orb", qty=10):
    return {"id": oid, "symbol": symbol, "side": side, "entry": entry,
            "stop": stop, "target": target, "rule": rule, "qty": qty}


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(settings, "WATCHLIST", {"005930": "삼성전자"})
    monkeypatch.setattr(settings, "COSTS", {"commission_pct": 0.015,
                                            "sell_tax_pct": 0.15, "slippage_bp": 5})


def test_open_records_fill_and_slippage(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    ledger.open_position(_order("a1"), fill=10_050)   # 모델 10,000 → 체결 10,050
    (p,) = ledger.positions(status="open")
    assert p["entry"] == 10_050 and p["model_entry"] == 10_000
    assert round(p["slippage_pct"], 2) == 0.5         # +0.5% 슬리피지
    assert p["name"] == "삼성전자"


def test_long_target_closes_with_profit(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    ledger.open_position(_order("a2"), fill=10_000)
    # 목표가 10,400 도달 → target 청산, 비용 차감 후에도 양수
    closed = ledger.monitor(lambda s: 10_500)
    assert closed == 1
    (p,) = ledger.positions(status="closed")
    assert p["exit_reason"] == "target" and p["exit"] == 10_400
    assert p["pnl_pct"] > 3.0 and p["pnl_krw"] > 0


def test_long_stop_closes_with_loss(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    ledger.open_position(_order("a3"), fill=10_000)
    ledger.monitor(lambda s: 9_700)                   # 손절 9,800 하회
    (p,) = ledger.positions(status="closed")
    assert p["exit_reason"] == "stop" and p["pnl_pct"] < 0


def test_short_profit_when_price_falls(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    ledger.open_position(_order("a4", side="short", entry=10_000,
                                stop=10_200, target=9_600), fill=10_000)
    ledger.monitor(lambda s: 9_500)                   # 목표 9,600 도달
    (p,) = ledger.positions(status="closed")
    assert p["exit_reason"] == "target" and p["pnl_pct"] > 0   # 숏은 하락이 이익


def test_force_close_eod(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    ledger.open_position(_order("a5"), fill=10_000)
    n = ledger.force_close_eod(lambda s: 10_100)
    assert n == 1
    (p,) = ledger.positions(status="closed")
    assert p["exit_reason"] == "eod"


def test_parse_execution_default_fids(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CONFIG", {})
    vals = {"9203": "0001234", "9001": "A005930", "913": "체결",
            "910": "10050", "911": "10", "908": "093015"}
    f = ledger.parse_execution(vals)
    assert f == {"ord_no": "0001234", "symbol": "005930", "state": "체결",
                 "price": 10050.0, "qty": 10, "ts": "093015"}


def test_record_fill_updates_long_entry_precisely(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "CONFIG", {})
    ledger.open_position(_order("o1"), fill=10_000, ord_no="0001234")  # 근사 진입 10,000
    # 실측 체결가 10,080 수신 → 진입가·슬리피지 정밀 갱신, 체결확인 플래그
    upd = ledger.record_fill({"ord_no": "0001234", "symbol": "005930",
                              "price": 10_080, "qty": 10, "state": "체결"})
    assert upd is True
    (p,) = ledger.positions(status="open")
    assert p["entry"] == 10_080 and p["fill_confirmed"] == 1
    assert round(p["slippage_pct"], 2) == 0.8
    assert len(ledger.fills()) == 1


def test_record_fill_short_audit_only(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "CONFIG", {})
    # 숏은 인버스 ETF(114800)로 집행 — 신호 종목(005930)과 다르므로 진입가 미갱신
    order = _order("o2", side="short")
    order["exec_symbol"] = "114800"
    ledger.open_position(order, fill=10_000, ord_no="0009999")
    upd = ledger.record_fill({"ord_no": "0009999", "symbol": "114800",
                              "price": 8_500, "qty": 12, "state": "체결"})
    assert upd is False                       # 진입가 갱신 안 함(감사 기록만)
    (p,) = ledger.positions(status="open")
    assert p["entry"] == 10_000 and p["fill_confirmed"] == 0
    assert ledger.fills()[0]["matched"] == 1  # 주문번호는 매칭됨


def test_record_fill_unmatched_ordno_is_safe(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "CONFIG", {})
    assert ledger.record_fill({"ord_no": "zzz", "symbol": "005930",
                               "price": 100, "qty": 1}) is False
    assert ledger.fills()[0]["matched"] == 0  # 미매칭이어도 예외 없이 감사 기록


def test_stats_aggregates_by_rule(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    ledger.open_position(_order("w", rule="orb"), fill=10_000)
    ledger.open_position(_order("l", rule="gap"), fill=10_000)
    ledger.monitor(lambda s: 10_500)                  # 둘 다 target? no — 서로 다른 종목 아님
    # 위 monitor 로 둘 다 target(10,400) 청산됨
    st = ledger.stats()
    assert st["overall"]["trades"] == 2
    assert st["open_count"] == 0
    assert "orb" in st["by_rule"] and "gap" in st["by_rule"]


def test_realized_today_sums_closed(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "CONFIG", {})
    ledger.open_position(_order("d1"), fill=10_000)
    ledger.open_position(_order("d2"), fill=10_000)
    ledger.monitor(lambda s: 10_500)          # 둘 다 목표 청산(이익)
    r = ledger.realized_today(equity=1_000_000)
    assert r["trades"] == 2 and r["krw"] > 0 and r["pct"] > 0
