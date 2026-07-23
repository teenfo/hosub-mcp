"""급등주 스캐너 — 거래대금상위(ka10032)에서 하락장 주도주 후보를 추린다.

선정 논리 (docs/requests/trading-deploy.md 의 리서치 결론 반영):
  등락률 상위 단독은 저유동성 잡주가 걸리므로, '거래대금 상위' 목록에서
  등락률·가격 필터를 교차 적용해 시장 전체가 미는 종목만 남긴다.
편승은 감시목록 편입까지만 — 진입/청산은 기존 신호 엔진과 승인 흐름이 담당한다.
"""
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from .. import settings

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")


def _num(v, cast=float):
    try:
        return cast(str(v).replace("+", "").replace("--", "-").strip() or 0)
    except (TypeError, ValueError):
        return cast(0)


def parse_rank(raw: dict) -> list[dict]:
    """순위 TR 응답에서 종목 배열을 찾아 표준화. 배열 키 이름이 TR 마다 달라
    'stk_cd 를 가진 dict 리스트' 를 탐색하는 방식으로 견고하게 처리한다."""
    items = None
    for v in raw.values():
        if isinstance(v, list) and v and isinstance(v[0], dict) and "stk_cd" in v[0]:
            items = v
            break
    out = []
    for it in items or []:
        out.append(
            {
                "code": str(it.get("stk_cd", "")).lstrip("A"),
                "name": it.get("stk_nm", ""),
                "price": abs(_num(it.get("cur_prc"), int)),
                "change_pct": _num(it.get("flu_rt")),
                # 거래대금 단위는 TR 문서상 명시가 없어 원 단위로 정규화하지 않는다.
                # 필터 기준(min_trade_value)과 같은 단위로만 쓰이므로 상대 비교는 유효.
                "trade_value": _num(it.get("trde_prica"), int),
            }
        )
    return out


def filter_candidates(items: list[dict], cfg: dict) -> list[dict]:
    min_chg = cfg.get("min_change_pct", 3.0)
    min_val = cfg.get("min_trade_value", 10_000)   # trde_prica 와 같은 단위(통상 백만원)
    min_price = cfg.get("min_price", 1_000)
    top_n = cfg.get("top_n", 10)
    picked = [
        it for it in items
        if it["change_pct"] >= min_chg
        and it["trade_value"] >= min_val
        and it["price"] >= min_price
        and it["code"] not in settings.WATCHLIST
    ]
    picked.sort(key=lambda x: x["change_pct"], reverse=True)
    return picked[:top_n]


class Scanner:
    def __init__(self) -> None:
        self.results: list[dict] = []
        self.last_scan: str = ""

    async def scan_once(self) -> list[dict]:
        from ..kiwoom.client import client  # 지연 임포트 (테스트 오프라인)

        cfg = settings.CONFIG.get("scanner", {})
        raw = await client.trade_value_rank(cfg.get("market", "000"))
        self.results = filter_candidates(parse_rank(raw), cfg)
        self.last_scan = datetime.now(KST).isoformat(timespec="seconds")
        return self.results

    async def loop(self, interval_sec: int = 60) -> None:
        while True:
            try:
                cfg = settings.CONFIG.get("scanner", {})
                now = datetime.now(KST)
                if (
                    cfg.get("enabled", True)
                    and settings.KIWOOM_APP_KEY
                    and now.weekday() < 5
                    and "09:00" <= now.strftime("%H:%M") <= "15:30"
                ):
                    await self.scan_once()
            except Exception:  # noqa: BLE001
                log.exception("스캐너 오류")
            await asyncio.sleep(interval_sec)
