"""장중 소스 3종 — 거래대금 상위 · 등락률 상위 · 거래량 급증.

세 소스 모두 `scanner.py` 의 파싱·필터 함수를 그대로 쓴다. 새 로직을 만들지
않는 이유는 화면에 보이는 후보와 엔진이 보는 후보가 갈라지면 안 되기 때문이다.

`presurge` 는 지금까지 **감시목록 편입 경로가 아예 없었다**(화면 표시만).
그래서 성적 데이터가 0건이고, 좋은지 나쁜지 알 수 없다. 엔진에 넣되 관측
전용으로 시작하는 이유다.
"""
import logging
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from ... import settings
from ...signals import scanner
from .. import model, promote
from ..model import Signal
from .base import rank_signals, watched_keep

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
OPEN = time(9, 0)
# 개장 후 이만큼 지나기 전에는 급증률을 믿지 않는다. 급증률의 분모인 '직전 기간
# 거래량' 이 개장 직후에는 존재하지 않아 비율이 폭발하기 때문이다(model.py
# SURGE_SANE_MAX 주석의 실측 참조). 기다리는 쪽을 택한 이유: 이 소스는 확정
# 신호가 아니라 관찰 후보이고, 개장 10분을 놓치는 비용보다 대형주가 만점을
# 받아 취합을 오염시키는 비용이 크다.
PRESURGE_WARMUP_MIN = 10


def session_open(now: datetime | None = None) -> bool:
    """장중인가. **세 소스 모두 장중에만 의미가 있다.**

    종전에는 마감 후에도 순위 API 를 매 분 계속 불렀다. 순위는 종가에 고정되므로
    같은 스냅샷을 밤새 되풀이해 적재하고, 그 신호로 **개장 전에 승격 결정이
    나갔다**(실측 2026-07-29 21~23 UTC = 7/30 06~08시 KST, `promote_collect` 6건).
    전날 종가 순위로 오늘 종목을 정하는 것은 근거가 약하고, 그 사이 API 호출은
    전부 낭비다(소스 3종 × 분당 1콜 × 마감 후 17시간).

    `enabled()` 로 끄는 것이 맞다 — 빈 리스트를 돌려주면 `mark_ok(0)` 이 찍혀
    '연속 0건' 경고가 매일 밤 울린다. 마감은 이상이 아니다.

    강등이 `promote.in_session` 으로 이미 막혀 있으므로, 마감 시각에 신호가
    만료돼도 감시목록은 그대로 유지된다. 뉴스·야간발굴은 장외에도 계속 돌아
    공시·리포트 기반 승격 경로는 살아 있다.
    """
    return promote.in_session(now)


class VolumeSource:
    """거래대금 상위 (ka10032) — '시장이 실제로 돈을 넣고 있는' 종목."""

    name = model.VOLUME

    def _cfg(self) -> dict:
        return settings.CONFIG.get("scanner", {})

    def enabled(self) -> bool:
        return (bool(self._cfg().get("enabled", True))
                and bool(settings.KIWOOM_APP_KEY) and session_open())

    def interval_sec(self) -> int:
        return int(self._cfg().get("interval_sec", 60))

    async def collect(self) -> list[Signal]:
        from ...kiwoom.client import client

        cfg = self._cfg()
        raw = await client.trade_value_rank(cfg.get("market", "000"))
        items = scanner.filter_candidates(scanner.parse_rank(raw), cfg,
                                          keep=watched_keep())
        return rank_signals(items, self.name)


class GainersSource:
    """등락률 상위 (ka10027) — 여러 시장을 합쳐서 본다.

    코스피만 보면 변동성 큰 코스닥 급등주가 구조적으로 들어오지 않는다
    (실측 2026-07-24: 변동폭 상위 20 중 감시 중이던 종목 2개).
    """

    name = model.GAINERS

    def _cfg(self) -> dict:
        return settings.CONFIG.get("gainers", {})

    def enabled(self) -> bool:
        return (bool(self._cfg().get("enabled", True))
                and bool(settings.KIWOOM_APP_KEY) and session_open())

    def interval_sec(self) -> int:
        return int(self._cfg().get("interval_sec", 60))

    async def collect(self) -> list[Signal]:
        from ...kiwoom.client import client

        cfg = self._cfg()
        markets = cfg.get("markets") or [cfg.get("market", "001")]
        items, seen = [], set()
        failures = 0
        for mk in markets:
            try:
                raw = await client.change_rate_rank(str(mk))
            except Exception as e:  # noqa: BLE001 - 한 시장 실패가 나머지를 막지 않는다
                failures += 1
                log.warning("급등률 조회 실패(시장 %s): %s", mk, e)
                continue
            for it in scanner.parse_rank(raw):
                if it["code"] and it["code"] not in seen:
                    seen.add(it["code"])
                    items.append(it)
        if failures and failures == len(markets):
            # 전부 실패한 것을 '급등주 없음' 으로 보고하면 TTL 이 무의미해진다
            raise RuntimeError(f"급등률 전 시장 조회 실패({failures}건)")
        return rank_signals(scanner.filter_gainers(items, cfg, keep=watched_keep()),
                            self.name)


class PresurgeSource:
    """거래량 급증 (ka10023) — 거래량은 터졌는데 가격은 아직 안 움직인 종목.

    거래량이 가격에 선행한다는 전제의 조기 포착이다. **확정 신호가 아니라
    관찰 후보**이고, 편입 경로가 없어 지금껏 측정된 적이 없다.
    """

    name = model.PRESURGE

    def __init__(self) -> None:
        # 종목별 (관측 시각, 당일 누적 거래량) 이력 — 문서 정합 관측의 재료.
        # 장중에만 자라고 프로세스 재시작이면 비는 메모리 값이다(관측 필드가
        # 몇 분 빌 뿐이라 디스크에 남길 이유가 없다).
        self._vol_hist: dict[str, list[tuple[datetime, int]]] = {}

    def _cfg(self) -> dict:
        return settings.CONFIG.get("scanner", {})

    def enabled(self) -> bool:
        return (bool(self._cfg().get("enabled", True))
                and bool(settings.KIWOOM_APP_KEY) and session_open())

    def interval_sec(self) -> int:
        return int(self._cfg().get("interval_sec", 60))

    def warmed_up(self, now: datetime | None = None) -> bool:
        """개장 워밍업이 끝났는가. 장중이 아니면 판단 자체를 하지 않는다.

        빈 리스트를 돌려주는 것이지 예외가 아니다 — 예외로 올리면 백오프가
        걸리고 `source_health` 가 '실패' 로 물든다. 워밍업은 실패가 아니다.
        """
        t = (now or datetime.now(UTC)).astimezone(KST)
        mins = int(self._cfg().get("presurge_warmup_min", PRESURGE_WARMUP_MIN))
        open_min = OPEN.hour * 60 + OPEN.minute
        return (t.hour * 60 + t.minute) >= open_min + mins

    async def collect(self) -> list[Signal]:
        from ...kiwoom.client import client

        cfg = self._cfg()
        if not self.warmed_up():
            return []
        raw = await client.volume_surge_rank(cfg.get("market", "000"))
        items = scanner.filter_presurge(scanner.parse_surge(raw), cfg,
                                        keep=watched_keep())
        now = datetime.now(UTC)
        doc_shares = int(cfg.get("doc_surge_shares", 100_000))
        out: list[Signal] = []
        # 순위가 아니라 급증률 자체를 세기로 쓴다 — 목록 길이에 좌우되지 않게
        for it in items:
            if not it.get("code"):
                continue
            # 문서 정합 관측 — 연구 문서(2026-08-01 §2.3)는 급증을 "3분봉
            # 거래량 10만~15만 주 이상" 으로 정의한다. 우리 세기(sdnin_rt,
            # 누적 대비 %)와 정의가 다르므로 **둘 다 남겨** 4주 뒤 어느
            # 정의가 실현 R 과 상관있는지 가른다. 3분 거래량은 누적 거래량
            # (now_volume)의 약 3분 전 대비 증가분으로 근사한다 — 콜 0.
            vol3 = self._vol_3min(it["code"], int(it.get("now_volume") or 0), now)
            out.append(Signal(
                code=it["code"], name=it.get("name") or it["code"], source=self.name,
                kind=self.name,
                strength=model.surge_strength(float(it.get("surge_pct", 0.0))),
                raw=float(it.get("surge_pct", 0.0)),
                price=float(it.get("price", 0.0)),
                evidence={"surge_pct": it.get("surge_pct"),
                          "change_pct": it.get("change_pct"),
                          "vol_3min": vol3,
                          "doc_surge": (None if vol3 is None
                                        else int(vol3 >= doc_shares))},
            ))
        return out

    def _vol_3min(self, code: str, now_volume: int,
                  now: datetime) -> int | None:
        """약 3분 전 누적 거래량 대비 증가분. 이력이 모자라면 None —
        0 으로 채우면 '거래 없음' 과 '아직 못 쟀다' 가 섞인다."""
        hist = self._vol_hist.setdefault(code, [])
        base = next((v for t, v in hist
                     if 150 <= (now - t).total_seconds() <= 360), None)
        hist.append((now, now_volume))
        del hist[:-6]                          # 최근 6관측(약 6분)만 유지
        if base is None or now_volume < base:
            return None                        # 이력 부족 또는 일자 경계
        return now_volume - base
