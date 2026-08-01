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
    """야간 전종목 배치 (평일 17:30, scout/nightly.py) — picks 를 소비한다.

    **발굴을 다시 돌리지 않는다.** 전종목 수집·피처 계산은 그대로 두고 결과만
    읽는다. 그 배치는 3,900종목을 도는 무거운 작업이라 엔진 주기로 돌 수 없다.

    strength 는 3규칙 점수 0~3 의 정규화지만 **예측력은 없다** — 이 점수 순
    상위 N 이 매수가능 무작위보다 0.22R 나빴다(2026-07-27 실측). 소스 합의를
    세는 용도로만 쓴다.
    """

    name = model.NIGHTLY

    def _cfg(self) -> dict:
        return settings.CONFIG.get("nightly", {})

    def enabled(self) -> bool:
        return bool(self._cfg().get("enabled", True))

    def interval_sec(self) -> int:
        return 1_800        # 30분마다 확인 — 배치가 끝났는지 보는 것뿐이라 싸다

    async def collect(self) -> list[Signal]:
        from ..nightly import latest_picks

        date, picks = latest_picks()
        out: list[Signal] = []
        for p in picks:
            if not p.get("code"):
                continue
            # **강도는 팔에 상관없이 하나다**(`NIGHTLY_STRENGTH`). 종전에는 점수
            # 표본만 `score_strength` 를 받아 자리가 모자랄 때 앞자리를 가져갔는데,
            # 변동성 정합 대조군으로 재니 점수 표본이 -0.168R(t=-10.70) 이고 점수가
            # 높을수록 더 나빴다. 그 순서는 거꾸로였다. 무작위를 위에 두지도
            # 않는다 — 대조군이지 이긴 쪽이 아니다. 둘 다 모르므로 같은 값이다.
            #
            # 원시 점수는 `raw` 와 `evidence` 에 그대로 남는다. 팔 구분은 `kind`
            # 가 하므로 사후 대조에서 섞이지 않는다.
            arm = p.get("pick_kind") or "score"
            out.append(Signal(
                code=p["code"], name=p.get("name") or p["code"], source=self.name,
                kind=self.name if arm == "score" else f"{self.name}:{arm}",
                strength=model.NIGHTLY_STRENGTH,
                raw=float(p.get("score", 0)),
                price=float(p.get("close", 0) or 0),
                evidence={"date": date, "pick_kind": p.get("pick_kind") or "score",
                          "reasons": p.get("reasons") or []},
            ))
        return out


class NewsSource:
    """뉴스·공시 (TNM) — `GET /api/items` 를 소비하는 **유일한** 편입 채널.

    TNM 의 `promote.loop()` 직접 편입은 2026-08-01 완전 통합에서 삭제됐다.
    그쪽의 보수적 게이트(min_score 70 · negative/unclear 차단)를 이 소스가
    계승한다. 소스를 `news` 로 태깅해야 만료·강등이 정상 동작한다.
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
                params={"min_score": min_score, "limit": limit,
                        "status": settings.TNM_STATUS_OK},
                headers={"X-Internal-Token": settings.TNM_TOKEN},
            )
            res.raise_for_status()
            items = res.json().get("items") or []
        out = []
        for it in items:
            code = str(it.get("ticker") or "").strip()
            # 악재·방향미상은 매수 후보가 아니다 — 폐기된 TNM promote 의
            # 게이트(negative·unclear 차단)를 계승한다(2026-08-01 완전 통합).
            # 방향이 빈 값은 통과 — 분류가 아직 안 붙은 것이지 '미상 판정' 이
            # 아니다(promote 의 규약과 동일).
            if not code or it.get("impact_direction") in ("negative", "unclear"):
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
