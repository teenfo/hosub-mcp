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
import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import settings
from . import ledger

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
SESSION = ("09:00", "15:30")

# 런타임 오버라이드. **배포 없이 끌 수 있어야 한다** — 초 단위로 실거래 청산을
# 내는 루프이므로 이상이 보이면 그 자리에서 멈출 수 있어야 한다.
# `scout/engine.py` 의 engine.json 과 같은 관례다(오버라이드 > config > 기본값).
STATE_FILE = Path(settings.DATA_DIR) / "desk.json"

DEFAULTS = {
    "enabled": False,        # 실거래 청산을 다루므로 배포와 활성화를 분리한다
    "interval_sec": 2.0,     # 현행 30초의 15배. 1초는 발주 왕복·SQLite 쓰기와 경합
    "stale_sec": 5.0,        # 이보다 오래된 WS 값은 못 믿는다 → REST 보충
    "max_symbols": 0,        # 0 = 보유 전량. 상한을 두면 그 초과분이 2초 감시 밖으로 나간다
    "degraded_interval_sec": 30.0,   # WS 미연결 시 강등 주기
    "trailing": True,        # 손절·익절선 추종. 끄면 진입 시 고정값 그대로 쓴다
}

# 화면이 읽는 계측. "WS 로 몇 건, REST 로 몇 건" 이 이 설계의 성적표다 —
# REST 가 늘고 있으면 분리한 의미가 사라지는 중이므로 눈에 보여야 한다.
STATE: dict = {
    "cycles": 0, "ws": 0, "rest": 0, "no_price": 0, "exits": 0, "lines": 0,
    "proposed": 0, "watched": 0, "degraded": False,
    "last_tick": None, "last_line": None,
}


def status() -> dict:
    c = cfg()
    return dict(STATE, enabled=bool(c.get("enabled", False)),
                trailing=trailing(),
                interval_sec=_num("interval_sec"), stale_sec=_num("stale_sec"),
                degraded_interval_sec=_num("degraded_interval_sec"),
                max_symbols=int(_num("max_symbols")),
                # 화면이 "설정값과 다르게 돌고 있다" 를 말할 수 있어야 한다
                override=_override())


def _override() -> dict:
    if STATE_FILE.exists():
        try:
            v = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(v, dict):
                return v
        except (OSError, ValueError):
            log.warning("desk.json 을 읽을 수 없습니다 — config 기본값 사용")
    return {}


def cfg() -> dict:
    """런타임 오버라이드 > config.yaml > 기본값."""
    c = (settings.CONFIG.get("execution", {}) or {}).get("desk", {})
    return DEFAULTS | (c if isinstance(c, dict) else {}) | _override()


def enabled() -> bool:
    return bool(cfg().get("enabled", False))


def set_state(**patch) -> dict:
    """데스크 설정을 런타임에 바꾼다. 다음 사이클(최대 `interval_sec`)부터 적용된다.

    끄면 루프는 살아 있되 아무것도 하지 않고, 청산 감시는 기존 30초 `_ledger_loop`
    가 그대로 맡는다 — **중간에 감시가 비는 구간이 없다.**
    """
    st = _override() | {k: v for k, v in patch.items() if v is not None}
    for k in ("enabled", "trailing"):
        if k in st:
            st[k] = bool(st[k])
    for k in ("interval_sec", "stale_sec", "degraded_interval_sec"):
        if k in st:
            st[k] = max(0.5, float(st[k]))
    if "max_symbols" in st:
        st["max_symbols"] = max(1, int(st["max_symbols"]))
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    log.warning("매매 데스크 설정 변경: %s", st)
    return st


def _num(key: str) -> float:
    try:
        return float(cfg().get(key, DEFAULTS[key]))
    except (TypeError, ValueError):
        return float(DEFAULTS[key])


def stale_sec() -> float:
    """WS 값을 믿을 수 있는 최대 나이(초). 이보다 오래되면 REST 로 보충한다."""
    return _num("stale_sec")


FAST = ("stop", "target")     # 데스크가 다루는 사유. 시각 기준(timeout)은 제외


def owns(reason: str) -> bool:
    """이 청산 사유를 데스크가 소유하는가 — 30초 루프는 소유하지 않은 것만 낸다.

    소유권을 하나로 두는 이유는 이중 발주(그건 `exit_pending` 이 막는다)가 아니라
    **어느 루프가 냈는지가 흐려지는 것**이다. 데스크를 켜고 껐을 때의 동작이
    뒤섞이면 관찰 결과를 읽을 수 없다.

    시간 손절(`timeout`)은 넘기지 않는다 — 시각 기준이라 2초 해상도가 필요 없고,
    보유시간 규칙은 데스크가 들고 있지 않다.

    **상한이 걸려 보유를 다 못 보면 소유권을 가져가지 않는다.** `max_symbols` 를
    두면 초과분은 데스크가 건너뛰는데, 그때도 소유권을 주장하면 그 종목은 데스크도
    30초 루프도 보지 않는 상태가 된다 — 손절이 아무도 없는 채로 남는다.
    상한이 걸린 동안에는 판정이 겹치지만, 겹치는 것보다 비는 것이 훨씬 위험하다.
    """
    if not (enabled() and reason in FAST):
        return False
    limit = int(_num("max_symbols"))
    if limit <= 0:
        return True                       # 보유 전량을 본다 — 온전한 소유권
    return len(ledger.positions(status="open", limit=200)) <= limit


def exit_route(reason: str) -> str:
    """30초 루프가 이 청산 사유를 어떻게 처리해야 하는가. 정책을 한 곳에 둔다.

    - `skip`    : 데스크가 소유한다. 손대지 않는다
    - `approve` : **자동 발주하지 않고 승인 대기로 올린다** (데스크 꺼짐 + 손절·목표)
    - `auto`    : 기존 규약(`stop_mode`/`auto_approve`)을 따른다

    `approve` 는 사용자 결정이다(2026-07-29). 30초 판정으로 시장가를 던지면 판정
    시점 가격과 벌어지는 것을 감수하는 셈인데, 그 트레이드오프를 사람이 보고
    정하겠다는 뜻이다. 데스크를 켜는 것이 곧 '그 판단을 자동에 맡긴다' 가 된다.

    상한 초과로 데스크가 못 보는 종목은 `auto` 로 남는다 — 그건 되돌리기가 아니라
    **감시 구멍**이고, 구멍에서는 보수적인 쪽이 자동 청산이다.
    """
    if owns(reason):
        return "skip"
    if not enabled() and reason in FAST:
        return "approve"
    return "auto"


def in_session(now: datetime | None = None) -> bool:
    t = now or datetime.now(KST)
    return t.weekday() < 5 and SESSION[0] <= t.strftime("%H:%M") <= SESSION[1]


# --------------------------------------------------------------------------
# 라인 갱신 훅 — 규칙은 아직 없다
# --------------------------------------------------------------------------
def trailing() -> bool:
    """라인 추종을 쓰는가. 데스크가 꺼져 있으면 라인도 움직이지 않는다."""
    return enabled() and bool(cfg().get("trailing", True))


def update_lines(pos: dict, price: float) -> tuple[float | None, float | None] | None:
    """손절·익절선을 현재가에 맞춰 끌어올린다. None 이면 '변화 없음'.

    사용자 지침(2026-07-29):
    > 손절: 하향은 없다. 현재 가격이 상승할 때마다 **동일한 갭**으로 상향한다.
    >       가격이 하락해도 손절라인을 유지해 이득을 보전한다.
    > 익절: 하향은 없다. 같은 방식으로 상향해 이득을 극대화한다.

    **갭은 진입 시 정한 폭**이다 — `entry - stop` / `target - entry`. 원본
    `pos["stop"]`/`pos["target"]` 은 되돌릴 기준이라 읽기만 하고, 갱신값은
    `stop_live`/`target_live` 에 따로 쌓인다.

        롱:  새 손절 = max(지금 손절, 현재가 − 손절갭)
             새 목표 = max(지금 목표, 현재가 + 목표갭)

    `max` 하나가 규칙 전체를 만든다 — 오르면 따라 올라가고 내리면 그 자리에
    남는다. 조건문으로 "상승했는가"를 따로 보지 않는 이유는, 틱이 튀거나 REST
    폴백으로 값이 한 번 건너뛰어도 결과가 같아야 하기 때문이다.

    **숏은 방향을 뒤집는다.** 숏에서 유리한 움직임은 하락이므로 손절선은
    내려가고 목표가도 내려간다(`min`). 지침 문구를 롱 기준 그대로 적용하면
    숏의 손절선이 **멀어져 손실이 커진다** — 추종은 언제나 유리한 방향 한쪽이다.

    ## 알아둘 것 — 목표가는 사실상 닿지 않게 된다

    목표가가 늘 `현재가 + 갭` 위로 달아나므로 목표 도달 청산은 발생하지 않는다.
    청산은 **따라 올라간 손절선**(또는 시간 손절·마감 정리)이 맡는다. 그것이
    "이득을 극대화" 지침의 귀결이다 — 이익을 미리 확정하지 않고 추세가 꺾일 때
    반납분만 내주고 나온다.
    """
    if not trailing():
        return None
    try:
        entry = float(pos["entry"])
        stop0, target0 = float(pos["stop"]), float(pos["target"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    if entry <= 0 or price <= 0:
        return None

    cur_stop, cur_target = ledger.effective_lines(pos)
    if pos["side"] == "long":
        s_gap, t_gap = entry - stop0, target0 - entry
        new_stop = max(cur_stop, price - s_gap) if s_gap > 0 else cur_stop
        new_target = max(cur_target, price + t_gap) if t_gap > 0 else cur_target
    else:
        s_gap, t_gap = stop0 - entry, entry - target0
        new_stop = min(cur_stop, price + s_gap) if s_gap > 0 else cur_stop
        new_target = min(cur_target, price - t_gap) if t_gap > 0 else cur_target

    new_stop, new_target = round(new_stop, 2), round(new_target, 2)
    if (new_stop, new_target) == (round(cur_stop, 2), round(cur_target, 2)):
        return None                      # 초 단위 루프다 — 안 바뀌면 쓰지 않는다
    return new_stop, new_target


# --------------------------------------------------------------------------
# 한 사이클
# --------------------------------------------------------------------------
def _auto(reason: str) -> bool:
    """이 청산을 승인 없이 즉시 발주해도 되는가.

    **기존 30초 루프와 같은 규약을 따라야 한다.** 데스크를 켜는 것은 판정 주기를
    바꾸는 것이지 승인 규칙을 바꾸는 것이 아니다. 여기서 갈리면 데스크를 켜는
    순간 목표 도달 청산이 조용히 자동 발주로 바뀐다.
    """
    ex = settings.CONFIG.get("execution", {}) or {}
    if settings.RISK.get("auto_approve", False):
        return True
    # 손절·시간손절은 계좌 보호라 stop_mode 를 따르고, 목표는 항상 승인이다
    return reason in ("stop", "timeout") and ex.get("stop_mode", "auto") == "auto"


async def tick(fresh_price, rest_price, execute_exit, now: datetime | None = None,
               propose_exit=None) -> dict:
    """보유 포지션 1회 판정. 반환: 계측값(화면·로그용).

    fresh_price(symbol) -> float | None   : WS 값. 낡았으면 None (콜 0)
    rest_price(symbol)  -> float | None   : REST 보충 (async)
    execute_exit(pos, reason, px)         : 청산 **발주** (async). 데스크가 직접 낸다 —
        판정만 하고 다른 루프에 넘기면 빨라진 의미가 없다
    propose_exit(pos, reason, px)         : 승인 대기 등록(동기). 승인 모드일 때만 쓴다

    전부 주입받는 이유는 **WS 가 있을 때 REST 를 부르지 않는다**를 테스트로 고정하기
    위해서다. 이 설계의 값이 거기서 나온다 — 콜이 늘면 분리한 의미가 없다.
    """
    now = now or datetime.now(KST)
    stat = {"watched": 0, "ws": 0, "rest": 0, "no_price": 0, "exits": 0,
            "lines": 0, "proposed": 0}
    rows = [p for p in ledger.positions(status="open", limit=200)
            if not p.get("exit_pending")]
    limit = int(_num("max_symbols"))
    if limit > 0:
        rows = rows[:limit]      # 0 이면 보유 전량 — 감시 밖으로 나가는 종목이 없다
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
                STATE["last_line"] = {
                    "at": now.strftime("%H:%M:%S"), "symbol": pos["symbol"],
                    "name": pos.get("name"), "stop": lines[0], "target": lines[1]}

        stop, target = ledger.effective_lines(pos)
        if pos["side"] == "long":
            reason = "stop" if px <= stop else ("target" if px >= target else None)
        else:
            reason = "stop" if px >= stop else ("target" if px <= target else None)
        if not reason:
            continue

        line_px = float(stop if reason == "stop" else target)
        # 발주 **전에** 잠근다. 2초 루프에서는 발주 왕복 안에 다음 사이클이 온다.
        ledger.set_exit_pending(pos["id"], 1)

        if not _auto(reason) and propose_exit is not None:
            # 승인 모드 — 기존 30초 루프와 같은 규약이다. 승인 대기에만 올린다
            try:
                await asyncio.to_thread(propose_exit, pos, reason, line_px)
            except Exception:  # noqa: BLE001
                log.exception("데스크 청산 승인 등록 오류 %s", pos.get("symbol"))
                ledger.set_exit_pending(pos["id"], 0)
                continue
            stat["proposed"] += 1
            log.info("데스크 청산 승인 대기 %s(%s) %s", pos.get("name"),
                     pos.get("symbol"), reason)
            continue

        try:
            r = await execute_exit(pos, reason, line_px)
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

    STATE["cycles"] += 1
    for k in ("ws", "rest", "no_price", "exits", "lines", "proposed"):
        STATE[k] += stat[k]
    STATE["watched"] = stat["watched"]
    STATE["last_tick"] = now.strftime("%H:%M:%S")
    return stat


# --------------------------------------------------------------------------
# 루프
# --------------------------------------------------------------------------
async def loop(fresh_price, rest_price, execute_exit, ws_ok, propose_exit=None) -> None:
    """장중에만 돈다. `enabled: false` 면 아무것도 하지 않는다.

    ws_ok() -> bool : WS 연결 상태. 끊겼으면 주기를 강등해 REST 폭주를 막는다.
    """
    while True:
        interval = _num("interval_sec")
        try:
            if enabled() and in_session():
                degraded = not ws_ok()
                if degraded:
                    # 전 종목이 낡아 매 사이클 REST 를 타게 된다 — 주기를 늦춘다
                    interval = _num("degraded_interval_sec")
                    log.warning("WS 미연결 — 데스크 주기를 %.0f초로 강등", interval)
                STATE["degraded"] = degraded
                await tick(fresh_price, rest_price, execute_exit,
                           propose_exit=propose_exit)
        except Exception:  # noqa: BLE001 - 데스크 오류가 서비스를 멈추지 않는다
            log.exception("매매 데스크 오류")
        await asyncio.sleep(max(0.5, interval))
