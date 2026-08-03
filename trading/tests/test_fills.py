"""실거래 체결 원장(broker_fills) — 저장·정렬·가드 주값 전환."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import settings
from app.trade import fills, ledger

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 7, 27, 10, 0, tzinfo=KST)      # 월요일 장중
DAY = "2026-07-27"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "trading.db")
    return tmp_path


def _order(oid="ord000000001", symbol="005930", qty=10, ord_no="0001111"):
    order = {"id": oid, "symbol": symbol, "entry": 10_000.0, "stop": 9_800.0,
             "target": 10_400.0, "side": "long", "qty": qty, "rule": "orb"}
    ledger.open_position(order, fill=10_000.0, ord_no=ord_no)
    return order


def _fill(ord_no="0001111", code="005930", side="buy", qty=10, price=10_000.0,
          t="095900", fee=10.0, tax=0.0):
    return {"ord_no": ord_no, "code": code, "name": "테스트", "side": side,
            "qty": qty, "price": price, "commission": fee, "tax": tax, "time": t}


def test_스냅샷_저장은_교체라_멱등이다(env):
    fills.store_today([_fill(), _fill(t="100100", qty=5)], DAY)
    assert len(fills.today_fills(DAY)) == 2
    fills.store_today([_fill()], DAY)          # 재동기화 → 통째 교체
    assert len(fills.today_fills(DAY)) == 1


def test_유령은_체결내역에_없으면_void(env, monkeypatch):
    monkeypatch.setattr(ledger, "KST", KST)
    _order(ord_no="0001111")                    # 체결내역에 있음
    _order(oid="ord000000002", symbol="000660", ord_no="0009999")   # 없음 — 유령
    fills.store_today([_fill()], day=datetime.now(KST).date().isoformat())
    got = fills.sync_from_fills(day=datetime.now(KST).date().isoformat(),
                                holdings=None,
                                now=datetime.now(KST) + timedelta(minutes=5))
    assert got["ghost"] == 1
    opens = ledger.positions(status="open")
    assert [p["symbol"] for p in opens] == ["005930"]


def test_유예_전에는_유령으로_안_잡는다(env):
    _order(oid="ord000000003", symbol="000660", ord_no="0009999")
    day = datetime.now(KST).date().isoformat()
    fills.store_today([], day)
    got = fills.sync_from_fills(day, None,
                                now=datetime.now(KST) + timedelta(seconds=30))
    assert got["ghost"] == 0


def test_놓친_청산은_fills_가격으로_닫는다(env):
    _order(ord_no="0001111")
    day = datetime.now(KST).date().isoformat()
    # 진입 체결 + 매도 체결(놓친 청산) — 계좌 보유 0
    fills.store_today([
        _fill(),
        _fill(ord_no="0002222", side="sell", qty=6, price=10_200.0, t="103000"),
        _fill(ord_no="0002222", side="sell", qty=4, price=10_100.0, t="103001"),
    ], day)
    got = fills.sync_from_fills(day, holdings={"005930": 0},
                                now=datetime.now(KST) + timedelta(minutes=5))
    assert got["missed_exit"] == 1
    p = ledger.positions(status="closed")[0]
    assert p["exit_reason"] == "fills_sync"
    assert p["exit"] == 10_160.0               # 수량가중 (6×10200+4×10100)/10
    assert p["exit_ord_no"] == "0002222"


def test_보유값이_없으면_청산하지_않는다(env):
    _order(ord_no="0001111")
    day = datetime.now(KST).date().isoformat()
    fills.store_today([_fill(ord_no="0002222", side="sell", qty=10,
                             price=10_200.0)], day)
    got = fills.sync_from_fills(day, holdings=None)      # 계좌 조회 실패
    assert got["missed_exit"] == 0


def test_수동_매매는_external_로_편입되고_왕복이면_닫힌다(env):
    day = datetime.now(KST).date().isoformat()
    fills.store_today([_fill(ord_no="0777777", code="035420", qty=3,
                             price=200_000.0)], day)
    got = fills.sync_from_fills(day, None, now=datetime.now(KST))
    assert got["external"] == 1
    ext = [p for p in ledger.positions(status="open") if p["rule"] == "external"]
    assert len(ext) == 1 and ext[0]["qty"] == 3 and ext[0]["stop"] is None
    # 같은 날 전량 매도 → 실비용 손익으로 닫힘
    fills.store_today([
        _fill(ord_no="0777777", code="035420", qty=3, price=200_000.0),
        _fill(ord_no="0777778", code="035420", side="sell", qty=3,
              price=202_000.0, t="140000", fee=10.0, tax=1_212.0),
    ], day)
    got = fills.sync_from_fills(day, None, now=datetime.now(KST))
    assert got["external_closed"] == 1
    p = [r for r in ledger.positions(status="closed") if r["rule"] == "external"][0]
    # (202000-200000)×3 − 수수료 20 − 세 1,212 = 4,768
    assert p["pnl_krw"] == 4_768.0


def test_외부_포지션은_자동청산_경로에서_빠진다(env):
    day = datetime.now(KST).date().isoformat()
    fills.store_today([_fill(ord_no="0777777", code="035420", qty=3,
                             price=200_000.0)], day)
    fills.sync_from_fills(day, None)
    # due_exits 가 stop=None 인 외부 포지션에서 죽지 않고 건너뛴다
    assert ledger.due_exits(lambda s: 100.0, now=NOW) == []


def test_가드는_증권사값을_주값으로_모델은_폴백(env):
    _order(ord_no="0001111")
    pid = ledger.positions(status="open")[0]["id"]
    ledger.close_position(pid, 10_200.0, "target")
    # 증권사 동기화 전 — 모델값
    out = ledger.realized_today(1_000_000)
    assert out["source"] == "model" and out["model_krw"] == out["krw"]
    # 증권사 값 저장 후 — broker 주값, 모델은 병기
    day = datetime.now(KST).date().isoformat()
    fills.store_daily(day, {"realized": 1_234.0, "commission": 20, "tax": 61})
    out = ledger.realized_today(1_000_000)
    assert out["source"] == "broker" and out["krw"] == 1_234.0
    assert out["model_krw"] != out["krw"]


def test_대조_리포트(env):
    _order(ord_no="0001111")
    day = datetime.now(KST).date().isoformat()
    fills.store_today([_fill(),
                       _fill(ord_no="0777777", code="035420", qty=3,
                             price=200_000.0)], day)
    fills.store_daily(day, {"realized": 500.0})
    d = fills.divergence(day, holdings={"005930": 7})
    assert d["fills"] == 2
    assert d["broker_realized_krw"] == 500.0
    assert len(d["unmatched_fills"]) == 1              # 수동 매매 1건
    assert d["qty_mismatch"] == [{"symbol": "005930", "ledger_qty": 10,
                                  "account_qty": 7}]


# --- 일지 주값 전환 재료 (2026-08-03) ---

def test_broker_daily_for_는_신선도를_따지지_않는다(env):
    """가드(180초)와 달리 지난 거래일의 값은 확정치다 — 일지가 언제 읽어도 같다."""
    fills.store_daily("2026-07-31", {"realized": -14_656.0,
                                     "commission": 940, "tax": 6_336})
    row = fills.broker_daily_for("2026-07-31")
    assert row["realized"] == -14_656.0
    assert fills.broker_daily_for("2026-07-30") is None


@pytest.mark.asyncio
async def test_ensure_daily_지난날_저장분이_있으면_API_를_안_부른다(env, monkeypatch):
    monkeypatch.setattr(settings, "KIWOOM_APP_KEY", "k")
    fills.store_daily("2026-07-31", {"realized": -14_656.0})
    import app.kiwoom.client as mod

    class _Boom:
        async def daily_realized(self, *a, **k):
            raise AssertionError("지난 날짜 확정치가 있는데 API 를 불렀다")

    monkeypatch.setattr(mod, "client", _Boom())
    row = await fills.ensure_daily("2026-07-31")
    assert row["realized"] == -14_656.0


@pytest.mark.asyncio
async def test_ensure_daily_없는_날은_받아서_채운다(env, monkeypatch):
    monkeypatch.setattr(settings, "KIWOOM_APP_KEY", "k")
    import app.kiwoom.client as mod

    class _C:
        async def daily_realized(self, frm, to):
            assert (frm, to) == ("20260730", "20260730")
            return {"return_code": 0, "rlzt_pl": "-5055",
                    "trde_cmsn": "300", "trde_tax": "1500", "dt_rlzt_pl": []}

    monkeypatch.setattr(mod, "client", _C())
    row = await fills.ensure_daily("2026-07-30")
    assert row["realized"] == -5_055.0


# --- 빈 응답 가드 (실측 2026-08-03 15:22 — 토큰 사고 중 스냅샷·확정치 증발) ---

def test_빈_응답은_쌓인_스냅샷을_지우지_못한다(env):
    fills.store_today([_fill()], DAY)
    assert fills.store_today([], DAY) == 1          # 기존 1건 유지
    assert len(fills.today_fills(DAY)) == 1


def test_처음부터_빈_날은_빈_저장이_정상이다(env):
    assert fills.store_today([], DAY) == 0
    assert fills.today_fills(DAY) == []


def test_조회_실패_응답은_확정치를_덮지_않는다(env):
    fills.store_daily(DAY, {"realized": -198.0, "commission": 600})
    fills.store_daily(DAY, {"ok": False, "error": "조회 실패"})
    assert fills.broker_daily_for(DAY)["realized"] == -198.0


def test_전부_0_응답은_0_아닌_확정치를_덮지_않는다(env):
    fills.store_daily(DAY, {"realized": 3_884.0, "commission": 470, "tax": 3_166})
    fills.store_daily(DAY, {"realized": 0, "commission": 0, "tax": 0})
    assert fills.broker_daily_for(DAY)["realized"] == 3_884.0


def test_거래_없는_날의_정당한_0_은_저장된다(env):
    fills.store_daily(DAY, {"realized": 0, "commission": 0, "tax": 0})
    assert fills.broker_daily_for(DAY)["realized"] == 0.0
