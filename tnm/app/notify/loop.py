"""알림 루프 (FR-08) — 60초 주기로 후보를 스캔해 rules.decide 대로 처리.

- 발송 성공 시에만 tnm_notifications 기록 (실패 시 다음 주기 재시도 — 전송-기록 원자성)
- defer 는 기록하지 않고 두었다가 익일 07:00 패스에서 묶음 1건으로 발송
- suppress 는 'suppressed' 채널로 기록해 재시도를 멈추고, 일말 요약 1건으로 대체
- shadow_mode 면 어떤 것도 발송하지 않는다 (기록도 없음 — 후보로 남아
  실운영 전환 시점 이후 신규 항목부터 발송된다)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from .. import db, settings
from . import rules, slack

log = logging.getLogger("tnm.notify")
KST = ZoneInfo("Asia/Seoul")


class Notifier:
    def __init__(self) -> None:
        self.sent = 0
        self.deferred_digest_date = ""     # 익일 묶음 발송 완료 날짜
        self.last_error = ""

    def status(self) -> dict:
        return {"sent": self.sent, "last_error": self.last_error,
                "configured": slack.configured(),
                "shadow_mode": bool(settings.ALERTS.get("shadow_mode", True))}

    async def run_once(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(KST)
        cfg = settings.ALERTS
        result = {"sent": 0, "deferred": 0, "suppressed": 0, "digest": 0}
        if cfg.get("shadow_mode", True):
            return result                   # Shadow — 발송 원천 차단 (M8 전환 전)
        # 1) 조용시간 종료 후 하루 1회: 밤사이 보류분을 묶음 1건으로 (요청서 7.2)
        #    개별 발송보다 먼저 처리해 같은 항목이 개별로 새지 않게 한다.
        today = now.date().isoformat()
        if (not rules.in_quiet_hours(now, cfg)
                and self.deferred_digest_date != today):
            overnight = await db.pending_alerts(limit=100)
            night = [x for x in overnight if x.get("category")
                     not in set(cfg.get("urgent_categories", []) or [])]
            if len(night) >= 2:             # 1건이면 굳이 묶지 않고 개별 발송에 맡긴다
                ok, err = await slack.post(rules.format_digest(night))
                if ok:
                    for x in night:
                        await db.insert_notification(x["id"], "slack", True)
                    result["digest"] = len(night)
                    self.sent += len(night)
                    self.deferred_digest_date = today
                else:
                    self.last_error = err
            else:
                self.deferred_digest_date = today
        # 2) 개별 발송
        candidates = await db.pending_alerts(limit=50)
        for it in candidates:
            sent_today = await db.notifications_today(it["ticker"])
            action = rules.decide(now, it.get("category"), sent_today, cfg)
            if action == "defer":
                result["deferred"] += 1
                continue                    # 기록 없음 — 익일 묶음 패스가 처리
            if action == "suppress":
                await db.insert_notification(it["id"], "suppressed", False)
                result["suppressed"] += 1
                continue
            ok, err = await slack.post(rules.format_message(it))
            if ok:
                await db.insert_notification(it["id"], "slack", False)
                self.sent += 1
                result["sent"] += 1
                self.last_error = ""
            else:                           # 실패 — 기록하지 않고 다음 주기 재시도
                self.last_error = err
                log.warning("슬랙 발송 실패(재시도 예정): %s", err)
                break
        return result

    async def loop(self) -> None:
        await asyncio.sleep(40)
        while True:
            try:
                if db.ready:
                    await self.run_once()
            except Exception:  # noqa: BLE001
                log.exception("알림 루프 오류")
            await asyncio.sleep(60)
