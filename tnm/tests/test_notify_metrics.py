"""M7·M8 완료판정 — Shadow 지표 산식·발송 판정 규칙·메시지 형식·발송 루프."""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from app import db, settings
from app.metrics import week_metrics
from app.notify import loop as loop_mod
from app.notify import rules, slack
from app.notify.loop import Notifier
from app.notify.rules import decide, format_message, in_quiet_hours

KST = ZoneInfo("Asia/Seoul")
CFG = {"shadow_mode": False, "quiet_start": "22:00", "quiet_end": "07:00",
       "urgent_categories": ["정정공시", "거래정지"], "default_daily_cap": 5}


def _t(hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 7, 28, h, m, tzinfo=KST)


# ---------- M7: 지표 산식 ----------

def test_week_metrics_formula():
    m = week_metrics({"week": "2026-07-27", "labeled": 20, "important": 10,
                      "tp": 9, "fp": 4, "fn": 1})
    assert m["precision"] == round(9 / 13, 3)
    assert m["recall"] == 0.9
    assert m["pass"] is True                      # 재현율 0.9·정밀도 0.69


def test_week_metrics_fail_and_empty():
    assert week_metrics({"tp": 5, "fp": 6, "fn": 3})["pass"] is False   # 재현율 0.625
    m = week_metrics({"tp": 0, "fp": 0, "fn": 0})
    assert m["precision"] is None and m["recall"] is None and m["pass"] is False


# ---------- M8: 발송 판정 (요청서 7.2, 시각 주입 — 결정론) ----------

def test_quiet_hours_midnight_wrap():
    assert in_quiet_hours(_t("22:30"), CFG)
    assert in_quiet_hours(_t("03:00"), CFG)
    assert not in_quiet_hours(_t("07:00"), CFG)   # 종료 경계는 발송 가능
    assert not in_quiet_hours(_t("12:00"), CFG)


def test_decide_shadow_blocks_everything():
    cfg = {**CFG, "shadow_mode": True}
    assert decide(_t("12:00"), "공급계약", 0, cfg) == "shadow"
    assert decide(_t("23:00"), "정정공시", 99, cfg) == "shadow"


def test_decide_quiet_defer_and_urgent_immediate():
    assert decide(_t("22:30"), "실적", 0, CFG) == "defer"
    assert decide(_t("22:30"), "정정공시", 0, CFG) == "send"   # 긴급은 조용시간에도
    assert decide(_t("10:00"), "실적", 0, CFG) == "send"


def test_decide_daily_cap():
    assert decide(_t("10:00"), "실적", 5, CFG) == "suppress"   # 상한 도달
    assert decide(_t("10:00"), "거래정지", 5, CFG) == "send"   # 긴급은 상한 무시


def test_format_message_spec():
    msg = format_message({"name": "효성중공업", "score": 82, "category": "공급계약",
                          "impact_direction": "positive", "impact_horizon": "short",
                          "summary": "북미 변압기 신규 수주 공시.",
                          "url": "https://dart.example/1"})
    lines = msg.split("\n")
    assert lines[0] == "[효성중공업] 82점 · 공급계약 · 긍정(단기)"
    assert lines[1].startswith("북미 변압기")
    assert lines[2] == "▸ 원문 보기: https://dart.example/1"     # 원문 링크 필수 (P5)


# ---------- M8: 발송 루프 ----------

def _wire(monkeypatch, candidates, sent_today=0):
    recorded, posted = [], []

    async def fake_pending(limit=50):
        # 실제 DB 처럼 이미 기록(발송/억제)된 항목은 후보에서 제외
        done = {aid for aid, _, _ in recorded}
        return [c for c in candidates if c["id"] not in done]

    async def fake_today(ticker):
        return sent_today

    async def fake_insert(aid, channel, deferred):
        recorded.append((aid, channel, deferred))

    async def fake_post(text):
        posted.append(text)
        return True, ""

    monkeypatch.setattr(db, "pending_alerts", fake_pending)
    monkeypatch.setattr(db, "notifications_today", fake_today)
    monkeypatch.setattr(db, "insert_notification", fake_insert)
    monkeypatch.setattr(loop_mod.slack, "post", fake_post)
    return recorded, posted


CAND = {"id": 1, "score": 82, "category": "공급계약", "impact_direction": "positive",
        "impact_horizon": "short", "summary": "요약", "title": "제목",
        "url": "https://x/1", "ticker": "298040", "name": "효성중공업"}


def test_notifier_shadow_sends_nothing(monkeypatch):
    n = Notifier()
    recorded, posted = _wire(monkeypatch, [CAND])
    monkeypatch.setattr(settings, "ALERTS", {**CFG, "shadow_mode": True})
    r = asyncio.run(n.run_once(_t("10:00")))
    assert posted == [] and recorded == [] and r["sent"] == 0


def test_notifier_sends_and_records(monkeypatch):
    n = Notifier()
    recorded, posted = _wire(monkeypatch, [CAND])
    monkeypatch.setattr(settings, "ALERTS", dict(CFG))
    r = asyncio.run(n.run_once(_t("10:00")))
    assert len(posted) == 1 and "[효성중공업] 82점" in posted[0]
    assert recorded == [(1, "slack", False)]      # 발송 성공 시에만 기록
    assert r["sent"] == 1


def test_notifier_failure_no_record(monkeypatch):
    """발송 실패 → 기록하지 않음 (다음 주기 재시도 — 전송·기록 원자성)."""
    n = Notifier()
    recorded, posted = _wire(monkeypatch, [CAND])

    async def fail_post(text):
        return False, "invalid_auth"

    monkeypatch.setattr(loop_mod.slack, "post", fail_post)
    monkeypatch.setattr(settings, "ALERTS", dict(CFG))
    r = asyncio.run(n.run_once(_t("10:00")))
    assert recorded == [] and r["sent"] == 0
    assert n.last_error == "invalid_auth"


def test_notifier_cap_suppresses(monkeypatch):
    n = Notifier()
    recorded, posted = _wire(monkeypatch, [CAND], sent_today=5)
    monkeypatch.setattr(settings, "ALERTS", dict(CFG))
    r = asyncio.run(n.run_once(_t("10:00")))
    assert posted == []                            # 상한 초과 — 발송 없음
    assert recorded == [(1, "suppressed", False)]  # 재시도 중단 마킹
    assert r["suppressed"] == 1


def test_slack_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "SLACK_BOT_TOKEN", "")
    ok, err = asyncio.run(slack.post("x"))
    assert not ok and "미설정" in err


def test_notifier_overnight_digest_bundles(monkeypatch):
    """조용시간 종료 후 첫 실행: 보류 2건 이상은 묶음 1건으로 (요청서 7.2)."""
    n = Notifier()
    c2 = {**CAND, "id": 2, "name": "삼성중공업", "ticker": "010140"}
    recorded, posted = _wire(monkeypatch, [CAND, c2])
    monkeypatch.setattr(settings, "ALERTS", dict(CFG))
    r = asyncio.run(n.run_once(_t("07:05")))
    assert len(posted) == 1 and posted[0].startswith("🌙 밤사이 알림 2건")
    assert set(recorded) == {(1, "slack", True), (2, "slack", True)}   # is_deferred
    assert r["digest"] == 2 and r["sent"] == 0
    # 같은 날 재실행 — 묶음은 반복되지 않음
    r2 = asyncio.run(n.run_once(_t("09:00")))
    assert r2["digest"] == 0 and len(posted) == 1
