"""슬랙 발송 — Bot Token + chat.postMessage (httpx POST 하나)."""
from __future__ import annotations

import logging

import httpx

from .. import settings

log = logging.getLogger("tnm.slack")

_API = "https://slack.com/api/chat.postMessage"


def configured() -> bool:
    return bool(settings.SLACK_BOT_TOKEN and settings.SLACK_CHANNEL)


async def post(text: str) -> tuple[bool, str]:
    """반환: (성공 여부, 오류 메시지). 실패 시 호출자가 다음 주기에 재시도한다."""
    if not configured():
        return False, "SLACK_BOT_TOKEN/SLACK_CHANNEL 미설정"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(_API, json={
                "channel": settings.SLACK_CHANNEL,
                "text": text,
                "unfurl_links": False,
            }, headers={"Authorization": f"Bearer {settings.SLACK_BOT_TOKEN}"})
            r.raise_for_status()
            body = r.json()
            if not body.get("ok"):
                return False, str(body.get("error", "unknown"))
            return True, ""
    except (httpx.HTTPError, ValueError) as e:
        return False, str(e)
