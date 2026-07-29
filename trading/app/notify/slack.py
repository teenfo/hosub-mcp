"""슬랙 발송 — Bot Token + chat.postMessage.

TNM 의 `notify/slack.py` 와 같은 방식이고 토큰·채널도 같은 값을 쓴다. 그런데
TNM 을 경유하지 않고 **직접 보낸다** — 경유하면 TNM 이 죽었을 때 매매 알림까지
멈춘다. 매매 알림은 뉴스 알림보다 급하고, 두 서비스의 수명주기를 묶을 이유가 없다.

발송 실패는 로그만 남기고 삼킨다. 알림이 안 갔다고 주문 흐름을 멈추면 안 된다 —
알림은 곁다리이고 주문이 본체다.
"""
from __future__ import annotations

import logging

import httpx

from .. import settings

log = logging.getLogger("trading.slack")

_API = "https://slack.com/api/chat.postMessage"


def configured() -> bool:
    return bool(settings.SLACK_BOT_TOKEN and settings.SLACK_CHANNEL)


async def post(text: str) -> tuple[bool, str]:
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


async def notify(text: str) -> None:
    """실패해도 예외를 올리지 않는다 — 알림은 주문 흐름을 막을 자격이 없다."""
    if not configured():
        return
    ok, err = await post(text)
    if not ok:
        log.warning("슬랙 발송 실패: %s", err)


def pending_order_text(rec: dict) -> str:
    """승인대기 주문 1건 → 슬랙 문구.

    사람이 폰에서 보고 **승인할지 말지 정할 수 있을 만큼**만 담는다. 종목·규칙·
    가격·수량·필요 금액, 그리고 지금 살 수 있는가. 자금이 모자란 건은 그 사실이
    제목에 드러나야 한다 — 열어 봐야 아는 정보는 알림의 값을 깎는다.
    """
    name, code = rec.get("name") or rec.get("symbol"), rec.get("symbol")
    qty = int(rec.get("qty") or 0)
    entry = float(rec.get("entry") or 0)
    need = int(rec.get("need_cash") or qty * entry)
    if rec.get("over_weight"):
        head = "⚠️ 비중 상한 초과 — 수동 승인만"
    elif rec.get("fundable"):
        head = "🟢 승인대기"
    else:
        head = "🟡 자금 부족 — 승인대기"
    lines = [
        f"{head}  *{name}* ({code})",
        f"• {rec.get('rule')} · {rec.get('side')} · {qty}주 @ {entry:,.0f}원"
        f" = {need:,.0f}원",
        f"• 손절 {float(rec.get('stop') or 0):,.0f} / 목표 "
        f"{float(rec.get('target') or 0):,.0f}",
    ]
    if rec.get("reason"):
        lines.append(f"• 사유: {rec['reason']}")
    if rec.get("note"):
        lines.append(f"• {rec['note']}")
    if rec.get("guard_warn"):
        lines.append(f"• ⚠️ {rec['guard_warn']}")
    return "\n".join(lines)
