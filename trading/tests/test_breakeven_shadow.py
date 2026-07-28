"""본전 이동 shadow — 관측 전용 규약의 회귀 방지.

가장 중요한 테스트는 마지막 두 개다: **shadow 가 실제 청산 판정을 바꾸지
않는가**, **끄면 아무것도 쓰지 않는가**. 나중에 이걸 실제 청산 규칙으로
승격하려면 그 테스트를 고쳐야 하고, 그때는 측정 결과가 있어야 한다.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import settings
from app.trade import breakeven, ledger

KST = ZoneInfo("Asia/Seoul")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(settings, "COSTS", {"commission_pct": 0.015,
                                            "sell_tax_pct": 0.20,
                                            "slippage_bp": 5})
    monkeypatch.setitem(settings.CONFIG, "execution",
                        {"breakeven_shadow": {"enabled": True, "arm_at_pct": 50}})
    return tmp_path


def _open(oid="p1", entry=10_000, stop=9_800, target=10_400, qty=10,
          symbol="005930", rule="orb", side="long"):
    ledger.open_position({"id": oid, "symbol": symbol, "side": side,
                          "entry": entry, "stop": stop, "target": target,
                          "rule": rule, "qty": qty}, fill=entry)


def _feed(prices, oid="p1"):
    """가격을 순서대로 흘려보내며 매 틱 observe 한다(30초 주기 흉내)."""
    t = datetime(2026, 7, 28, 9, 40, tzinfo=KST)
    for i, p in enumerate(prices):
        breakeven.observe(lambda _s, _p=p: _p, now=t + timedelta(seconds=30 * i))


def _row(oid="p1"):
    rows = [r for r in breakeven.rows_on("2026-07-28") if r["pos_id"] == oid]
    return rows[0] if rows else None


# --------------------------------------------------------------------------
# 무장 판정
# --------------------------------------------------------------------------
def test_목표거리_50퍼센트에_닿아야_무장한다(env):
    _open()
    _feed([10_100, 10_150])          # 목표거리 400 중 150 → 37.5%
    r = _row()
    assert r["armed"] == 0
    assert r["reach_pct"] == pytest.approx(37.5, abs=0.1)

    _feed([10_200])                  # 200/400 = 50% → 무장
    assert _row()["armed"] == 1


def test_무장_후_진입가로_되돌아오면_가상청산된다(env):
    _open()
    _feed([10_250, 10_120, 9_990])
    r = _row()
    assert r["armed"] == 1
    assert r["shadow_exit"] == 10_000      # 진입가에서 청산했다고 가정
    assert r["shadow_exit_at"]


def test_무장_전에_되돌아오면_가상청산이_없다(env):
    _open()
    _feed([10_050, 9_900])                 # 12.5% 만 갔다 → 무장 안 됨
    r = _row()
    assert r["armed"] == 0
    assert r["shadow_exit"] is None


def test_가상청산은_한_번만_기록된다(env):
    _open()
    _feed([10_250, 9_990])
    first = _row()["shadow_exit_at"]
    _feed([9_900, 10_300])                 # 이후 실제 포지션이 어디로 가든
    assert _row()["shadow_exit_at"] == first


def test_최유리_가격은_30초_표본만_본다(env):
    """봉 고가가 아니라 관측된 틱만 쓴다 — 실시간 구현이 보는 것과 같아야 한다."""
    _open()
    _feed([10_100, 10_300, 10_050])
    assert _row()["peak"] == 10_300
    assert _row()["mfe_pct"] == pytest.approx(3.0, abs=0.01)


# --------------------------------------------------------------------------
# 정산
# --------------------------------------------------------------------------
def test_가상청산이_손절보다_나았으면_델타가_양수다(env):
    _open()
    _feed([10_250, 9_990])                 # 무장 후 본전 복귀
    ledger.close_position("p1", 9_800, "stop")
    assert breakeven.settle() == 1
    r = _row()
    assert r["settled"] == 1
    assert r["shadow_pct"] > r["actual_pct"]
    assert r["delta_krw"] > 0


def test_본전_이동은_손익_본전이_아니다(env):
    """진입가에 팔아도 수수료·거래세가 남는다 — shadow 손익은 음수다."""
    _open()
    _feed([10_250, 9_990])
    ledger.close_position("p1", 9_800, "stop")
    breakeven.settle()
    assert _row()["shadow_pct"] < 0


def test_목표까지_간_건은_가상청산이_없어_델타가_0이다(env):
    _open()
    _feed([10_250, 10_400])                # 무장했지만 되돌아오지 않음
    ledger.close_position("p1", 10_400, "target")
    breakeven.settle()
    r = _row()
    assert r["shadow_exit"] is None
    assert r["delta_krw"] == 0
    assert r["armed"] == 1


def test_이겼을_건을_죽이면_델타가_음수다(env):
    """되돌아왔다가 다시 올라 목표에 닿은 건 — shadow 는 중간에 나가서 손해다."""
    _open()
    _feed([10_250, 9_990])                 # 여기서 shadow 청산
    ledger.close_position("p1", 10_400, "target")
    breakeven.settle()
    assert _row()["delta_krw"] < 0


def test_정산은_멱등이다(env):
    _open()
    _feed([10_250, 9_990])
    ledger.close_position("p1", 9_800, "stop")
    assert breakeven.settle() == 1
    assert breakeven.settle() == 0


# --------------------------------------------------------------------------
# 집계
# --------------------------------------------------------------------------
def test_report_는_유리_불리를_함께_센다(env):
    _open("p1", symbol="005930")
    _feed([10_250, 9_990], "p1")
    ledger.close_position("p1", 9_800, "stop")
    _open("p2", symbol="000660")
    _feed([10_250, 9_990], "p2")
    ledger.close_position("p2", 10_400, "target")
    breakeven.settle()
    rep = breakeven.report("2026-07-28")
    assert rep["trades"] == 2
    assert rep["triggered"] == 2
    assert rep["helped"] == 1 and rep["hurt"] == 1


def test_reset_은_관측치를_지운다(env):
    _open()
    _feed([10_250])
    assert breakeven.rows_on("2026-07-28")
    assert breakeven.reset() == 1
    assert breakeven.rows_on("2026-07-28") == []


# --------------------------------------------------------------------------
# 규약 — 여기부터가 이 기능의 경계다
# --------------------------------------------------------------------------
def test_shadow_는_실제_청산_판정을_바꾸지_않는다(env):
    """같은 가격을 due_exits 에 넣었을 때, shadow 유무로 결과가 달라지면 안 된다."""
    _open()
    price = 9_990                          # 무장 후 본전 복귀 — shadow 는 청산
    _feed([10_250, price])
    assert _row()["shadow_exit"] is not None

    due = ledger.due_exits(lambda _s: price)
    assert due == [], "shadow 가 청산됐다고 실제 포지션이 청산 대상이 되면 안 된다"

    pos = ledger.positions("open")
    assert len(pos) == 1
    assert pos[0]["status"] == "open"
    assert pos[0]["stop"] == 9_800, "실제 손절선이 진입가로 올라가면 안 된다"


def test_꺼져_있으면_아무것도_쓰지_않는다(env, monkeypatch):
    monkeypatch.setitem(settings.CONFIG, "execution",
                        {"breakeven_shadow": {"enabled": False}})
    _open()
    assert breakeven.observe(lambda _s: 10_300) == 0
    assert breakeven.rows_on("2026-07-28") == []


def test_일지_집계에_섞이지_않는다(env, monkeypatch, tmp_path):
    """breakeven_shadow 는 별도 칸이고 trades/by_rule 에 영향을 주지 않는다."""
    from app import journal

    monkeypatch.setattr(journal, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(journal, "JOURNAL_DIR", tmp_path / "journal")
    _open()
    _feed([10_250, 9_990])
    ledger.close_position("p1", 9_800, "stop")
    breakeven.settle()

    day = str(ledger.positions("closed")[0]["closed"])[:10]
    entry = journal.build(day)
    assert entry["breakeven_shadow"]["triggered"] == 1
    assert entry["trades"]["trades"] == 1
    # 실현손익은 실제 청산 그대로 — shadow 델타가 더해지면 안 된다
    assert entry["trades"]["pnl_krw"] == pytest.approx(
        ledger.positions("closed")[0]["pnl_krw"], abs=1)
