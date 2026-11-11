"""계좌 TR 초당 1회 대응 — 키움 공지 2026-11-09(적용 2026-11-12 18시~)의 회귀 방지.

계좌 경로(/api/dostk/acnt) 호출은 앞 호출이 **끝난 뒤** 최소 간격을 두고 한 줄로
나간다. 주문·시세 경로는 그 줄에 묶이지 않는다. 계좌 한도 초과는 계좌 경로만 늦추고
(전역 감속 없음), 본문으로 온 한도 초과는 빈 결과가 아니라 조회 실패로 올라온다 —
빈 체결내역은 원장 정렬에서 오늘 연 포지션을 유령으로 회수할 수 있다.
"""
import asyncio
import time
from pathlib import Path

import httpx
import pytest
import yaml

from app import settings
from app.kiwoom import client as client_mod

GAP = 0.2       # 테스트는 짧은 간격으로 잰다 — 규칙은 실제 값(1.1초)과 같다
LIMIT_BODY = {"return_code": 5,
              "return_msg": "허용된 요청 개수를 초과하였습니다[1700:허용된 요청 개수를 "
                            "초과하였습니다. API ID=ka10076]"}


def _make(monkeypatch, handler, gap=GAP):
    monkeypatch.setitem(settings.CONFIG, "kiwoom", {"account_min_interval_sec": gap})
    kc = client_mod.KiwoomClient()
    kc._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(kc._limiter, "max_rps", 1000)      # 전역 리미터는 비켜 둔다
    monkeypatch.setattr(kc._limiter, "effective_rps", 1000)

    async def token():
        return "T"

    monkeypatch.setattr(client_mod.token_manager, "get", token)
    return kc


def _timed(log, latency=0.05, status=200, body=None):
    """요청마다 (경로, TR, 시작, 끝) 을 남기는 가짜 서버 — 응답에 latency 가 걸린다."""
    async def handler(request: httpx.Request) -> httpx.Response:
        start = time.monotonic()
        await asyncio.sleep(latency)
        log.append((request.url.path, request.headers["api-id"], start, time.monotonic()))
        return httpx.Response(status, json=body if body is not None else {"return_code": 0})
    return handler


async def test_계좌_호출은_앞_호출이_끝난_뒤_간격을_두고_한_줄로_나간다(monkeypatch):
    """_fills_sync 는 체결내역 → 잔고 → 실현손익을 연달아 부른다(1초 안에 3콜)."""
    log = []
    kc = _make(monkeypatch, _timed(log))
    await asyncio.gather(kc.executions(), kc.balance(),
                         kc.daily_realized("20261112", "20261112"))
    acct = sorted((r for r in log if r[0] == client_mod.PATH_ACCOUNT), key=lambda r: r[2])
    assert len(acct) == 3
    for prev, cur in zip(acct, acct[1:]):
        assert cur[2] >= prev[3] + GAP - 0.01, (prev, cur)   # 다음 시작 ≥ 앞 끝 + 간격
    assert kc._gates[client_mod.PATH_ACCOUNT].calls == 3


async def test_시세와_주문은_계좌_줄을_기다리지_않는다(monkeypatch):
    """청산 발주(PATH_ORDER)가 계좌 조회 뒤에 줄 서면 안 된다."""
    log = []
    kc = _make(monkeypatch, _timed(log), gap=0.5)
    t0 = time.monotonic()
    await kc.balance()
    await kc.quote("005930")
    await kc.order("sell", "005930", 1)
    await kc.cancel_order("0000001", "005930")
    assert time.monotonic() - t0 < 0.4                      # 계좌 간격(0.5초)을 안 기다렸다
    await kc.balance()                                      # 계좌끼리는 사이 호출과 무관하게 간격
    first, second = [r for r in log if r[0] == client_mod.PATH_ACCOUNT]
    assert second[2] >= first[3] + 0.5 - 0.01


async def test_계좌_한도_초과_429는_전역을_늦추지_않고_계좌_간격만_두_배로(monkeypatch):
    kc = _make(monkeypatch, _timed([], status=429, body=LIMIT_BODY))
    with pytest.raises(httpx.HTTPStatusError):
        await kc.balance()
    gate = kc._gates[client_mod.PATH_ACCOUNT]
    assert kc._limiter.effective_rps == 1000                # 분봉·시세·데스크는 그대로
    assert gate.penalties == 1 and gate.interval == pytest.approx(GAP * 2)
    assert kc.usage.rate_limited == 1


async def test_다른_경로의_429는_종전대로_전역을_감속한다(monkeypatch):
    kc = _make(monkeypatch, _timed([], status=429, body={}))
    with pytest.raises(httpx.HTTPStatusError):
        await kc.quote("005930")
    assert kc._limiter.effective_rps < 1000
    assert kc._gates[client_mod.PATH_ACCOUNT].penalties == 0


async def test_본문으로_온_한도_초과는_빈_체결내역이_아니라_조회_실패다(monkeypatch):
    """파서는 return_code 만 보고 빈 목록을 돌려준다 — 그게 '오늘 체결 없음'으로 읽히면
    원장 정렬 ①이 오늘 연 포지션을 유령으로 회수할 수 있다."""
    kc = _make(monkeypatch, _timed([], body=LIMIT_BODY))
    with pytest.raises(client_mod.KiwoomRateLimited):
        await kc.executions()
    gate = kc._gates[client_mod.PATH_ACCOUNT]
    assert gate.penalties == 1 and kc.usage.rate_limited == 1
    assert kc.usage_snapshot()["rate_limited_1h"] == 1


async def test_다른_오류_본문은_종전대로_돌려준다(monkeypatch):
    body = {"return_code": 20, "return_msg": "조회할 자료가 없습니다"}
    kc = _make(monkeypatch, _timed([], body=body))
    assert await kc.balance() == body
    assert kc._gates[client_mod.PATH_ACCOUNT].penalties == 0


async def test_앞_호출이_실패해도_다음_계좌_호출은_간격을_지킨다(monkeypatch):
    starts = []

    async def handler(request: httpx.Request) -> httpx.Response:
        starts.append(time.monotonic())
        if len(starts) == 1:
            raise httpx.ConnectError("끊김")
        return httpx.Response(200, json={"return_code": 0})

    kc = _make(monkeypatch, handler)
    with pytest.raises(httpx.ConnectError):
        await kc.balance()
    failed_at = time.monotonic()
    await kc.balance()
    assert starts[1] >= failed_at + GAP - 0.02


async def test_간격을_0으로_두면_끈다(monkeypatch):
    log = []
    kc = _make(monkeypatch, _timed(log, latency=0.01), gap=0)
    t0 = time.monotonic()
    await asyncio.gather(*(kc.balance() for _ in range(3)))
    assert time.monotonic() - t0 < 0.15 and len(log) == 3


async def test_체결내역이_한도_초과면_원장_정렬을_건너뛴다(monkeypatch):
    """조회 실패는 '모름'이다 — 스냅샷 저장도, 유령·놓친 청산 판정도 하지 않는다."""
    from app import main
    from app.trade import fills

    class _Limited:
        async def executions(self, *a, **k):
            raise client_mod.KiwoomRateLimited("ka10076: 허용된 요청 개수를 초과하였습니다")

    monkeypatch.setattr(client_mod, "client", _Limited())
    monkeypatch.setattr(main.settings, "KIWOOM_APP_KEY", "K")
    touched = []
    monkeypatch.setattr(fills, "store_today", lambda *a, **k: touched.append("store") or 0)
    monkeypatch.setattr(fills, "sync_from_fills", lambda *a, **k: touched.append("sync") or {})
    assert await main._fills_sync(force=True) is None
    assert touched == []


def test_사용량_스냅샷에_계좌_줄_관측치가_실린다():
    snap = client_mod.KiwoomClient().usage_snapshot()
    assert set(snap["account_gate"]) == {"interval_sec", "calls", "wait_sec", "penalties"}


def test_config_와_코드_기본값이_같다():
    cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml")
                         .read_text(encoding="utf-8"))
    assert (cfg["kiwoom"]["account_min_interval_sec"]
            == client_mod.ACCOUNT_MIN_INTERVAL_SEC == 1.1)
