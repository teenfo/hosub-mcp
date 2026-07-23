"""키움 WebSocket 실시간 시세 구독 (스켈레톤).

접속 → LOGIN(token) → REG(체결 0B) 흐름. 패킷 포맷은 공식 문서와 대조 후
모의투자에서 검증할 것. 수신 체결 틱은 on_tick(symbol, price, volume, ts) 콜백으로 전달.
"""
import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import websockets

from .. import settings
from .auth import token_manager

log = logging.getLogger(__name__)

TickHandler = Callable[[str, float, int, str], Awaitable[None]]


class RealtimeFeed:
    def __init__(self, on_tick: TickHandler) -> None:
        self.on_tick = on_tick
        self._symbols: set[str] = set()
        self._task: asyncio.Task | None = None
        self._ws = None

    def start(self, symbols: list[str]) -> None:
        self._symbols = set(symbols)
        if not self._task or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def update(self, symbols: list[str]) -> None:
        """구독 종목 변경 — 현재 연결을 닫아 재접속하며 새 목록으로 REG 한다."""
        self._symbols = set(symbols)
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        elif not self._task or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        backoff = 5
        while True:
            try:
                await self._connect_once()
                backoff = 5  # 정상 종료 후엔 빠르게 재접속
            except Exception as e:  # noqa: BLE001 - 재접속 루프
                log.warning("WS 재접속 %ds 후: %s", backoff, e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)  # 장 마감 등 지속 실패 시 완화

    async def _connect_once(self) -> None:
        token = await token_manager.get()
        try:
            async with websockets.connect(settings.WS_BASE) as ws:
                self._ws = ws
                await self._session(ws, token)
        finally:
            self._ws = None

    async def _session(self, ws, token: str) -> None:
        await ws.send(json.dumps({"trnm": "LOGIN", "token": token}))
        await ws.send(
            json.dumps(
                {
                    "trnm": "REG",
                    "grp_no": "1",
                    "refresh": "1",
                    "data": [
                        {"item": sorted(self._symbols), "type": ["0B"]}  # 0B=주식체결
                    ],
                }
            )
        )
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("trnm") == "PING":
                await ws.send(raw)  # 그대로 회신
                continue
            if msg.get("trnm") != "REAL":
                continue
            for item in msg.get("data", []):
                values = item.get("values", {})
                symbol = item.get("item", "")
                try:
                    price = abs(float(values.get("10", 0)))   # 현재가
                    volume = abs(int(float(values.get("15", 0))))  # 체결량
                    ts = values.get("20", "")                 # 체결시간 HHMMSS
                except (TypeError, ValueError):
                    continue
                if symbol and price:
                    await self.on_tick(symbol, price, volume, ts)
