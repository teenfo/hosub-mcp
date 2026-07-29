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
from ..data.exclude import is_excluded as _is_excluded

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
                # 현재 거래량(주) — ka10027 은 거래대금 필드가 없어 거래량×현재가로 근사.
                "volume": _num(it.get("now_trde_qty"), int),
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


def filter_presurge(items: list[dict], cfg: dict,
                    keep: frozenset = frozenset()) -> list[dict]:
    """'급등 조짐' 필터: 거래량은 급증했는데 가격은 아직 크게 안 움직인 종목.
    거래량이 가격에 선행한다는 전제의 조기 포착 — 확정 신호가 아니라 관찰 후보다.

    keep: 이미 감시 중이어도 후보로 유지할 코드. 형제 필터(filter_candidates /
    filter_gainers)와 같은 규약이다. 발굴 엔진은 여기에 감시목록 전체를 넘겨
    '이미 감시 중이면 제외' 를 무력화한다 — 그러지 않으면 승격 직후 소스가
    보고를 멈춰 신호가 만료되고, 강등되면 다시 보고하는 발진이 생긴다.
    """
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
        and (it["code"] not in settings.WATCHLIST or it["code"] in keep)
    ]
    picked.sort(key=lambda x: x.get("surge_pct", 0), reverse=True)
    return picked[:top_n]


def trade_tier_cap(cfg: dict) -> float:
    """매매 tier 로 넣을 최고 주가. 자산이 동기화돼 있으면 '한 종목 최대 투입액'
    (자산 × 종목당 비중 상한)을 쓰고, 아니면 config 고정값으로 폴백한다.
    1주도 살 수 없는 종목을 매매 대상에 올려 봐야 '잔고 부족'만 쌓인다."""
    return settings.tradable_price_cap(cfg.get("trade_max_price", 30_000))


def filter_candidates(items: list[dict], cfg: dict,
                      keep: frozenset = frozenset()) -> list[dict]:
    """거래대금 상위에서 등락률·유동성·가격 필터 교차. collect_only tier 부여.

    keep: 기존 active 소스 코드 — 이미 감시 중이어도 후보 유지(교체 시 탈락 방지).
    """
    min_chg = cfg.get("min_change_pct", 3.0)
    min_val = cfg.get("min_trade_value", 10_000)   # trde_prica 와 같은 단위(통상 백만원)
    min_price = cfg.get("min_price", 1_000)
    top_n = cfg.get("top_n", 10)
    picked = [
        it for it in items
        if it["change_pct"] >= min_chg
        and it["trade_value"] >= min_val
        and it["price"] >= min_price
        and not _is_excluded(it["name"])
        and (it["code"] not in settings.WATCHLIST or it["code"] in keep)
    ]
    picked.sort(key=lambda x: x["change_pct"], reverse=True)
    picked = picked[:top_n]
    cap = trade_tier_cap(cfg)
    for p in picked:
        p["collect_only"] = p["price"] > cap    # 1주도 못 사는 가격이면 수집전용
    return picked




def filter_gainers(items: list[dict], cfg: dict,
                   keep: frozenset = frozenset()) -> list[dict]:
    """급등률 상위에서 유동성·저가·비ETF·우선주 제외 후 상위 N. collect_only tier 부여
    (매매가능 저가주 = trade_max_price 이하 → 매매, 그 외 → 수집전용).
    ka10027 은 거래대금 필드가 없어 '거래량×현재가(원)' 로 유동성을 근사한다.
    keep: 기존 gainer 소스 코드 — 이미 감시 중이어도 유지(교체 시 탈락 방지)."""
    min_price = cfg.get("min_price", 1_000)
    min_tv = cfg.get("min_trade_value_krw", 100_000_000)   # 거래대금 근사 하한(원)
    tmax = trade_tier_cap(cfg)
    top_n = cfg.get("top_n", 15)
    picked = [
        it for it in items
        if it["change_pct"] > 0
        and it["price"] >= min_price
        and it["price"] * it.get("volume", 0) >= min_tv
        and not _is_excluded(it["name"])
        # seed/manual/auto 로 이미 감시 중이면 제외하되, 기존 gainer 는 유지
        and (it["code"] not in settings.WATCHLIST or it["code"] in keep)
    ]
    picked.sort(key=lambda x: x["change_pct"], reverse=True)
    picked = picked[:top_n]
    for p in picked:
        p["collect_only"] = p["price"] > tmax   # 고가주는 수집전용
    return picked


def _engine_owns() -> bool:
    """엔진(full)이 감시목록을 소유하면 스캐너는 **직접 쓰지 않는다.**

    파싱·필터 결과(`self.results`/`self.gainers`)는 그대로 남는다 — 화면이 읽고
    엔진의 volume·gainers 어댑터가 같은 함수를 재사용한다. 회수되는 것은
    '감시목록에 직접 쓰는' 부분뿐이다.
    """
    from ..scout import engine as scout

    if scout.owns_watchlist():
        log.info("스캐너 자동편입 보류 — 감시목록은 엔진(full)이 소유")
        return True
    return False


class Scanner:
    def __init__(self) -> None:
        self.results: list[dict] = []       # 이미 급등 중 (편승 후보)
        self.presurge: list[dict] = []      # 급등 조짐 (거래량 선행)
        self.gainers: list[dict] = []       # KOSPI 급등률 상위 (자동편입 대상)
        self.last_scan: str = ""

    async def scan_once(self) -> list[dict]:
        from ..data import watchlist
        from ..kiwoom.client import client  # 지연 임포트 (테스트 오프라인)

        cfg = settings.CONFIG.get("scanner", {})
        raw = await client.trade_value_rank(cfg.get("market", "000"))
        # 기존 active 소스는 이미 감시 중이어도 후보 유지(교체 시 탈락 방지)
        keep = frozenset(e["code"] for e in watchlist.entries()
                         if e.get("source") == "active")
        self.results = filter_candidates(parse_rank(raw), cfg, keep=keep)
        if cfg.get("auto_watch", False) and not _engine_owns():
            # 거래대금 상위는 '시장이 실제로 돈을 넣는' 종목이라 유동성이 안전하다.
            # 이 필터 결과가 종전에는 화면 표시로만 쓰이고 감시목록에 닿지 않았다.
            watchlist.replace_active(self.results)
            await watchlist.notify()
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
        """등락률 상위(ka10027) 조회 → 필터 → (옵션) 감시목록 자동편입.

        시장을 여러 개 지정할 수 있다(markets). 코스피만 보면 변동성 큰 코스닥
        급등주가 구조적으로 들어오지 않는다 — 실측 2026-07-24 변동폭 상위 20 중
        감시 중이던 종목은 2개뿐이었다.
        """
        from ..data import watchlist
        from ..kiwoom.client import client

        cfg = settings.CONFIG.get("gainers", {})
        if not cfg.get("enabled", True):
            return []
        markets = cfg.get("markets") or [cfg.get("market", "001")]
        items: list[dict] = []
        seen: set[str] = set()
        for mk in markets:
            try:
                raw = await client.change_rate_rank(str(mk))
            except Exception as e:  # noqa: BLE001 - 한 시장 실패가 나머지를 막지 않는다
                log.warning("급등률 조회 실패(시장 %s): %s", mk, e)
                continue
            for it in parse_rank(raw):
                if it["code"] and it["code"] not in seen:
                    seen.add(it["code"])
                    items.append(it)
        # 기존 gainer 소스 코드는 이미 감시 중이어도 후보 유지(교체 시 탈락 방지)
        keep = frozenset(e["code"] for e in watchlist.entries()
                         if e.get("source") == "gainer")
        self.gainers = filter_gainers(items, cfg, keep=keep)
        if cfg.get("auto_watch", True) and not _engine_owns():
            # 성공한 스캔은 빈 결과라도 반영 — 더 이상 급등이 아닌 종목을 정리
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
