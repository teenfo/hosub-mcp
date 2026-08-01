"""중앙 관제 루프 — 여섯 소스를 하나의 통로로 모은다.

## 모드

shadow → collect → full 로 3거래일씩 관찰하며 올라왔고, **2026-08-01 완전
통합으로 기존 직접 편입 경로(replace_* 계열)는 삭제됐다.** 이제 감시목록
쓰기는 이 엔진과 사용자 수동 조작뿐이다.

    shadow   기록만. **감시목록은 동결된다** — 다른 쓰기 경로가 없다
    collect  수집전용 tier 만 엔진이 관리
    full     매매 tier 까지 엔진이 관리 (현행 운영 모드)

모드는 `data/engine.json` 런타임 오버라이드로 바꾼다 — 실거래 중에 배포 없이
되돌릴 수 있어야 하기 때문이다(`settings.RULES_FILE` 과 같은 패턴). 되돌리기
(shadow)는 이제 '기존 경로로 복귀' 가 아니라 '현 상태 동결' 을 뜻한다.

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
# 연속 0건 경고 주기. 매 폴마다 찍으면 로그가 묻히고, 한 번만 찍으면 놓친다.
# 60초 주기 소스라면 20회 = 20분마다 한 줄.
EMPTY_WARN_EVERY = 20


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


def owns_watchlist() -> bool:
    """엔진이 감시목록 편입을 **소유하는가**. `full` 모드에서만 참이다.

    2026-08-01 완전 통합 이후 이 함수의 소비자는 **TNM promote 뿐이었고 그것도
    폐기됐다** — 기존 직접 편입 경로(discovery.replace_auto · scanner.replace_
    active/replace_gainers)는 코드째 삭제됐다. 남겨 두는 이유는 API(status)와
    "지금 누가 소유하는가" 라는 질문 자체가 화면에 필요해서다.

    야간 배치(nightly)·scanner 의 **수집은 계속 돈다** — 전종목 일봉·피처·국면·급등
    후보는 엔진의 입력이지 중복이 아니다. 회수된 것은 '감시목록에 직접 쓰는'
    부분뿐이다(계획 문서의 '폐기 범위는 절반' 정의 그대로).
    """
    return mode() == "full"


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

    `protected`(감시목록에서 빼지 않음)에 보유 종목과 seed/manual 을 넣는다.
    보유분이 감시목록에서 빠지면 청산 감시가 최대 15분 묵은 가격으로 돈다
    (보유가 빠지면 WS 구독 해제 → 손절이 최대 15분 묵은 가격으로 판정된다).

    `held` 는 그중 **매매 tier 에서도 내리지 않는** 것 — 보유만이다. 수동/seed 는
    감시목록에는 남지만 매매 자리를 예약하지 않는다(사용자 결정 2026-07-31:
    "수동 입력은 단순 관심 종목일 수 있으므로 특별 가중치를 두지 않는다.
    다만 관련 정보를 계속 수집한다는 의미로 둔다").
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
    held: set[str] = set()
    try:
        from ..trade import ledger

        held = set(ledger.open_symbols())
        protected |= held
    except Exception:  # noqa: BLE001 - 조회 실패 시 아무것도 빼지 않는 쪽이 안전
        log.exception("보유 종목 조회 실패 — 이번 사이클은 강등하지 않는다")
        protected |= set(tier)
        held = set(tier)
    # 회전 판정 재료 — 원장에서 읽는다(감시목록에는 tier 변경 시각이 없다).
    # 실패해도 회전만 못 할 뿐이므로 판정 전체를 멈추지 않는다.
    trade_since: dict[str, datetime] = {}
    last_signal: dict[str, datetime] = {}
    demoted_at: dict[str, datetime] = {}
    rotation_ready = True
    try:
        from .. import journal

        trade_since = store.last_action_at("promote_trade")
        demoted_at = store.last_action_at("demote")
        last_signal = journal.last_signal_at()
    except Exception:  # noqa: BLE001 - 회전은 부가 기능이다
        # **플래그를 내려야 실제로 회전이 멈춘다.** 종전에는 빈 dict 로 두고
        # 문구만 "회전하지 않는다" 였는데, silent() 는 재료가 없으면 편입
        # 시각으로 폴백하고 last_signal 도 비어 **전 종목이 침묵으로 분류**됐다
        # — DB 잠금 한 번에 매매 tier 가 사이클당 3건씩 강등될 수 있었다
        # (감사 2026-08-01 H5).
        rotation_ready = False
        log.exception("회전 판정 재료 조회 실패 — 이번 사이클은 회전하지 않는다")
    return Current(tier=tier, since=since, protected=frozenset(protected),
                   held=frozenset(held), names=names,
                   trade_since=trade_since, last_signal=last_signal,
                   demoted_at=demoted_at, rotation_ready=rotation_ready)


def _parse_ts(raw) -> datetime:
    try:
        t = datetime.fromisoformat(str(raw))
        return t if t.tzinfo else t.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)      # 알 수 없으면 '방금' — 체류시간 보호가 걸린다


async def apply(decisions: list[dict]) -> frozenset[str]:
    """결정을 감시목록에 반영한다. **모드가 허용하는 것만.**

    반환은 **실제로 반영된 코드 집합**이다. 건수(int)였을 때는 배치에 5건 중
    1건만 성공해도 로그가 5건 전부 applied=1 로 남았고(감사 2026-08-01 H3),
    그 오염이 회전 판정(last_action_at → silent)까지 흘렀다.
    """
    from ..data import watchlist

    m = mode()
    if m == "shadow" or frozen():
        return frozenset()
    done: set[str] = set()
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
            done.add(d["code"])
        except Exception:  # noqa: BLE001 - 한 종목 실패가 나머지를 막지 않는다
            log.exception("감시목록 반영 실패: %s", d["code"])
    if done:
        await watchlist.notify()
    return frozenset(done)


# ka10095 한 콜에 넣을 종목 수 상한. 89종목은 실측으로 확인했고(2026-07-30),
# 그 위는 아직 안 재봤다 — 모르는 한도에 기대는 대신 여유를 두고 나눈다.
WATCH_INFO_CHUNK = 80


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


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
            if not src.enabled():
                # 꺼진 소스는 '오늘 아직 보고하지 않았다' 로 되돌린다.
                #
                # 장중 소스를 마감 후 끄기 시작하면서 생긴 구멍이다: 다음 날 09:00 에
                # 어제 신호는 TTL(180초)로 이미 만료됐는데 `polled` 에는 남아 있어
                # `ready()` 가 True 가 된다. 그 상태로 투영하면 장중 소스 점수가
                # 전부 0 이라 **개장 직후 대량 강등**이 나간다(MAX_SHRINK 가 10건에서
                # 끊을 뿐이다). 종전에는 밤새 폴링해서 이 창이 없었다.
                self.polled.discard(src.name)
                continue
            if not self.due(src.name, now):
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
            empty = store.mark_ok(src.name, len(signals))
            # 조회 성공 = 소스 정상, 이 아니다. presurge 가 사흘간 `fails: 0` 인 채
            # 0건이었고 아무 데도 드러나지 않았다(문턱 단위가 틀려 200건을 받아
            # 전량 탈락). 정당하게 0건인 시간대는 `enabled()` 가 걸러 준다.
            if empty and empty % EMPTY_WARN_EVERY == 0:
                log.warning("%s 연속 %d회 0건 — 조회는 성공하는데 통과가 없다."
                            " 필터 문턱·응답 단위를 확인할 것", src.name, empty)
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
        from ..kiwoom.quote import parse_watch_info

        targets = [c for c in self.candidates
                   if cur.tier.get(c.code) == promote.COLLECT]
        if not targets:
            return 0
        # **한 콜로 묶는다.** 종목당 1콜(ka10006)이던 시절 full 전환 직후
        # mrkcond 가 분당 72~85콜까지 올라 전체 예산의 절반을 먹었다(실측
        # 2026-07-30). ka10095 는 89종목을 1콜로 받는다.
        #
        # 한 콜이라 실패도 전부 함께 실패한다 — 그게 맞다. 시세를 못 받으면
        # price_fresh 가 False 로 남고 `_tradable` 이 이번 사이클 승격을
        # 보류한다. 30초 뒤 다시 온다.
        got = 0
        for chunk in _chunks([c.code for c in targets], WATCH_INFO_CHUNK):
            try:
                quotes = parse_watch_info(await client.watch_info(chunk))
            except Exception as e:  # noqa: BLE001 - 못 받으면 승격을 미룬다
                log.warning("현재가 묶음 조회 실패 %d종목 — 이번 사이클 매매 승격 "
                            "보류: %s", len(chunk), e)
                continue
            # **판정과 독립으로 관측을 남긴다.** `decisions` 에만 실으면 승격이
            # 일어난 종목만 남는데, 매매 tier 가 상한에 닿으면 판정 자체가 안
            # 일어난다 — 실측 2026-07-31: `promote_trade` 결정이 7/29 이후 0건
            # 이라 스프레드가 하나도 안 쌓였다. 여기서 남기면 판정 여부와 무관하게
            # 후보 전체(실측 92종목)가 매 사이클 기록된다. 추가 호출은 없다.
            store.record_quotes(quotes, {c.code: c.name for c in targets})
            for c in targets:
                q = quotes.get(c.code)
                if q:
                    c.quote = q
                    got += 1
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
        store.log_decisions(rows, mode=mode(), applied=applied)
        self.last_cycle = datetime.now(KST).isoformat(timespec="seconds")
        if rows:
            log.info("발굴 엔진(%s): 신호 %d · 결정 %d · 반영 %d",
                     mode(), got, len(rows), len(applied))
        return {"signals": got, "decisions": len(rows), "applied": len(applied)}

    async def loop(self, interval_sec: int = 30) -> None:
        purged_for = ""
        while True:
            try:
                if settings.CONFIG.get("scout", {}).get("enabled", True):
                    await self.run_once()
                    self.last_error = ""
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
                log.exception("발굴 엔진 오류")
            try:
                # 원장 정리 — 하루 1회. run_once 와 **다른 try** 다: 같은 블록에
                # 두면 run_once 예외가 그날 정리를 영영 막고, 정리 실패가
                # last_error 를 '엔진 오류' 로 오염시킨다(감사 2026-08-01 L3).
                today = datetime.now(KST).date().isoformat()
                if purged_for != today:
                    n = await asyncio.to_thread(store.purge)
                    purged_for = today
                    if n:
                        log.info("신호 원장 정리 — %d행 (보존 %d일)",
                                 n, store.RETAIN_DAYS)
            except Exception:  # noqa: BLE001 - 정리 실패가 엔진을 멈추지 않는다
                log.exception("신호 원장 정리 실패 — 내일 다시 시도")
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
