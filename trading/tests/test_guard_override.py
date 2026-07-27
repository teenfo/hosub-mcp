"""일일 손실 가드 임시 해제.

안전장치를 스스로 여는 기능이라, 지켜야 할 것이 기능 자체보다 중요하다.
- 당일 실현손익은 절대 바뀌지 않는다(바뀌는 건 '멈추는 선'뿐)
- 오늘만 유효 · 시간 만료 · 하루 총량/횟수 상한
- 사용 이력이 남는다
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import settings
from app.signals.engine import SignalEngine
from app.trade import ledger, override

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 7, 27, 11, 0, tzinfo=KST)


@pytest.fixture
def ov(tmp_path, monkeypatch):
    monkeypatch.setattr(override, "FILE", tmp_path / "guard_override.json")
    monkeypatch.setitem(settings.RISK, "guard_override_max_pct", 2.0)
    monkeypatch.setitem(settings.RISK, "guard_override_max_count", 2)
    return tmp_path


# --- 부여·상태 ---

def test_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(override, "FILE", tmp_path / "g.json")
    monkeypatch.setitem(settings.RISK, "guard_override_max_pct", 0)
    monkeypatch.setitem(settings.RISK, "guard_override_max_count", 0)
    assert override.state(NOW)["enabled"] is False
    with pytest.raises(ValueError, match="꺼져 있습니다"):
        override.grant(mode="reset", extra_pct=1.0, pct_at=-1.5, now=NOW)


def test_reset_sets_absolute_total(ov):
    st = override.grant(mode="reset", extra_pct=1.86, pct_at=-1.86, now=NOW)
    assert st["active"] is True and st["extra_loss_pct"] == 1.86 and st["count"] == 1
    # 두 번째 리셋은 누적이 아니라 재설정 — 이미 앞의 해제가 손익에 반영돼 있다
    st = override.grant(mode="reset", extra_pct=1.9, pct_at=-1.9,
                        now=NOW + timedelta(minutes=10))
    assert st["extra_loss_pct"] == 1.9 and st["count"] == 2


def test_extend_accumulates(ov):
    override.grant(mode="extend", extra_pct=0.5, pct_at=-1.5, now=NOW)
    st = override.grant(mode="extend", extra_pct=0.5, pct_at=-1.9, now=NOW)
    assert st["extra_loss_pct"] == 1.0 and st["count"] == 2


def test_total_cap_enforced(ov):
    with pytest.raises(ValueError, match="총량 상한"):
        override.grant(mode="reset", extra_pct=2.5, pct_at=-2.5, now=NOW)
    override.grant(mode="extend", extra_pct=1.5, pct_at=-1.5, now=NOW)
    with pytest.raises(ValueError, match="총량 상한"):
        override.grant(mode="extend", extra_pct=1.0, pct_at=-2.0, now=NOW)


def test_count_cap_enforced(ov):
    override.grant(mode="extend", extra_pct=0.2, pct_at=-1.5, now=NOW)
    override.grant(mode="extend", extra_pct=0.2, pct_at=-1.6, now=NOW)
    with pytest.raises(ValueError, match="횟수 상한"):
        override.grant(mode="extend", extra_pct=0.2, pct_at=-1.7, now=NOW)


def test_rejects_bad_input(ov):
    with pytest.raises(ValueError, match="mode"):
        override.grant(mode="nope", extra_pct=1.0, pct_at=-1.5, now=NOW)
    with pytest.raises(ValueError, match="0보다"):
        override.grant(mode="extend", extra_pct=0, pct_at=-1.5, now=NOW)
    with pytest.raises(ValueError, match="1~600분"):
        override.grant(mode="extend", extra_pct=0.5, pct_at=-1.5, minutes=0, now=NOW)


# --- 만료 ---

def test_expires_after_minutes(ov):
    override.grant(mode="extend", extra_pct=0.5, pct_at=-1.5, minutes=30, now=NOW)
    assert override.state(NOW + timedelta(minutes=29))["active"] is True
    late = override.state(NOW + timedelta(minutes=31))
    assert late["active"] is False and late["extra_loss_pct"] == 0.0
    assert late["expired"] is True
    assert late["granted_pct"] == 0.5          # 오늘 준 총량은 이력으로 남는다


def test_defaults_to_market_close(ov):
    st = override.grant(mode="reset", extra_pct=1.0, pct_at=-1.0, now=NOW)
    assert st["until"].startswith("2026-07-27T15:30:00")
    assert override.state(datetime(2026, 7, 27, 15, 31, tzinfo=KST))["active"] is False


def test_rejects_after_close_without_minutes(ov):
    with pytest.raises(ValueError, match="장이 마감"):
        override.grant(mode="reset", extra_pct=1.0, pct_at=-1.0,
                       now=datetime(2026, 7, 27, 16, 0, tzinfo=KST))


def test_yesterdays_grant_does_not_carry_over(ov):
    override.grant(mode="reset", extra_pct=1.8, pct_at=-1.8, now=NOW)
    tomorrow = override.state(NOW + timedelta(days=1))
    assert tomorrow["active"] is False and tomorrow["count"] == 0


def test_corrupt_until_treated_as_expired(ov):
    override.grant(mode="extend", extra_pct=0.5, pct_at=-1.5, now=NOW)
    import json
    d = json.loads(override.FILE.read_text())
    d["until"] = "쓰레기"
    override.FILE.write_text(json.dumps(d, ensure_ascii=False))
    assert override.state(NOW)["active"] is False


# --- 취소·이력 ---

def test_clear_returns_to_configured_limit(ov):
    override.grant(mode="reset", extra_pct=1.5, pct_at=-1.5, now=NOW)
    st = override.clear(NOW)
    assert st["active"] is False and st["extra_loss_pct"] == 0.0
    assert st["count"] == 1                    # 사용 횟수는 되돌리지 않는다
    assert st["history"][-1]["mode"] == "clear"


def test_history_records_context(ov):
    override.grant(mode="reset", extra_pct=1.86, pct_at=-1.86, minutes=60, now=NOW)
    h = override.state(NOW)["history"][-1]
    assert h["mode"] == "reset" and h["extra_pct"] == 1.86
    assert h["pct_at"] == -1.86 and h["at"].startswith("2026-07-27T11:00")


def test_reset_extra_calculation():
    assert override.reset_extra(-1.86, 1.5) == 1.86     # 손실 구간
    assert override.reset_extra(0.5, 1.5) == 0.0        # 이익 구간이면 불필요
    assert override.reset_extra(0.0, 1.5) == 0.0


# --- 가드와의 결합 ---

def _engine(monkeypatch, ov_dir, pct):
    # 가드 결합 테스트는 '지금'을 그대로 쓴다(state 가 실시간 시계를 읽으므로).
    # 만료 시각만 시계에서 떼어 놓지 않으면 장 마감 후에 돌릴 때 grant 가
    # '이미 마감' 으로 거절해 테스트가 깨진다(2026-07-27 16:06 실측).
    # _close_at 자체의 동작은 별도 테스트가 검증한다.
    monkeypatch.setattr(override, "_close_at", lambda now: now + timedelta(hours=6))
    eng = SignalEngine(equity=1_000_000)
    monkeypatch.setattr(eng, "_effective_regime", lambda: "중립")
    monkeypatch.setattr(ledger, "realized_today",
                        lambda eq: {"krw": pct * 10_000, "pct": pct, "trades": 7})
    monkeypatch.setitem(settings.RISK, "daily_loss_limit_pct", 1.5)
    monkeypatch.setitem(settings.RISK, "daily_target_pct", 0)
    return eng


def test_guard_uses_effective_limit(ov, monkeypatch):
    eng = _engine(monkeypatch, ov, -1.86)
    g = eng.day_guard_status()
    assert g["halted"] is True and g["loss_limit_effective_pct"] == 1.5

    override.grant(mode="reset", extra_pct=1.86, pct_at=-1.86)
    g = eng.day_guard_status()
    assert g["halted"] is False                       # 자동 매매 재개
    assert g["loss_limit_effective_pct"] == 3.36
    assert g["daily_loss_limit_pct"] == 1.5           # 설정값은 그대로
    assert g["pct"] == -1.86 and g["krw"] == -18600   # 실현손익은 손대지 않는다


def test_guard_halts_again_past_extended_limit(ov, monkeypatch):
    eng = _engine(monkeypatch, ov, -2.1)              # 2.0% 초과
    override.grant(mode="extend", extra_pct=0.5, pct_at=-1.5)
    g = eng.day_guard_status()
    assert g["halted"] is True
    assert "임시 허용 +0.5% 반영 후에도 초과" in g["reason"]


def test_guard_restores_limit_after_expiry(ov, monkeypatch):
    eng = _engine(monkeypatch, ov, -1.86)
    override.grant(mode="reset", extra_pct=1.86, pct_at=-1.86, minutes=1)
    assert eng.day_guard_status()["halted"] is False
    # 만료 시각을 과거로 돌려 놓으면 다시 설정 한도로 복귀
    import json
    d = json.loads(override.FILE.read_text())
    d["until"] = (datetime.now(KST) - timedelta(minutes=1)).isoformat(timespec="seconds")
    override.FILE.write_text(json.dumps(d, ensure_ascii=False))
    g = eng.day_guard_status()
    assert g["halted"] is True and g["loss_limit_effective_pct"] == 1.5
