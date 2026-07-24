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


def parse_surge(raw: dict) -> list[dict]:
    """거래량급증(ka10023) 응답 표준화. 배열 키: trde_qty_sdnin."""
    out = []
    for it in raw.get("trde_qty_sdnin") or []:
        out.append(
            {
                "code": str(it.get("stk_cd", "")).lstrip("A"),
                "name": it.get("stk_nm", ""),
                "price": abs(_num(it.get("cur_prc"), int)),
                "change_pct": _num(it.get("flu_rt")),
                "surge_pct": _num(it.get("sdnin_rt")),      # 거래량 급증률
                "now_volume": _num(it.get("now_trde_qty"), int),
            }
        )
    return out


def filter_presurge(items: list[dict], cfg: dict) -> list[dict]:
    """'급등 조짐' 필터: 거래량은 급증했는데 가격은 아직 크게 안 움직인 종목.
    거래량이 가격에 선행한다는 전제의 조기 포착 — 확정 신호가 아니라 관찰 후보다."""
    min_surge = cfg.get("min_volume_surge_pct", 300.0)   # 거래량 급증률 최소
    lo = cfg.get("change_pct_min", -1.0)                 # 등락률 하한
    hi = cfg.get("change_pct_max", 3.0)                  # 이 이상 오르면 이미 급등(기존 스캐너 몫)
    min_price = cfg.get("min_price", 1_000)
    top_n = cfg.get("top_n", 10)
    picked = [
        it for it in items
        if it.get("surge_pct", 0) >= min_surge
        and lo <= it["change_pct"] <= hi
        and it["price"] >= min_price
        and it["code"] not in settings.WATCHLIST
    ]
    picked.sort(key=lambda x: x.get("surge_pct", 0), reverse=True)
    return picked[:top_n]


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


# ETF·ETN·리츠·스팩 등 비보통주 제외(급등 자동편입 시 잡ETF 유입 방지)
_EXCL_KW = ("KODEX", "TIGER", "KOSEF", "ARIRANG", "HANARO", "PLUS", "RISE", "ACE",
            "SOL", "KBSTAR", "TIMEFOLIO", "ETN", "레버리지", "인버스", "선물",
            "리츠", "스팩", "채권", "국채", "커버드콜", "배당", "TR")


def _is_excluded(name: str) -> bool:
    return any(k in (name or "") for k in _EXCL_KW)


def filter_gainers(items: list[dict], cfg: dict) -> list[dict]:
    """급등률 상위에서 유동성·저가·비ETF 필터 후 상위 N. collect_only tier 부여
    (매매가능 저가주 = trade_max_price 이하 → 매매, 그 외 → 수집전용)."""
    min_price = cfg.get("min_price", 1_000)
    min_val = cfg.get("min_trade_value", 5_000)
    tmax = cfg.get("trade_max_price", 30_000)
    top_n = cfg.get("top_n", 15)
    picked = [
        it for it in items
        if it["change_pct"] > 0
        and it["price"] >= min_price
        and it["trade_value"] >= min_val
        and not _is_excluded(it["name"])
        and it["code"] not in settings.WATCHLIST
    ]
    picked.sort(key=lambda x: x["change_pct"], reverse=True)
    picked = picked[:top_n]
    for p in picked:
        p["collect_only"] = p["price"] > tmax   # 고가주는 수집전용
    return picked


class Scanner:
    def __init__(self) -> None:
        self.results: list[dict] = []       # 이미 급등 중 (편승 후보)
        self.presurge: list[dict] = []      # 급등 조짐 (거래량 선행)
        self.gainers: list[dict] = []       # KOSPI 급등률 상위 (자동편입 대상)
        self.last_scan: str = ""

    async def scan_once(self) -> list[dict]:
        from ..kiwoom.client import client  # 지연 임포트 (테스트 오프라인)

        cfg = settings.CONFIG.get("scanner", {})
        raw = await client.trade_value_rank(cfg.get("market", "000"))
        self.results = filter_candidates(parse_rank(raw), cfg)
        try:
            surge_raw = await client.volume_surge_rank(cfg.get("market", "000"))
            self.presurge = filter_presurge(parse_surge(surge_raw), cfg)
        except Exception as e:  # noqa: BLE001 - 조짐 스캔 실패는 비치명적
            log.warning("거래량급증 스캔 실패: %s", e)
        try:
            await self.scan_gainers()
        except Exception as e:  # noqa: BLE001 - 급등률 스캔 실패는 비치명적
            log.warning("급등률 상위 스캔 실패: %s", e)
        self.last_scan = datetime.now(KST).isoformat(timespec="seconds")
        return self.results

    async def scan_gainers(self) -> list[dict]:
        """KOSPI 등락률 상위(ka10027) 조회 → 필터 → (옵션) 감시목록 자동편입."""
        from ..data import watchlist
        from ..kiwoom.client import client

        cfg = settings.CONFIG.get("gainers", {})
        if not cfg.get("enabled", True):
            return []
        raw = await client.change_rate_rank(cfg.get("market", "001"))
        self.gainers = filter_gainers(parse_rank(raw), cfg)
        if cfg.get("auto_watch", True) and self.gainers:
            watchlist.replace_gainers(self.gainers)
            await watchlist.notify()
        return self.gainers

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
