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
import time
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
    "tighten_at": 0.5,       # 목표까지 이만큼 왔으면 손절 계산을 바꾼다(목표갭 대비)
    "lock_gain_pct": 30.0,   # 그 뒤로는 **상승분의 이만큼**을 손절선으로 확정한다(%)
    "trail_target": False,   # 익절선도 따라 올릴 것인가. **기본은 고정**(아래 실측)
    "max_gain_pct": 3.0,     # 익절선 상한(진입가 대비 %). 0이면 상한 없음
}

# 화면이 읽는 계측. "WS 로 몇 건, REST 로 몇 건" 이 이 설계의 성적표다 —
# REST 가 늘고 있으면 분리한 의미가 사라지는 중이므로 눈에 보여야 한다.
STATE: dict = {
    "cycles": 0, "ws": 0, "rest": 0, "rest_cache": 0, "no_price": 0, "exits": 0,
    "lines": 0, "proposed": 0, "watched": 0, "degraded": False,
    "last_tick": None, "last_line": None,
}

# REST 폴백 캐시 — symbol -> (monotonic, price). tick() 의 스로틀 주석 참조.
_REST_CACHE: dict[str, tuple[float, float]] = {}


def status() -> dict:
    c = cfg()
    return dict(STATE, enabled=bool(c.get("enabled", False)),
                trailing=trailing(), trail_target=bool(cfg().get("trail_target", False)),
                tighten_at=_num("tighten_at"),
                lock_gain_pct=_num("lock_gain_pct"), max_gain_pct=_num("max_gain_pct"),
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
    for k in ("enabled", "trailing", "trail_target"):
        if k in st:
            st[k] = bool(st[k])
    for k in ("interval_sec", "stale_sec", "degraded_interval_sec"):
        if k in st:
            st[k] = max(0.5, float(st[k]))
    if "max_symbols" in st:
        st["max_symbols"] = max(1, int(st["max_symbols"]))
    # 추종 파라미터. 범위를 강제한다 — 실거래 청산선을 정하는 값이라
    # 오타 하나가 손절선을 엉뚱한 곳에 놓는다.
    if "tighten_at" in st:
        # 목표갭 대비 비율(0~1). 1 이면 목표에 닿아야 발동 = 사실상 미사용
        st["tighten_at"] = min(1.0, max(0.0, float(st["tighten_at"])))
    if "lock_gain_pct" in st:
        # 상승분의 몇 %를 확정할지(0~100). 100 이면 손절선이 현재가에 붙는다
        st["lock_gain_pct"] = min(100.0, max(0.0, float(st["lock_gain_pct"])))
    if "max_gain_pct" in st:
        st["max_gain_pct"] = max(0.0, float(st["max_gain_pct"]))
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

    ## 목표까지 절반을 오면 상승분의 x% 를 확정한다 (지침 보강 2026-07-29)

    > 가격이 진입가·익절가 갭의 50%를 넘으면, 손절라인을 **상승한 금액의 x%** 로
    > 조정한다. x 는 설정값이다.

        손절 = 진입가 + (현재가 − 진입가) × x%

    지침의 예: 진입 1,000 · 목표 1,400 · 현재가 1,200(상승 200) · x=90
        → 1,000 + 200 × 0.9 = **1,180**

    앞의 '같은 갭' 추종과 다른 점은 **손절선이 가격을 따라 붙는 속도**다. 갭 추종은
    거리를 고정하지만 이쪽은 상승분에 비례해 거리가 `(1 − x%)` 로 줄어든다 —
    많이 오를수록 더 많이 확정한다. 발동 전에는 갭 추종을 그대로 쓴다.

    두 계산 중 **높은 쪽**을 쓴다(`max`). 그래야 x 를 낮게 잡아도 갭 추종보다
    나빠지지 않는다.

    ## 익절선은 기본적으로 **고정**이다 (`trail_target: false`)

    익절선까지 따라 올리면 목표가가 늘 달아나 도달이 안 된다. 상한(3%)으로
    막아도 실측에서 손해였다 — 실거래 57건 투사에서 **고정이면 목표에 닿아
    익절했을 7건이 되밀려 손절선에서 나왔다(−6,593원).** 손절선이 목표보다
    먼저 따라붙기 때문이다.

    그래서 기본은 손절선만 추종하고 익절선은 진입 시 값을 지킨다. 예전 동작이
    필요하면 `trail_target: true` 로 되돌린다 — 설정으로 남겨 둔 이유다.

    ## 익절 상한 (`max_gain_pct`)

    익절선의 천장이다. 규칙에 따라 목표가 3%를 넘게 잡히는 경우가 있는데
    (`target_r` 1.5 × 손절폭 2.5% = 3.75%) 그때는 목표가가 내려온다.
    0으로 두면 상한이 없다.
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

    at, lock = _num("tighten_at"), _num("lock_gain_pct") / 100.0
    cap_pct = _num("max_gain_pct") / 100.0
    tgt = bool(cfg().get("trail_target", False))
    cur_stop, cur_target = ledger.effective_lines(pos)
    if pos["side"] == "long":
        s_gap, t_gap = entry - stop0, target0 - entry
        cand = price - s_gap if s_gap > 0 else cur_stop
        if t_gap > 0 and lock > 0 and price >= entry + t_gap * at:
            cand = max(cand, entry + (price - entry) * lock)   # 상승분의 x% 확정
        new_stop = max(cur_stop, cand)
        new_target = (max(cur_target, price + t_gap)
                      if (tgt and t_gap > 0) else cur_target)
        if cap_pct > 0:
            new_target = min(new_target, entry * (1 + cap_pct))
    else:
        s_gap, t_gap = stop0 - entry, entry - target0
        cand = price + s_gap if s_gap > 0 else cur_stop
        if t_gap > 0 and lock > 0 and price <= entry - t_gap * at:
            cand = min(cand, entry - (entry - price) * lock)
        new_stop = min(cur_stop, cand)
        new_target = (min(cur_target, price - t_gap)
                      if (tgt and t_gap > 0) else cur_target)
        if cap_pct > 0:
            new_target = max(new_target, entry * (1 - cap_pct))

    new_stop, new_target = round(new_stop, 2), round(new_target, 2)
    if (new_stop, new_target) == (round(cur_stop, 2), round(cur_target, 2)):
        return None                      # 초 단위 루프다 — 안 바뀌면 쓰지 않는다
    # 원본과 같은 값은 `live` 로 쓰지 않는다(None = 원본 사용). 같은 값을 써두면
    # 화면이 '추종 중' 으로 보이고, 시간 손절 시계도 움직이지 않은 손절선 때문에
    # 초기화된다.
    return (None if new_stop == stop0 else new_stop,
            None if new_target == target0 else new_target)


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
    stat = {"watched": 0, "ws": 0, "rest": 0, "rest_cache": 0, "no_price": 0,
            "exits": 0, "lines": 0, "proposed": 0}
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
            # REST 폴백은 종목당 stale_sec 에 한 번만 — 종전에는 stale 이 지속되면
            # **매 사이클(2초)** 콜이 나갔다(실측 2026-07-31: 보유 2종목 분당 47콜).
            # 사이 사이클은 마지막 REST 값으로 판정한다 — stale_sec 이내 값은
            # 어차피 '믿는 나이' 안이다.
            mono = time.monotonic()
            hit = _REST_CACHE.get(pos["symbol"])
            if hit and mono - hit[0] < _num("stale_sec"):
                px = hit[1]
                stat["rest_cache"] += 1
            else:
                px = await rest_price(pos["symbol"])
                if px is None:
                    stat["no_price"] += 1
                    continue      # 가격을 모르면 판정하지 않는다
                _REST_CACHE[pos["symbol"]] = (mono, float(px))
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
    for k in ("ws", "rest", "rest_cache", "no_price", "exits", "lines", "proposed"):
        STATE[k] += stat[k]
    STATE["watched"] = stat["watched"]
    STATE["last_tick"] = now.strftime("%H:%M:%S")
    _summarize(now)
    return stat


# 주기 요약 — 2초 루프라 매 사이클 찍으면 로그가 무의미해진다. 1분에 한 줄.
_LAST_SUMMARY: dict = {"at": None, "ws": 0, "rest": 0, "rest_cache": 0}
SUMMARY_SEC = 60.0


def _summarize(now: datetime) -> None:
    """"WS 로 몇 건, REST 로 몇 건" 을 로그에도 남긴다.

    화면에는 누적값이 보이지만, **무엇이 언제 나빠졌는지**는 시계열이라야 읽힌다.
    발굴 엔진 전환과 데스크 활성화를 같은 날 하면 원인이 겹치는데(사용자 결정
    2026-07-29), 그때 둘을 가르는 것은 이 줄과 WS 재구독 로그의 시각이다.
    """
    prev = _LAST_SUMMARY["at"]
    if prev is not None and (now - prev).total_seconds() < SUMMARY_SEC:
        return
    _LAST_SUMMARY["at"] = now
    d_ws = STATE["ws"] - _LAST_SUMMARY["ws"]
    d_rest = STATE["rest"] - _LAST_SUMMARY["rest"]
    # 캐시 히트도 REST 계열로 센다 — 이 경보의 목적은 호출량이 아니라 **WS 가
    # 죽었는지**다. 스로틀(2026-08-01)이 콜을 줄여도 'WS 아님' 비율은 그대로
    # 보여야 13시대 좀비 같은 상황이 로그에 남는다.
    d_cache = STATE["rest_cache"] - _LAST_SUMMARY.get("rest_cache", 0)
    # 청산도 델타로 — '최근 N초' 라고 말하는 줄에 프로세스 누적값을 섞으면
    # 재시작 전까지 계속 같은 숫자가 찍혀 문구가 거짓이 된다(감사 2026-08-01).
    d_exit = STATE["exits"] - _LAST_SUMMARY.get("exits", 0)
    _LAST_SUMMARY.update(ws=STATE["ws"], rest=STATE["rest"],
                         rest_cache=STATE["rest_cache"], exits=STATE["exits"])
    if prev is None or (d_ws + d_rest + d_cache) == 0:
        return
    pct = (d_rest + d_cache) / (d_ws + d_rest + d_cache) * 100
    fn = log.warning if pct > 20 else log.info
    fn("데스크 요약(최근 %.0f초): 감시 %d종목 · WS %d · REST %d(캐시 %d) "
       "(%.0f%%) · 청산 %d (누적 %d)",
       SUMMARY_SEC, STATE["watched"], d_ws, d_rest, d_cache, pct, d_exit,
       STATE["exits"])


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
