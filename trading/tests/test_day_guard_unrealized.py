"""일일 손실 가드가 평가손익을 세는지 — 이번 실측의 구멍.

가드는 청산된 것만 셌다. 그래서 물려 있는 포지션이 아무리 깊어도 침묵했고,
실측 2026-07-30 한도 1.5% 인 날 계좌는 -4.26% 까지 내려갔다.
"""
from app.trade import ledger, risk


def test_실현만으로는_안_걸려도_평가를_더하면_걸린다():
    """이번 회귀의 핵심 — 종전 동작이면 통과시켰을 상황."""
    halted, why = risk.day_guard(-0.8, 1.0, 1.5, unrealized_pct=-2.4)
    assert halted and "손실 한도" in why
    assert "-3.20%" in why            # 실현 + 평가를 함께 보여준다


def test_종전_동작으로_되돌릴_수_있다():
    halted, _ = risk.day_guard(-0.8, 1.0, 1.5, unrealized_pct=-2.4,
                               include_unrealized=False)
    assert not halted


def test_이익_목표는_실현분만_본다():
    """평가이익으로 그날 장사를 접지 않는다 — 아직 손에 쥔 돈이 아니다."""
    halted, _ = risk.day_guard(0.3, 1.0, 1.5, unrealized_pct=+2.0)
    assert not halted
    halted, why = risk.day_guard(1.2, 1.0, 1.5, unrealized_pct=-0.1)
    assert halted and "목표" in why


def test_평가이익이_손실을_덮으면_안_걸린다():
    halted, _ = risk.day_guard(-1.4, 0, 1.5, unrealized_pct=+0.9)
    assert not halted


def test_정리선은_기본값_0_이면_꺼져_있다():
    assert risk.flatten_call(-3.0, -5.0, 0) == (False, "")


def test_정리선을_켜면_한도보다_늦게_걸린다():
    # 한도 1.5 는 이미 넘었지만 정리선 4.0 은 아직
    assert risk.day_guard(-1.0, 0, 1.5, unrealized_pct=-1.0)[0]
    assert not risk.flatten_call(-1.0, -1.0, 4.0)[0]
    fired, why = risk.flatten_call(-1.0, -3.5, 4.0)
    assert fired and "정리" in why


# --- open_pnl -----------------------------------------------------------------

def _seed(monkeypatch, rows):
    monkeypatch.setattr(ledger, "positions", lambda **kw: rows)


def test_평가손익_합계(monkeypatch):
    _seed(monkeypatch, [
        {"symbol": "005930", "side": "long", "entry": 1000.0, "qty": 10},
        {"symbol": "000660", "side": "short", "entry": 2000.0, "qty": 5},
    ])
    px = {"005930": 900.0, "000660": 1800.0}
    out = ledger.open_pnl(px.get, equity=100_000)
    # 롱 -100×10 = -1,000 / 숏 +200×5 = +1,000
    assert out["krw"] == 0.0
    assert out["positions"] == 2 and out["priced"] == 2


def test_가격을_못_얻은_건은_0_으로_세지_않는다(monkeypatch):
    """0 으로 세면 손실을 과소계상해 가드가 다시 침묵한다."""
    _seed(monkeypatch, [
        {"symbol": "005930", "side": "long", "entry": 1000.0, "qty": 10},
        {"symbol": "999999", "side": "long", "entry": 1000.0, "qty": 10},
    ])
    out = ledger.open_pnl(lambda s: 900.0 if s == "005930" else None, equity=100_000)
    assert out["krw"] == -1000.0
    assert out["positions"] == 2 and out["priced"] == 1   # 호출부가 구멍을 안다


def test_보유가_없으면_0(monkeypatch):
    _seed(monkeypatch, [])
    out = ledger.open_pnl(lambda s: 100.0, equity=100_000)
    assert out == {"krw": 0.0, "pct": 0.0, "positions": 0, "priced": 0}
