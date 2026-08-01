"""키움 WebSocket 실시간 시세 구독.

접속 → LOGIN(token) → REG(체결 0B) 흐름. 수신 체결 틱은
on_tick(symbol, price, volume, ts) 콜백으로 전달.

## 구독 변경은 재접속이 아니라 증분 등록이다 (2026-08-01)

종전에는 감시목록이 한 종목만 바뀌어도 소켓을 닫고 LOGIN+REG 를 다시 했다.
실측 2026-07-31: 재접속이 **하루 102회**, 매번 ~30초의 틱 유실 창 — 기회비용
회전이 add/remove 를 만들 때마다 보유 종목 청산 감시가 같이 끊겼다.

키움 WS 는 trnm=REG(refresh="1" 기존 유지)/REMOVE 로 **연결을 유지한 채**
종목을 더하고 뺄 수 있다. 이제 update() 는:
  추가 → 살아 있는 소켓에 REG 만 보낸다 (틱 유실 0)
  제거 → REMOVE 를 보내되, 실패해도 재접속하지 않는다 — 구독이 남는 것은
        무해하다(소비자가 무시). 재접속은 **추가 실패**에만 쓴다
서버가 오류(return_code≠0)를 돌려주면 재접속으로 폴백해 전체 REG 로 복구한다.

## 좀비 감시 (2026-08-01)

실측 2026-07-31: 13시대에 연결은 살아 있는데 REAL 틱이 수십 분 동안 0건 —
보유 종목 감시가 통째로 REST 폴백(100%)에 빠졌고 감지 장치가 없었다.
15:03 케이스는 keepalive 가 잡았지만 복구에 4분이 걸렸다.

watchdog: 장중(평일 09:00~15:30)에 구독이 있는데 REAL 이 `zombie_sec`(기본
15초) 동안 0건이면 소켓을 강제로 닫는다 — 120종목 전부가 장중 15초간 무체결일
확률은 사실상 0이다. 닫으면 _run 루프가 즉시 재접속한다.
"""
import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from zoneinfo import ZoneInfo

import websockets

from .. import settings
from .auth import token_manager

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

TickHandler = Callable[[str, float, int, str], Awaitable[None]]
FillHandler = Callable[[dict], Awaitable[None]]


def _zombie_sec() -> float:
    cfg = (settings.CONFIG.get("execution", {}) or {}).get("ws", {})
    try:
        return max(5.0, float(cfg.get("zombie_sec", 15)))
    except (TypeError, ValueError):
        return 15.0


def in_session(now: datetime | None = None) -> bool:
    t = (now or datetime.now(KST)).astimezone(KST)
    return t.weekday() < 5 and "09:00" <= t.strftime("%H:%M") < "15:30"


class RealtimeFeed:
    def __init__(self, on_tick: TickHandler, on_fill: FillHandler | None = None) -> None:
        self.on_tick = on_tick
        self.on_fill = on_fill        # 주문체결 실시간(type 00) 콜백 — 실측 체결가 기록
        self._symbols: set[str] = set()
        self._task: asyncio.Task | None = None
        self._watch_task: asyncio.Task | None = None
        self._ws = None
        self._last_real: float | None = None   # 마지막 REAL 수신 (monotonic)
        # 계측 — "재접속이 몇 번, 증분이 몇 번" 이 이 개선의 성적표다
        self.stats = {"reconnects": 0, "zombie_kills": 0,
                      "incr_reg": 0, "incr_remove": 0, "incr_fail": 0}

    @property
    def connected(self) -> bool:
        """소켓이 붙어 있는가. 매매 데스크가 주기 강등을 판단하는 근거다 —
        끊긴 동안에는 WS 값이 전부 낡아 REST 폴백이 매 사이클 터진다."""
        return self._ws is not None

    def info(self) -> dict:
        age = None if self._last_real is None else round(
            time.monotonic() - self._last_real, 1)
        return {"connected": self.connected, "symbols": len(self._symbols),
                "last_real_age_sec": age, **self.stats}

    def start(self, symbols: list[str]) -> None:
        self._symbols = set(symbols)
        if not self._task or self._task.done():
            self._task = asyncio.create_task(self._run())
        if not self._watch_task or self._watch_task.done():
            self._watch_task = asyncio.create_task(self._watchdog())

    @staticmethod
    def _reg_payload(trnm: str, items: set[str]) -> str:
        return json.dumps({"trnm": trnm, "grp_no": "1", "refresh": "1",
                           "data": [{"item": sorted(items), "type": ["0B"]}]})

    async def update(self, symbols: list[str]) -> None:
        """구독 종목 변경 — **연결을 유지한 채** 증분 REG/REMOVE 를 보낸다.

        집합이 같으면 아무것도 하지 않는다. 재접속은 (a) 소켓이 없거나
        (b) 추가 REG 전송이 실패했을 때만 한다. 제거 실패는 무해하므로
        (잔여 틱은 소비자가 무시한다) 재접속 사유가 아니다.
        """
        new = set(symbols)
        if new == self._symbols and self._ws is not None:
            return
        old = self._symbols
        self._symbols = new
        ws = self._ws
        if ws is None:
            if not self._task or self._task.done():
                self._task = asyncio.create_task(self._run())
            if not self._watch_task or self._watch_task.done():
                self._watch_task = asyncio.create_task(self._watchdog())
            return
        added, removed = new - old, old - new
        if added:
            try:
                await ws.send(self._reg_payload("REG", added))
                self.stats["incr_reg"] += 1
            except Exception as e:  # noqa: BLE001 - 추가 실패 → 재접속으로 복구
                self.stats["incr_fail"] += 1
                log.warning("WS 증분 등록 실패(%s) — 재접속으로 전체 REG", e)
                await self._force_close()
                return
        if removed:
            try:
                await ws.send(self._reg_payload("REMOVE", removed))
                self.stats["incr_remove"] += 1
            except Exception:  # noqa: BLE001 - 제거 실패는 무해 — 재접속하지 않는다
                self.stats["incr_fail"] += 1
                log.info("WS 증분 해지 실패 — 잔여 구독은 무해하므로 유지")

    async def _force_close(self) -> None:
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    def zombie(self, now_mono: float | None = None,
               now: datetime | None = None) -> bool:
        """장중인데 REAL 이 zombie_sec 동안 0건인가 — watchdog 판정(테스트 대상)."""
        if self._ws is None or not self._symbols or not in_session(now):
            return False
        if self._last_real is None:
            return False
        age = (now_mono or time.monotonic()) - self._last_real
        return age > _zombie_sec()

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                if self.zombie():
                    age = time.monotonic() - (self._last_real or 0)
                    self.stats["zombie_kills"] += 1
                    log.warning("WS 좀비 의심 — 장중 %.0f초간 체결 틱 0건 (구독 %d종목), "
                                "강제 재접속", age, len(self._symbols))
                    self._last_real = None
                    await self._force_close()
            except Exception:  # noqa: BLE001 - 감시가 감시 대상을 죽이면 안 된다
                log.exception("WS watchdog 오류")

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
                self.stats["reconnects"] += 1
                # 접속 시각을 기준선으로 — "붙었는데 첫 REAL 이 영영 안 옴" 도 좀비다
                self._last_real = time.monotonic()
                await self._session(ws, token)
        finally:
            self._ws = None

    def _exec_type(self) -> str | None:
        cfg = settings.CONFIG.get("execution", {})
        if self.on_fill is None or not cfg.get("enabled", True):
            return None
        return str(cfg.get("rt_type", "00"))  # 00=주문체결(계좌 스코프)

    async def _session(self, ws, token: str) -> None:
        await ws.send(json.dumps({"trnm": "LOGIN", "token": token}))
        data = [{"item": sorted(self._symbols), "type": ["0B"]}]  # 0B=주식체결
        exec_type = self._exec_type()
        if exec_type:
            data.append({"item": [""], "type": [exec_type]})     # 주문체결(계좌)
        await ws.send(json.dumps(
            {"trnm": "REG", "grp_no": "1", "refresh": "1", "data": data}))
        async for raw in ws:
            msg = json.loads(raw)
            trnm = msg.get("trnm")
            if trnm == "PING":
                await ws.send(raw)  # 그대로 회신
                continue
            if trnm in ("REG", "REMOVE"):
                # 증분 등록/해지 응답 — 거부는 재접속으로 복구(전체 REG 가 다시 나간다)
                rc = str(msg.get("return_code", "0"))
                if rc not in ("0", "", "None"):
                    log.warning("WS %s 거부 rc=%s msg=%s — 재접속으로 전체 REG",
                                trnm, rc, msg.get("return_msg"))
                    self.stats["incr_fail"] += 1
                    break            # async-for 종료 → _connect_once 재접속
                continue
            if trnm != "REAL":
                continue
            self._last_real = time.monotonic()
            for item in msg.get("data", []):
                rtype = str(item.get("type") or item.get("name") or "")
                values = item.get("values", {})
                if exec_type and rtype == exec_type:
                    await self._handle_fill(values)
                    continue
                symbol = item.get("item", "")
                try:
                    price = abs(float(values.get("10", 0)))   # 현재가
                    volume = abs(int(float(values.get("15", 0))))  # 체결량
                    ts = values.get("20", "")                 # 체결시간 HHMMSS
                except (TypeError, ValueError):
                    continue
                if symbol and price:
                    await self.on_tick(symbol, price, volume, ts)

    async def _handle_fill(self, values: dict) -> None:
        """주문체결 실시간 수신 → 파싱해 on_fill 로 전달. FID 검증용으로 raw 도 로깅."""
        from ..trade import ledger

        try:
            fill = ledger.parse_execution(values)
        except Exception:  # noqa: BLE001
            log.warning("주문체결 파싱 실패 raw=%s", values)
            return
        log.info("주문체결 수신 ord_no=%s %s %s×%s (raw=%s)", fill.get("ord_no"),
                 fill.get("symbol"), fill.get("price"), fill.get("qty"), values)
        if self.on_fill and (fill.get("price") or fill.get("ord_no")):
            await self.on_fill(fill)
