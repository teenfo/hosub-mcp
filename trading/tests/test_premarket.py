"""NXT 프리마켓 관측 — 우리가 못 보던 한 시간.

`stex_tp` 를 실호출로 확정했다(2026-07-31): 1=KRX · 2=NXT(`005930_NX`) ·
3=통합(`000660_AL`). 그때까지 순위 3종·관측 2종·주문·잔고가 전부 KRX 였다.
"""
import asyncio
from datetime import datetime

import pytest

from app.kiwoom import venue
from app.scout import premarket, store

KST = store.KST


# --- 코드 정규화 ---------------------------------------------------------------

@pytest.mark.parametrize("raw, want", [
    ("005930_NX", ("005930", venue.NXT)),
    ("000660_AL", ("000660", venue.ALL)),
    ("000660", ("000660", venue.KRX)),
    ("A005930", ("005930", venue.KRX)),       # 일부 TR 이 A 를 붙인다
    ("", ("", venue.KRX)),
])
def test_거래소_접미사를_벗긴다(raw, want):
    assert venue.split_venue(raw) == want


def test_정규화는_유효성을_판정하지_않는다():
    """'6자리 숫자인가' 를 여기서 걸러 버리면 parse_stock_list 가 저지른 것과
    같은 침묵이 된다 — 종목이 사라진 줄도 모른다."""
    assert venue.split_venue("이상한값") == ("이상한값", venue.KRX)


# --- 응답 → 적재 행 -------------------------------------------------------------

def _payload(codes):
    return {"trde_prica_upper": [
        {"stk_cd": c, "stk_nm": "종목%s" % c, "cur_prc": "+1000",
         "flu_rt": "+2.5", "trde_prica": "12345", "now_trde_qty": "500"}
        for c in codes]}


def test_NXT_행만_남기고_코드를_벗긴다():
    rows = premarket.to_rows(_payload(["005930_NX", "000660_NX"]))
    assert [r["code"] for r in rows] == ["005930", "000660"]
    assert all(r["venue"] == venue.NXT for r in rows)
    assert [r["rank"] for r in rows] == [1, 2]
    assert rows[0]["change_pct"] == 2.5 and rows[0]["price"] == 1000


def test_KRX_행이_섞여_오면_버린다():
    """NXT 를 요청했는데 접미사가 없는 행이 오면 거래소 구분이 없는 응답이다.
    그대로 섞어 쌓으면 나중에 이 표가 무엇의 기록인지 말할 수 없다."""
    rows = premarket.to_rows(_payload(["005930_NX", "000660", "035420_AL"]))
    assert [r["code"] for r in rows] == ["005930"]


def test_코드가_6자리_숫자가_아니면_버린다():
    assert premarket.to_rows(_payload(["ABCDEF_NX"])) == []


def test_빈_응답은_빈_목록():
    assert premarket.to_rows({}) == []


# --- 창 판정 --------------------------------------------------------------------

@pytest.mark.parametrize("hhmm, want", [
    ("07:59", False), ("08:00", True), ("08:30", True),
    ("08:50", True), ("08:51", False), ("09:05", False),
])
def test_프리마켓_창(monkeypatch, hhmm, want):
    monkeypatch.setattr(premarket, "_cfg", lambda: {})
    h, m = int(hhmm[:2]), int(hhmm[3:])
    assert premarket.in_window(datetime(2026, 8, 3, h, m, tzinfo=KST)) is want


def test_주말은_돌지_않는다(monkeypatch):
    monkeypatch.setattr(premarket, "_cfg", lambda: {})
    assert premarket.in_window(datetime(2026, 8, 1, 8, 30, tzinfo=KST)) is False  # 토
    assert premarket.in_window(datetime(2026, 8, 3, 8, 30, tzinfo=KST)) is True   # 월


def test_창은_설정으로_바꾼다(monkeypatch):
    """애프터마켓(15:30~20:00)도 같은 코드로 볼 수 있어야 한다."""
    monkeypatch.setattr(premarket, "_cfg",
                        lambda: {"start": "15:30", "end": "20:00"})
    assert premarket.window() == ("15:30", "20:00")
    assert premarket.in_window(datetime(2026, 8, 3, 18, 0, tzinfo=KST)) is True
    assert premarket.in_window(datetime(2026, 8, 3, 8, 30, tzinfo=KST)) is False


# --- 수집·적재 ------------------------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "scout.db")
    monkeypatch.setattr(premarket, "_cfg", lambda: {})


def test_시점별로_쌓는다_접지_않는다(db):
    """프리마켓 50분의 **경로**가 질문이지 평균이 아니다."""
    rows = premarket.to_rows(_payload(["005930_NX"]))
    assert store.record_premarket(rows, datetime(2026, 8, 3, 8, 10, tzinfo=KST)) == 1
    assert store.record_premarket(rows, datetime(2026, 8, 3, 8, 12, tzinfo=KST)) == 1
    with store._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM premarket_obs").fetchone()[0] == 2


def test_같은_시각_같은_종목은_한_행(db):
    rows = premarket.to_rows(_payload(["005930_NX"]))
    t = datetime(2026, 8, 3, 8, 10, tzinfo=KST)
    store.record_premarket(rows, t)
    store.record_premarket(rows, t)
    with store._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM premarket_obs").fetchone()[0] == 1


def test_창_밖에서는_호출하지_않는다(db, monkeypatch):
    called = []

    class Fake:
        async def trade_value_rank(self, market="000", stex_tp="1"):
            called.append(stex_tp)
            return _payload(["005930_NX"])

    import app.kiwoom.client as kc
    monkeypatch.setattr(kc, "client", Fake(), raising=False)
    assert asyncio.run(
        premarket.collect_once(datetime(2026, 8, 3, 9, 30, tzinfo=KST))) == 0
    assert called == []                       # API 를 아예 안 부른다


def test_창_안에서는_NXT_로_조회한다(db, monkeypatch):
    called = []

    class Fake:
        async def trade_value_rank(self, market="000", stex_tp="1"):
            called.append(stex_tp)
            return _payload(["005930_NX", "000660_NX"])

    import app.kiwoom.client as kc
    monkeypatch.setattr(kc, "client", Fake(), raising=False)
    n = asyncio.run(premarket.collect_once(datetime(2026, 8, 3, 8, 10, tzinfo=KST)))
    assert n == 2 and called == [venue.TP_NXT]
    assert store.premarket_days()[0]["codes"] == 2


def test_조회_실패는_삼킨다(db, monkeypatch):
    class Fake:
        async def trade_value_rank(self, market="000", stex_tp="1"):
            raise RuntimeError("서버 오류")

    import app.kiwoom.client as kc
    monkeypatch.setattr(kc, "client", Fake(), raising=False)
    assert asyncio.run(
        premarket.collect_once(datetime(2026, 8, 3, 8, 10, tzinfo=KST))) == 0


def test_기본_호출은_여전히_KRX(monkeypatch):
    """기존 호출부의 동작이 바뀌면 안 된다 — 기본값이 KRX 다."""
    import inspect

    from app.kiwoom.client import KiwoomClient
    sig = inspect.signature(KiwoomClient.trade_value_rank)
    assert sig.parameters["stex_tp"].default == venue.TP_KRX
