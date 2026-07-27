"""느린 소스 3종 — 야간 발굴 · 뉴스/공시 · 사용자 수동 지정.

장중 소스와 달리 하루 1회~1시간 주기다. 신호 수명도 길어서(nightly 1일 /
news 12시간) 한 번 들어온 종목이 그날 내내 후보로 남는다.
"""
import logging

from ... import settings
from .. import model
from ..model import Signal

log = logging.getLogger(__name__)


class NightlySource:
    """야간 전종목 발굴 (평일 17:30) — `discovery.latest()` 를 소비한다.

    **발굴을 다시 돌리지 않는다.** 전종목 수집·피처 계산은 그대로 두고 결과만
    읽는다. 그 배치는 3,900종목을 도는 무거운 작업이라 엔진 주기로 돌 수 없다.

    strength 는 3규칙 점수 0~3 의 정규화지만 **예측력은 없다** — 이 점수 순
    상위 N 이 매수가능 무작위보다 0.22R 나빴다(2026-07-27 실측). 소스 합의를
    세는 용도로만 쓴다.
    """

    name = model.NIGHTLY

    def _cfg(self) -> dict:
        return settings.CONFIG.get("discovery", {})

    def enabled(self) -> bool:
        return bool(self._cfg().get("enabled", True))

    def interval_sec(self) -> int:
        return 1_800        # 30분마다 확인 — 배치가 끝났는지 보는 것뿐이라 싸다

    async def collect(self) -> list[Signal]:
        from ...discovery import latest_picks

        date, picks = latest_picks()
        return [
            Signal(
                code=p["code"], name=p.get("name") or p["code"], source=self.name,
                kind=self.name,
                strength=model.score_strength(float(p.get("score", 0))),
                raw=float(p.get("score", 0)),
                price=float(p.get("close", 0) or 0),
                evidence={"date": date, "reasons": p.get("reasons") or []},
            )
            for p in picks if p.get("code")
        ]


class NewsSource:
    """뉴스·공시 (TNM) — 기존 `GET /api/items` 를 그대로 소비한다.

    TNM 의 `promote.loop()` 이 직접 편입하던 것을 대체한다. 지금은 trading API 에
    `manual` 로 넣어서 **어떤 정리 경로도 지우지 않아 영구 잔류**한다.
    소스를 `news` 로 태깅해야 만료·강등이 정상 동작한다.
    """

    name = model.NEWS

    def _cfg(self) -> dict:
        return settings.CONFIG.get("scout", {}).get("news", {})

    def enabled(self) -> bool:
        # **trading 자신의 INTERNAL_TOKEN 이 아니다** — 서비스마다 토큰이 따로라
        # 자기 것을 보내면 401 이 온다(실측 2026-07-27, 이 어댑터 첫 배포).
        return bool(self._cfg().get("enabled", True)) and bool(settings.TNM_TOKEN)

    def interval_sec(self) -> int:
        return int(self._cfg().get("interval_sec", 900))

    async def collect(self) -> list[Signal]:
        import httpx

        cfg = self._cfg()
        url = cfg.get("url") or settings.TNM_URL
        min_score = int(cfg.get("min_score", 60))
        limit = int(cfg.get("limit", 50))
        async with httpx.AsyncClient(timeout=10.0) as c:
            res = await c.get(
                f"{url}/api/items",
                params={"min_score": min_score, "limit": limit, "status": "done"},
                headers={"X-Internal-Token": settings.TNM_TOKEN},
            )
            res.raise_for_status()
            items = res.json().get("items") or []
        out = []
        for it in items:
            code = str(it.get("ticker") or "").strip()
            # 악재는 매수 후보가 아니다 — 원장에는 남기지 않고 여기서 건너뛴다
            if not code or it.get("impact_direction") == "negative":
                continue
            out.append(Signal(
                code=code, name=it.get("name") or code, source=self.name,
                kind=f"{self.name}:{it.get('category') or '기타'}",
                strength=model.news_strength(float(it.get("score") or 0)),
                raw=float(it.get("score") or 0),
                evidence={"title": it.get("title"), "url": it.get("url"),
                          "category": it.get("category"),
                          "published_at": str(it.get("published_at") or "")},
            ))
        return out


class ManualSource:
    """사용자가 직접 지정한 종목 — 감시목록의 seed/manual 항목.

    만료도 감쇠도 없다. 엔진이 사용자의 결정을 덮어쓰면 안 되기 때문이다.
    기존 `replace_*` 가 seed/manual 을 건드리지 않던 규약을 그대로 옮긴 것이다.
    """

    name = model.MANUAL

    def enabled(self) -> bool:
        return True

    def interval_sec(self) -> int:
        return 300

    async def collect(self) -> list[Signal]:
        from ...data import watchlist

        return [
            Signal(code=e["code"], name=e.get("name") or e["code"],
                   source=self.name, kind=e.get("source") or "manual",
                   strength=1.0, evidence={"added": e.get("added")})
            for e in watchlist.entries()
            if e.get("source") in ("seed", "manual") and e.get("code")
        ]
