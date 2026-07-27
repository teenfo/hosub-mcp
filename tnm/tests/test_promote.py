"""고점수 뉴스 종목의 감시목록 편입 — **수집전용으로만**.

여기서 지켜야 할 것은 편입 자체가 아니라 **검증 전 신호로 매매하지 않는 것**이다.
발굴 3규칙에서 겪은 실수(하락장 데이터만 보고 유효하다고 판단 → 국면 분리에서
베타로 판명)를 뉴스 점수에서 반복하지 않기 위한 방어다.
"""
import asyncio

import pytest

from app import db, settings
from app.promote import ALLOWED_DIRECTIONS, Promoter, eligible


def _row(ticker, score, direction="긍정", name=None):
    return {"ticker": ticker, "name": name or f"종목{ticker}", "score": score,
            "impact_direction": direction, "category": "공급계약",
            "title": "수주 공시", "url": "https://x/1"}


# --- ① 후보 선별 ---

def test_eligible_filters_by_score():
    rows = [_row("000001", 80), _row("000002", 50)]
    assert [r["ticker"] for r in eligible(rows, 70, 5)] == ["000001"]


def test_eligible_sorts_by_score_desc():
    rows = [_row("000001", 72), _row("000002", 95), _row("000003", 80)]
    assert [r["ticker"] for r in eligible(rows, 70, 5)] == \
        ["000002", "000003", "000001"]


def test_eligible_caps_per_run():
    """감시목록이 뉴스로 급팽창하지 않게 — 사이클당 상한."""
    rows = [_row(f"00000{i}", 90) for i in range(9)]
    assert len(eligible(rows, 70, 3)) == 3


def test_eligible_one_per_ticker():
    rows = [_row("000001", 90), _row("000001", 85)]
    assert len(eligible(rows, 70, 5)) == 1


def test_eligible_skips_negative_news():
    """악재 종목을 감시목록에 넣을 이유가 없다."""
    rows = [_row("000001", 90, direction="부정"), _row("000002", 75, direction="긍정")]
    assert [r["ticker"] for r in eligible(rows, 70, 5)] == ["000002"]


def test_eligible_allows_neutral_and_unknown():
    rows = [_row("000001", 90, direction="중립"), _row("000002", 90, direction="")]
    assert len(eligible(rows, 70, 5)) == 2
    assert "긍정" in ALLOWED_DIRECTIONS


# --- ② 편입은 반드시 수집전용 ---

class _FakeClient:
    """트레이딩 API 대역 — 호출 순서를 그대로 기록한다."""

    def __init__(self, existing=(), fail_on=None):
        self.existing = list(existing)
        self.fail_on = fail_on or set()
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    class _R:
        def __init__(self, payload=None):
            self._p = payload or {}

        def raise_for_status(self):
            return None

        def json(self):
            return self._p

    async def get(self, url, headers=None):
        self.calls.append(("GET", url))
        return self._R({"entries": [{"code": c} for c in self.existing]})

    async def post(self, url, headers=None, json=None):
        path = url.split("/api/")[-1]
        self.calls.append(("POST", path, (json or {}).get("code")))
        if path in self.fail_on:
            raise OSError("트레이딩 오류")
        return self._R({"ok": True})


def _promoter(monkeypatch, rows, client):
    monkeypatch.setattr(db, "ready", True)
    monkeypatch.setattr(settings, "TRADING_TOKEN", "T")
    monkeypatch.setattr(settings, "TRADING_URL", "http://x")
    monkeypatch.setitem(settings.PROMOTE, "enabled", True)
    monkeypatch.setitem(settings.PROMOTE, "min_score", 70)

    async def fake_high(min_score, hours):
        return rows

    monkeypatch.setattr(db, "high_score_recent", fake_high)
    import app.promote as mod

    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **k: client)
    return Promoter()


def test_promote_marks_collect_only(monkeypatch):
    """편입 직후 반드시 수집전용으로 내려야 한다 — 검증 전 신호로 매매 금지."""
    client = _FakeClient()
    p = _promoter(monkeypatch, [_row("000001", 90)], client)
    out = asyncio.run(p.run_once())
    assert out["added"] == 1
    posts = [c for c in client.calls if c[0] == "POST"]
    assert posts[0][1] == "watchlist" and posts[1][1] == "watchlist/mode"


def test_promote_rolls_back_when_mode_fails(monkeypatch):
    """수집전용 지정이 실패하면 매매 대상으로 남는다 — 그건 허용할 수 없다."""
    client = _FakeClient(fail_on={"watchlist/mode"})
    p = _promoter(monkeypatch, [_row("000001", 90)], client)
    out = asyncio.run(p.run_once())
    assert out["added"] == 0 and out["errors"] == 1
    assert ("POST", "watchlist/remove", "000001") in client.calls


def test_promote_skips_already_watched(monkeypatch):
    client = _FakeClient(existing=["000001"])
    p = _promoter(monkeypatch, [_row("000001", 90)], client)
    out = asyncio.run(p.run_once())
    assert out["already"] == 1 and out["added"] == 0
    assert not [c for c in client.calls if c[0] == "POST"]


def test_promote_isolates_per_ticker_failure(monkeypatch):
    client = _FakeClient()
    original = client.post
    seen = {"n": 0}

    async def flaky(url, headers=None, json=None):
        if (json or {}).get("code") == "000001" and url.endswith("/watchlist"):
            seen["n"] += 1
            raise OSError("일시 오류")
        return await original(url, headers=headers, json=json)

    client.post = flaky
    p = _promoter(monkeypatch, [_row("000001", 95), _row("000002", 90)], client)
    out = asyncio.run(p.run_once())
    assert out["added"] == 1 and out["errors"] == 1
    assert out["tickers"] == ["000002"]


# --- ③ 안전장치 ---

def test_disabled_by_default_config_flag(monkeypatch):
    monkeypatch.setattr(db, "ready", True)
    monkeypatch.setitem(settings.PROMOTE, "enabled", False)
    assert "skipped" in asyncio.run(Promoter().run_once())


def test_skips_without_trading_token(monkeypatch):
    monkeypatch.setattr(db, "ready", True)
    monkeypatch.setitem(settings.PROMOTE, "enabled", True)
    monkeypatch.setattr(settings, "TRADING_TOKEN", "")
    assert "skipped" in asyncio.run(Promoter().run_once())


def test_skips_when_db_not_ready(monkeypatch):
    monkeypatch.setattr(db, "ready", False)
    monkeypatch.setitem(settings.PROMOTE, "enabled", True)
    assert "skipped" in asyncio.run(Promoter().run_once())


def test_watchlist_fetch_failure_does_not_add_blindly(monkeypatch):
    """감시목록을 모르면 중복 편입 위험 — 아무것도 추가하지 않는다."""
    client = _FakeClient()

    async def boom(url, headers=None):
        raise OSError("조회 실패")

    client.get = boom
    p = _promoter(monkeypatch, [_row("000001", 90)], client)
    out = asyncio.run(p.run_once())
    assert out.get("errors") == 1
    assert not [c for c in client.calls if c[0] == "POST"]


def test_config_promote_is_stricter_than_alerts():
    """편입은 알림보다 보수적이어야 한다 — 감시목록은 되돌리기 번거롭다."""
    assert settings.PROMOTE["min_score"] > settings.ALERTS["default_threshold"]
    assert settings.PROMOTE["max_per_run"] >= 1


def test_status_exposes_enabled():
    st = Promoter().status()
    assert "enabled" in st and "last_run" in st
