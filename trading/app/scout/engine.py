"""중앙 관제 루프 — 여섯 소스를 하나의 통로로 모은다.

## 모드

`shadow` 로 시작한다. 신호를 모으고 점수를 내고 **결정을 기록만 한다** —
감시목록에는 손대지 않는다. 화면에 "엔진이라면 이렇게 했을 것" 을 실제
감시목록과 나란히 놓고 3거래일 관찰한 뒤에야 다음으로 간다.

    shadow   기록만. 기존 replace_* 경로가 그대로 돈다
    collect  수집전용 tier 만 엔진이 관리
    full     매매 tier 까지. 기존 replace_* 는 비활성

모드는 `data/engine.json` 런타임 오버라이드로 바꾼다 — 실거래 중에 배포 없이
되돌릴 수 있어야 하기 때문이다(`settings.RULES_FILE` 과 같은 패턴).

## 재시작 직후 투영 폭주 방어

신호 저장소가 비어 있으면 "감시목록 전체 삭제" diff 가 나온다. **각 소스가
최소 1회 성공 폴을 마치기 전에는 적용하지 않는다.** 축소 폭 상한도 함께 둔다 —
한 사이클에 몇 종목까지 뺄 수 있는지.

## 왜 스레드가 아니라 순차 await 인가

무거운 작업(전종목 발굴)은 이미 별도 프로세스로 나가 있다. 어댑터는 전부
HTTP 대기라 이벤트 루프를 막지 않는다. `asyncio.to_thread` 는 CPU 작업을
격리하지 못한다 — GIL 때문에 이벤트 루프가 굶는다(2026-07-27 실장애: 연결
141개 적체).
"""
import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import settings
from . import promote, scoring, store
from .model import Signal
from .promote import COLLECT, NONE, TRADE, Current
from .sources import ALL

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
STATE_FILE = Path(settings.DATA_DIR) / "engine.json"

MODES = ("shadow", "collect", "full")
MAX_SHRINK = 10        # 한 사이클에 뺄 수 있는 최대 종목 수 — 서킷브레이커
BACKOFF_BASE = 2       # 연속 실패 시 주기 배수 상한 2^n


def _state() -> dict:
    if STATE_FILE.exists():
        try:
            v = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(v, dict):
                return v
        except (OSError, ValueError):
            log.warning("engine.json 을 읽을 수 없습니다 — config 기본값 사용")
    return {}


def mode() -> str:
    """현재 모드. 런타임 오버라이드 > config > shadow."""
    st = _state()
    m = st.get("mode") or settings.CONFIG.get("scout", {}).get("mode", "shadow")
    return m if m in MODES else "shadow"


def frozen() -> bool:
    """킬 스위치 — 쓰기만 멈추고 감시목록은 현 상태로 동결한다."""
    return bool(_state().get("frozen"))


def set_state(**patch) -> dict:
    st = _state() | {k: v for k, v in patch.items() if v is not None}
    if "mode" in st and st["mode"] not in MODES:
        raise ValueError(f"알 수 없는 모드: {st['mode']}")
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    log.warning("발굴 엔진 상태 변경: %s", st)
    return st


def snapshot_current() -> Current:
    """지금 감시목록 상태 → 투영기 입력.

    `protected` 에 보유 종목과 seed/manual 을 넣는다. 보유분이 감시목록에서
    빠지면 청산 감시가 최대 15분 묵은 가격으로 돈다(watchlist._held 참조).
    """
    from ..data import watchlist

    tier: dict[str, str] = {}
    since: dict[str, datetime] = {}
    names: dict[str, str] = {}
    protected: set[str] = set()
    for e in watchlist.entries():
        code = e["code"]
        names[code] = e.get("name") or code
        tier[code] = COLLECT if e.get("collect_only") else TRADE
        since[code] = _parse_ts(e.get("added"))
        if e.get("source") in ("seed", "manual"):
            protected.add(code)
    try:
        from ..trade import ledger

        protected |= ledger.open_symbols()
    except Exception:  # noqa: BLE001 - 조회 실패 시 아무것도 빼지 않는 쪽이 안전
        log.exception("보유 종목 조회 실패 — 이번 사이클은 강등하지 않는다")
        protected |= set(tier)
    return Current(tier=tier, since=since, protected=frozenset(protected),
                   names=names)


def _parse_ts(raw) -> datetime:
    try:
        t = datetime.fromisoformat(str(raw))
        return t if t.tzinfo else t.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)      # 알 수 없으면 '방금' — 체류시간 보호가 걸린다


async def apply(decisions: list[dict]) -> int:
    """결정을 감시목록에 반영한다. **모드가 허용하는 것만.**"""
    from ..data import watchlist

    m = mode()
    if m == "shadow" or frozen():
        return 0
    applied = 0
    for d in decisions:
        to = d["to_tier"]
        if m == "collect" and (to == TRADE or d["from_tier"] == TRADE):
            continue                      # 매매 tier 는 아직 기존 경로 소유
        try:
            if to == NONE:
                watchlist.remove(d["code"])
            elif d["from_tier"] == NONE:
                watchlist.add(d["code"], d["name"], source="scout")
                watchlist.set_mode(d["code"], collect_only=(to == COLLECT))
            else:
                watchlist.set_mode(d["code"], collect_only=(to == COLLECT))
            applied += 1
        except Exception:  # noqa: BLE001 - 한 종목 실패가 나머지를 막지 않는다
            log.exception("감시목록 반영 실패: %s", d["code"])
    if applied:
        await watchlist.notify()
    return applied


class Engine:
    def __init__(self) -> None:
        self.sources = [cls() for cls in ALL]
        self.next_due: dict[str, float] = {}
        self.polled: set[str] = set()          # 최소 1회 성공한 소스
        self.last_cycle: str = ""
        self.last_error: str = ""
        self.candidates: list[scoring.Candidate] = []
        self.last_decisions: list[dict] = []

    # --- 수집 ---
    def due(self, name: str, now: float) -> bool:
        return now >= self.next_due.get(name, 0.0)

    async def collect_due(self, now: float | None = None) -> int:
        """due 인 소스만 수집. 어댑터별 예외 격리 + 연속 실패 지수 백오프."""
        now = now if now is not None else asyncio.get_event_loop().time()
        got = 0
        for src in self.sources:
            if not src.enabled() or not self.due(src.name, now):
                continue
            try:
                signals: list[Signal] = await src.collect()
            except Exception as e:  # noqa: BLE001 - 한 소스가 나머지를 막지 않는다
                fails = store.mark_fail(src.name, e)
                wait = src.interval_sec() * min(2 ** fails, BACKOFF_BASE ** 5)
                self.next_due[src.name] = now + wait
                log.warning("%s 수집 실패(%d회) — %d초 후 재시도: %s",
                            src.name, fails, wait, e)
                continue
            store.record(signals)
            store.mark_ok(src.name, len(signals))
            self.polled.add(src.name)
            self.next_due[src.name] = now + src.interval_sec()
            got += len(signals)
        return got

    # --- 투영 ---
    def ready(self) -> bool:
        """켜져 있는 소스가 전부 최소 1회 성공했는가.

        저장소가 비어 있는 채로 투영하면 '감시목록 전체 삭제' diff 가 나온다.
        재시작 직후가 정확히 그 상태다.
        """
        want = {s.name for s in self.sources if s.enabled()}
        return bool(want) and want <= self.polled

    async def refresh_quotes(self, cur, now: datetime | None = None) -> int:
        """**매매 승격 후보에만** 현재 시세를 실측한다.

        수집 tier 에 있는 후보만 매매로 올라갈 수 있으므로(promote.plan) 그
        집합에만 부른다 — 실측 기준 하루 1~2종목이라 레이트리밋 부담이 없다.
        전 후보(오늘 27종목)에 부르면 30초 사이클마다 27콜이라 얘기가 다르다.

        실패는 삼킨다. 시세를 못 받으면 `price_fresh` 가 False 로 남고
        `_tradable` 이 이번 사이클 승격을 보류한다 — 그게 올바른 결말이다.
        반환: 실측에 성공한 종목 수.
        """
        if not settings.KIWOOM_APP_KEY or not promote.in_session(now):
            return 0
        from ..kiwoom.client import client
        from ..kiwoom.quote import parse_quote

        got = 0
        for c in self.candidates:
            if cur.tier.get(c.code) != promote.COLLECT:
                continue
            try:
                c.quote = parse_quote(await client.quote(c.code))
            except Exception as e:  # noqa: BLE001 - 못 받으면 승격을 미룬다
                log.warning("현재가 조회 실패 %s — 이번 사이클 매매 승격 보류: %s",
                            c.code, e)
                continue
            got += 1 if c.quote else 0
        return got

    def project(self, now: datetime | None = None, cur=None) -> list[dict]:
        now = now or datetime.now(UTC)
        if cur is None:
            self.candidates = scoring.aggregate(store.live(now), now)
            cur = snapshot_current()
        rows = promote.plan(self.candidates, cur, now)
        shrink = [r for r in rows if r["to_tier"] == NONE]
        if len(shrink) > MAX_SHRINK:
            # 서킷브레이커 — 한 번에 대량 축소는 대개 소스 장애다
            keep = {id(r) for r in shrink[MAX_SHRINK:]}
            rows = [r for r in rows if id(r) not in keep]
            log.warning("축소 폭 상한 — %d건 중 %d건만 적용",
                        len(shrink), MAX_SHRINK)
        return rows

    async def run_once(self) -> dict:
        got = await self.collect_due()
        if not self.ready():
            self.last_cycle = datetime.now(KST).isoformat(timespec="seconds")
            return {"signals": got, "decisions": 0, "applied": 0,
                    "reason": "소스 첫 수집 대기 중"}
        now = datetime.now(UTC)
        self.candidates = scoring.aggregate(store.live(now), now)
        cur = snapshot_current()
        # 승격 후보의 가격을 실측한 **뒤에** 판정한다 — 순서가 뒤집히면
        # 게이트가 또 낡은 값을 본다
        await self.refresh_quotes(cur, now)
        rows = self.project(now, cur)
        self.last_decisions = rows
        applied = await apply(rows)
        store.log_decisions(rows, mode=mode(), applied=bool(applied))
        self.last_cycle = datetime.now(KST).isoformat(timespec="seconds")
        if rows:
            log.info("발굴 엔진(%s): 신호 %d · 결정 %d · 반영 %d",
                     mode(), got, len(rows), applied)
        return {"signals": got, "decisions": len(rows), "applied": applied}

    async def loop(self, interval_sec: int = 30) -> None:
        while True:
            try:
                if settings.CONFIG.get("scout", {}).get("enabled", True):
                    await self.run_once()
                    self.last_error = ""
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
                log.exception("발굴 엔진 오류")
            await asyncio.sleep(interval_sec)

    # --- 상태 ---
    def status(self) -> dict:
        return {
            "mode": mode(), "frozen": frozen(), "ready": self.ready(),
            "last_cycle": self.last_cycle, "last_error": self.last_error,
            "max_score": scoring.max_possible(),
            "thresholds": promote.cfg(),
            "sources": [
                {"name": s.name, "enabled": s.enabled(),
                 "interval_sec": s.interval_sec(),
                 "polled": s.name in self.polled} | store.health().get(s.name, {})
                for s in self.sources
            ],
        }


engine = Engine()
