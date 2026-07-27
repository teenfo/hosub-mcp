"""Slack 알림 — 사람이 개입해야 하는 순간에만 보낸다.

이 게이트웨이에는 사람이 모르면 조용히 멈추는 지점이 둘 있다.

1. **모델 설치 승인 대기** — 외부 서비스가 미설치 모델을 쓰면 잡이 pending 으로
   멈춘다. 대시보드를 열어보지 않으면 멈춘 줄 모른다.
2. **백엔드 오프라인** — 맥이 잠들거나 정전으로 꺼지면 야간 배치가 조용히 실패한다.

설계 원칙:
- **알림 실패가 파이프라인에 영향을 주면 안 된다.** 모든 호출은 예외를 삼키고
  로그만 남긴다. Slack 이 죽어도 잡은 계속 돈다.
- **상태 전이에서만 보낸다.** 맥이 꺼져 있는 30초마다 알리면 아무도 안 본다.
  online→offline 처럼 바뀌는 순간에만 한 번.
- **비밀은 절대 담지 않는다.** 토큰·프롬프트·응답 본문은 보내지 않는다.
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("llmgw.notify")

TIMEOUT = 5


class SlackNotifier:
    def __init__(self, webhook_url: str | None = None, *,
                 enabled: bool | None = None) -> None:
        self.webhook_url = (webhook_url
                            if webhook_url is not None
                            else os.environ.get("SLACK_WEBHOOK_URL", "")).strip()
        self.enabled = bool(self.webhook_url) if enabled is None else enabled
        # 같은 상태를 반복해서 알리지 않기 위한 마지막 전이 기록
        self._last: dict[str, str] = {}

    async def _post(self, text: str, blocks: list | None = None) -> None:
        if not self.enabled:
            return
        payload: dict = {"text": text}
        if blocks:
            payload["blocks"] = blocks
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                res = await client.post(self.webhook_url, json=payload)
            if res.status_code >= 400:
                # 웹훅 URL 이 폐기되면 400 이 온다 — 조용히 넘기지 말고 남긴다
                log.warning("Slack 알림 실패 HTTP %s: %s",
                            res.status_code, res.text[:200])
        except Exception as exc:
            # 알림 때문에 잡이 죽으면 안 된다
            log.warning("Slack 알림 전송 오류: %s: %s", type(exc).__name__, exc)

    def _changed(self, key: str, state: str) -> bool:
        """상태가 실제로 바뀌었을 때만 True. 반복 알림을 막는다."""
        if self._last.get(key) == state:
            return False
        self._last[key] = state
        return True

    def forget(self, key: str) -> None:
        """전이 기록을 지운다.

        모델을 삭제할 때 반드시 불러야 한다. 안 지우면 그 모델이 나중에 진짜로
        다시 필요해졌을 때 "이미 pending 을 알렸다" 며 알림이 **억제**된다.
        """
        self._last.pop(key, None)

    # ---------- 1) 모델 설치 승인 ----------
    async def model_approval_needed(self, req: dict) -> None:
        model = req.get("model", "?")
        if not self._changed(f"model:{model}", "pending"):
            return
        size = req.get("est_size_gb")
        roles = ", ".join(req.get("roles") or []) or "-"
        await self._post(
            f"[LLM 게이트웨이] 모델 설치 승인 필요: {model}",
            blocks=[
                {"type": "header",
                 "text": {"type": "plain_text", "text": "🧩 모델 설치 승인 필요"}},
                {"type": "section", "fields": [
                    {"type": "mrkdwn", "text": f"*모델*\n`{model}`"},
                    {"type": "mrkdwn",
                     "text": f"*예상 크기*\n{f'~{size}GB' if size else '미상'}"},
                    {"type": "mrkdwn",
                     "text": f"*요청 서비스*\n{req.get('requested_by') or '-'}"},
                    {"type": "mrkdwn", "text": f"*사용 역할*\n{roles}"},
                ]},
                {"type": "context", "elements": [{
                    "type": "mrkdwn",
                    "text": "승인 전까지 이 모델을 쓰는 잡은 대기합니다 "
                            "(다른 잡은 영향 없음). 대시보드 LLM 페이지에서 승인하세요.",
                }]},
            ],
        )

    async def model_install_finished(self, model: str, *, ok: bool,
                                     error: str | None = None) -> None:
        state = "ready" if ok else "failed"
        if not self._changed(f"model:{model}", state):
            return
        if ok:
            await self._post(f"[LLM 게이트웨이] 모델 설치 완료: {model} — 대기하던 잡이 재개됩니다")
        else:
            await self._post(
                f"[LLM 게이트웨이] 모델 설치 실패: {model} — {error or '원인 미상'}"
            )

    async def model_deleted(self, model: str, *, freed_gb: float | None = None,
                            actor: str | None = None) -> None:
        """모델 삭제는 상태 전이가 아니라 **사건**이라 _changed 를 거치지 않는다.
        (거쳤다면 지웠다가 다시 깔았을 때 두 번째 삭제가 조용히 묻힌다)"""
        freed = f" (~{freed_gb}GB 확보)" if freed_gb else ""
        by = f" by {actor}" if actor else ""
        await self._post(f"[LLM 게이트웨이] 모델 삭제: {model}{freed}{by}")

    # ---------- 2) 백엔드 장애 ----------
    async def backend_offline(self, base_url: str, error: str) -> None:
        if not self._changed("backend", "offline"):
            return
        await self._post(
            f"[LLM 게이트웨이] 맥 백엔드 응답 없음 — {base_url}",
            blocks=[
                {"type": "header",
                 "text": {"type": "plain_text", "text": "⚠️ LLM 백엔드 오프라인"}},
                {"type": "section", "text": {"type": "mrkdwn",
                 "text": f"*{base_url}* 에 닿지 않습니다.\n```{error[:300]}```"}},
                {"type": "context", "elements": [{
                    "type": "mrkdwn",
                    "text": "잡은 유실되지 않고 큐에 남아 복구 시 이어서 실행됩니다. "
                            "맥 잠자기·정전·Tailscale 키 만료를 확인하세요.",
                }]},
            ],
        )

    async def backend_online(self, base_url: str) -> None:
        # 처음 켜질 때(이전 상태 없음)는 알리지 않는다 — 기동 때마다 시끄럽다
        if "backend" not in self._last:
            self._last["backend"] = "online"
            return
        if not self._changed("backend", "online"):
            return
        await self._post(f"[LLM 게이트웨이] 맥 백엔드 복구됨 — {base_url}")
