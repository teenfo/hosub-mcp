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
