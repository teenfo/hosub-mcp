"""매매 데스크 — 보유 포지션만 빠르게 보는 청산 감시 루프.

## 왜 분리했나

2026-07-29 실측: 신호 엔진 사이클이 **40~50초**였다(설정은 `scan_interval_sec: 20`).
매매 tier 44종목 × 분봉 백필 1콜 = 사이클당 45콜, 그 버스트에만 17~19초가 든다.
감시목록이 커질수록 매매 판단이 그만큼 느려진다 — 발굴이 매매의 예산을 먹는 구조다.

청산 감시(`_ledger_loop`)는 그와 별개로 **30초 고정**이었다.

## 추가 API 호출은 거의 없다 — 이 설계의 핵심

가격은 이미 실시간이다. `BarAggregator` 가 WebSocket 틱으로 메모리에서 분봉을 만들고
있고, `snapshot()` 은 API 콜이 0이다. **30초였던 것은 가격이 아니라 판정 주기다.**

그래서 이 루프는 "보유 5건을 1~3초마다 **호출**" 하지 않는다.
**"1~3초마다 판정하고, WS 값이 낡았을 때만 REST 로 보충"** 한다.

    WS 틱 ──▶ aggregator.fresh_price(stale_sec)  ← 콜 0
                     └─ None(낡음/없음) ──▶ REST 보충 (그 종목만)

거래가 뜸해 틱이 안 오는 종목만 REST 를 타므로, 최악에도 보유 5종목 × (1/stale_sec)
콜이다. 실제로는 거의 안 걸린다 — 그 사실을 계측해 화면에 노출한다.

## 1~3초 루프가 만드는 새 위험

주기를 15배 올리면 30초를 전제하던 것들이 깨진다. 막아둔 것:

- **중복 청산 발주** — 발주 왕복 안에 다음 사이클이 온다. `exit_pending` 을 발주
  **전에** 세우고 실패했을 때만 푼다.
- **WS 끊김 시 폭주** — 재접속 중엔 전 종목이 낡아 매 사이클 REST 를 탄다.
  WS 미연결이면 **주기를 mid 수준으로 자동 강등**한다.
- **DB 쓰기 경합** — 라인 갱신은 값이 바뀔 때만 쓴다(`ledger.set_lines`).
- **되돌리기** — `execution.desk.enabled: false` 면 루프가 뜨지 않고 기존 30초 경로가
  그대로 돈다. 배포 없이 런타임에서 끈다.

## 손절·익절 라인 갱신

`update_lines()` 가 그 자리다. **아직 규칙이 없다** — 사용자가 지정하면 이 함수만
채운다. 계산 재료는 이미 있다(`breakeven.observe` 가 MFE·도달률을 추적 중이다).
지금은 항상 '변화 없음'을 돌려주므로 기존 손절·목표가 그대로 쓰인다.
"""
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from .. import settings
from . import ledger

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
SESSION = ("09:00", "15:30")

DEFAULTS = {
    "enabled": False,        # 실거래 청산을 다루므로 배포와 활성화를 분리한다
    "interval_sec": 2.0,     # 현행 30초의 15배. 1초는 발주 왕복·SQLite 쓰기와 경합
    "stale_sec": 5.0,        # 이보다 오래된 WS 값은 못 믿는다 → REST 보충
    "max_symbols": 5,        # max_positions 와 같게. 초과분은 기존 30초 루프가 맡는다
    "degraded_interval_sec": 30.0,   # WS 미연결 시 강등 주기
}


def cfg() -> dict:
    c = (settings.CONFIG.get("execution", {}) or {}).get("desk", {})
    return DEFAULTS | (c if isinstance(c, dict) else {})


def enabled() -> bool:
    return bool(cfg().get("enabled", False))


def _num(key: str) -> float:
    try:
        return float(cfg().get(key, DEFAULTS[key]))
    except (TypeError, ValueError):
        return float(DEFAULTS[key])


def stale_sec() -> float:
    """WS 값을 믿을 수 있는 최대 나이(초). 이보다 오래되면 REST 로 보충한다."""
    return _num("stale_sec")


def in_session(now: datetime | None = None) -> bool:
    t = now or datetime.now(KST)
    return t.weekday() < 5 and SESSION[0] <= t.strftime("%H:%M") <= SESSION[1]


# --------------------------------------------------------------------------
# 라인 갱신 훅 — 규칙은 아직 없다
# --------------------------------------------------------------------------
def update_lines(pos: dict, price: float) -> tuple[float | None, float | None] | None:
    """손절·익절선을 다시 계산한다. None 이면 '변화 없음'.

    **아직 규칙이 정해지지 않았다.** 사용자가 지침을 주면 여기에 들어온다.
    지금은 항상 None 을 돌려주므로 진입 시 고정된 손절·목표가 그대로 쓰인다 —
    즉 이 훅이 켜져 있어도 매매 동작은 지금과 같다.

    규칙을 넣을 때 지켜야 할 것:
    - `pos["stop"]`/`pos["target"]` 은 **원본**이다. 되돌릴 기준이므로 읽기만 한다
    - 롱에서 손절선을 **내리지 않는다**(트레일링은 한 방향이다). 내리면 손실이 커진다
    - 계산 재료는 `breakeven.observe` 가 이미 추적 중인 MFE·도달률을 쓴다
    """
    return None


# --------------------------------------------------------------------------
# 한 사이클
# --------------------------------------------------------------------------
async def tick(fresh_price, rest_price, execute_exit, now: datetime | None = None) -> dict:
    """보유 포지션 1회 판정. 반환: 계측값(화면·로그용).

    fresh_price(symbol) -> float | None   : WS 값. 낡았으면 None (콜 0)
    rest_price(symbol)  -> float | None   : REST 보충 (async)
    execute_exit(pos, reason, px)         : 청산 발주 (async)

    전부 주입받는 이유는 **WS 가 있을 때 REST 를 부르지 않는다**를 테스트로 고정하기
    위해서다. 이 설계의 값이 거기서 나온다 — 콜이 늘면 분리한 의미가 없다.
    """
    now = now or datetime.now(KST)
    stat = {"watched": 0, "ws": 0, "rest": 0, "no_price": 0, "exits": 0, "lines": 0}
    limit = int(_num("max_symbols"))
    rows = [p for p in ledger.positions(status="open", limit=200)
            if not p.get("exit_pending")][:limit]
    stat["watched"] = len(rows)

    for pos in rows:
        px = fresh_price(pos["symbol"])
        if px is not None:
            stat["ws"] += 1
        else:
            px = await rest_price(pos["symbol"])
            if px is None:
                stat["no_price"] += 1
                continue          # 가격을 모르면 판정하지 않는다
            stat["rest"] += 1

        lines = update_lines(pos, float(px))
        if lines is not None:
            if ledger.set_lines(pos["id"], lines[0], lines[1], now=now):
                stat["lines"] += 1
                pos = {**pos, "stop_live": lines[0], "target_live": lines[1]}

        stop, target = ledger.effective_lines(pos)
        if pos["side"] == "long":
            reason = "stop" if px <= stop else ("target" if px >= target else None)
        else:
            reason = "stop" if px >= stop else ("target" if px <= target else None)
        if not reason:
            continue

        # 발주 **전에** 잠근다. 2초 루프에서는 발주 왕복 안에 다음 사이클이 온다.
        ledger.set_exit_pending(pos["id"], 1)
        try:
            r = await execute_exit(pos, reason, float(stop if reason == "stop" else target))
        except Exception:  # noqa: BLE001
            log.exception("데스크 청산 발주 오류 %s", pos.get("symbol"))
            ledger.set_exit_pending(pos["id"], 0)
            continue
        if r and r.get("ok"):
            stat["exits"] += 1
            log.info("데스크 청산 %s %s(%s) @ %s", reason, pos.get("name"),
                     pos.get("symbol"), px)
        else:
            ledger.set_exit_pending(pos["id"], 0)   # 실패면 다음 사이클에 다시 본다
    return stat


# --------------------------------------------------------------------------
# 루프
# --------------------------------------------------------------------------
async def loop(fresh_price, rest_price, execute_exit, ws_ok) -> None:
    """장중에만 돈다. `enabled: false` 면 아무것도 하지 않는다.

    ws_ok() -> bool : WS 연결 상태. 끊겼으면 주기를 강등해 REST 폭주를 막는다.
    """
    while True:
        interval = _num("interval_sec")
        try:
            if enabled() and in_session():
                if not ws_ok():
                    # 전 종목이 낡아 매 사이클 REST 를 타게 된다 — 주기를 늦춘다
                    interval = _num("degraded_interval_sec")
                    log.warning("WS 미연결 — 데스크 주기를 %.0f초로 강등", interval)
                await tick(fresh_price, rest_price, execute_exit)
        except Exception:  # noqa: BLE001 - 데스크 오류가 서비스를 멈추지 않는다
            log.exception("매매 데스크 오류")
        await asyncio.sleep(max(0.5, interval))
