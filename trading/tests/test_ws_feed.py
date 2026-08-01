"""WS 피드 — 증분 등록·좀비 판정·수신 지연 계측·데스크 REST 스로틀."""
import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.data import collector
from app.kiwoom import ws as ws_mod

KST = ZoneInfo("Asia/Seoul")
SESSION_NOW = datetime(2026, 7, 27, 10, 0, tzinfo=KST)      # 월요일 장중
OFF_NOW = datetime(2026, 7, 25, 10, 0, tzinfo=KST)          # 토요일


class FakeWS:
    def __init__(self):
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self):
        self.closed = True


async def _noop(*a):
    return None


def _feed_with_ws(symbols):
    feed = ws_mod.RealtimeFeed(_noop)
    feed._symbols = set(symbols)
    feed._ws = FakeWS()
    return feed


def test_추가는_REG_제거는_REMOVE_재접속_없음():
    feed = _feed_with_ws(["000001", "000002"])
    asyncio.run(feed.update(["000001", "000003"]))
    ws = feed._ws
    assert not ws.closed                       # 연결 유지 — 이 개선의 핵심
    trnms = [(m["trnm"], m["data"][0]["item"]) for m in ws.sent]
    assert ("REG", ["000003"]) in trnms
    assert ("REMOVE", ["000002"]) in trnms
    assert feed.stats["incr_reg"] == 1 and feed.stats["incr_remove"] == 1


def test_집합이_같으면_아무것도_안_보낸다():
    feed = _feed_with_ws(["000001"])
    asyncio.run(feed.update(["000001"]))
    assert feed._ws.sent == []


def test_추가_전송_실패면_재접속_제거_실패는_유지():
    class SendFail(FakeWS):
        async def send(self, raw):
            raise ConnectionError("죽은 소켓")

    feed = ws_mod.RealtimeFeed(_noop)
    feed._symbols = {"000001"}
    feed._ws = SendFail()
    asyncio.run(feed.update(["000001", "000002"]))   # 추가 실패 → 강제 close
    assert feed._ws.closed
    assert feed.stats["incr_fail"] == 1

    feed2 = ws_mod.RealtimeFeed(_noop)
    feed2._symbols = {"000001", "000002"}
    feed2._ws = SendFail()
    asyncio.run(feed2.update(["000001"]))            # 제거만 실패 → 재접속 없음
    assert not feed2._ws.closed


def test_좀비_판정은_장중에만(monkeypatch):
    monkeypatch.setitem(collector.store.__dict__, "record_tick_lag",
                        lambda *a, **k: None)
    feed = _feed_with_ws(["000001"])
    feed._last_real = 1000.0
    # 장중 + 나이 20초 > 기본 15초 → 좀비
    assert feed.zombie(now_mono=1020.0, now=SESSION_NOW) is True
    # 나이 5초 → 정상
    assert feed.zombie(now_mono=1005.0, now=SESSION_NOW) is False
    # 주말이면 무틱이 정상 — 좀비 아님
    assert feed.zombie(now_mono=1020.0, now=OFF_NOW) is False
    # 구독이 없으면 판정하지 않는다
    feed._symbols = set()
    assert feed.zombie(now_mono=1020.0, now=SESSION_NOW) is False


def test_수신지연_계산():
    now = datetime(2026, 7, 27, 10, 0, 3, tzinfo=KST)
    assert collector.tick_lag_ms("100002", now) == 1000.0    # 1초 늦음
    assert collector.tick_lag_ms("100004", now) == 0.0       # 시계 오차 → 0 으로
    assert collector.tick_lag_ms("094500", now) is None      # 15분 전 — 파싱 오류 취급
    assert collector.tick_lag_ms("쓰레기", now) is None
    assert collector.tick_lag_ms("", now) is None


def test_수신지연_분당_원장(monkeypatch):
    rows = []
    monkeypatch.setattr(collector.store, "record_tick_lag",
                        lambda m, n, avg, mx: rows.append((m, n, avg, mx)))
    agg = collector.BarAggregator()
    monkeypatch.setattr(collector.store, "upsert_bars", lambda *a: 0)
    m1 = datetime(2026, 7, 27, 10, 0, tzinfo=KST)
    m2 = datetime(2026, 7, 27, 10, 1, tzinfo=KST)
    agg._lag_add(100.0, m1)
    agg._lag_add(300.0, m1)
    agg._lag_add(50.0, m2)         # 분이 바뀌면 직전 분을 원장에 접는다
    assert rows == [(m1.isoformat(), 2, 200.0, 300.0)]
    assert agg.lag_last["n"] == 2 and agg.lag_last["avg_ms"] == 200.0


def test_데스크_REST_는_stale_sec_당_1회(monkeypatch):
    from app.trade import desk

    calls = {"n": 0}

    async def rest_price(sym):
        calls["n"] += 1
        return 1000.0

    monkeypatch.setattr(desk.ledger, "positions",
                        lambda **k: [{"id": "x1", "symbol": "000001", "side": "long",
                                      "entry": 1000.0, "stop": 900.0, "target": 1100.0,
                                      "qty": 1, "exit_pending": 0}])
    monkeypatch.setattr(desk.ledger, "effective_lines", lambda p: (900.0, 1100.0))
    monkeypatch.setattr(desk, "update_lines", lambda p, px: None)
    desk._REST_CACHE.clear()
    now = datetime(2026, 7, 27, 10, 0, tzinfo=KST)
    for _ in range(3):             # 2초 루프 3사이클을 흉내 — 전부 stale
        st = asyncio.run(desk.tick(lambda s: None, rest_price, _noop, now=now))
    assert calls["n"] == 1         # 종전에는 3콜이 나갔다
    assert st["rest_cache"] == 1 and st["watched"] == 1
