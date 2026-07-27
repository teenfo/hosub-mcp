"""장중 소스 3종 — 거래대금 상위 · 등락률 상위 · 거래량 급증.

세 소스 모두 `scanner.py` 의 파싱·필터 함수를 그대로 쓴다. 새 로직을 만들지
않는 이유는 화면에 보이는 후보와 엔진이 보는 후보가 갈라지면 안 되기 때문이다.

`presurge` 는 지금까지 **감시목록 편입 경로가 아예 없었다**(화면 표시만).
그래서 성적 데이터가 0건이고, 좋은지 나쁜지 알 수 없다. 엔진에 넣되 관측
전용으로 시작하는 이유다.
"""
import logging

from ... import settings
from ...signals import scanner
from .. import model
from ..model import Signal
from .base import rank_signals, watched_keep

log = logging.getLogger(__name__)


class VolumeSource:
    """거래대금 상위 (ka10032) — '시장이 실제로 돈을 넣고 있는' 종목."""

    name = model.VOLUME

    def _cfg(self) -> dict:
        return settings.CONFIG.get("scanner", {})

    def enabled(self) -> bool:
        return bool(self._cfg().get("enabled", True)) and bool(settings.KIWOOM_APP_KEY)

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
        return bool(self._cfg().get("enabled", True)) and bool(settings.KIWOOM_APP_KEY)

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

    def _cfg(self) -> dict:
        return settings.CONFIG.get("scanner", {})

    def enabled(self) -> bool:
        return bool(self._cfg().get("enabled", True)) and bool(settings.KIWOOM_APP_KEY)

    def interval_sec(self) -> int:
        return int(self._cfg().get("interval_sec", 60))

    async def collect(self) -> list[Signal]:
        from ...kiwoom.client import client

        cfg = self._cfg()
        raw = await client.volume_surge_rank(cfg.get("market", "000"))
        items = scanner.filter_presurge(scanner.parse_surge(raw), cfg,
                                        keep=watched_keep())
        # 순위가 아니라 급증률 자체를 세기로 쓴다 — 목록 길이에 좌우되지 않게
        return [
            Signal(
                code=it["code"], name=it.get("name") or it["code"], source=self.name,
                kind=self.name,
                strength=model.surge_strength(float(it.get("surge_pct", 0.0))),
                raw=float(it.get("surge_pct", 0.0)),
                price=float(it.get("price", 0.0)),
                evidence={"surge_pct": it.get("surge_pct"),
                          "change_pct": it.get("change_pct")},
            )
            for it in items if it.get("code")
        ]
