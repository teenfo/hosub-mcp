"""느린 소스 3종 — 야간 발굴 · 뉴스/공시 · 사용자 수동 지정.

장중 소스와 달리 하루 1회~1시간 주기다. 신호 수명도 길어서(nightly 1일 /
news 12시간) 한 번 들어온 종목이 그날 내내 후보로 남는다.
"""
import logging
from datetime import datetime

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
        # 배치(`nightly.enabled`)와 **다른 키**다. 편입이 엔진 단일 통로가 된
        # 지금, 배치를 하루 끄는 결정이 전날 picks 소비까지 멎게 하면 감시목록이
        # 조용히 마른다(감사 2026-08-01). 소비자는 따로 끈다.
        return bool(self._cfg().get("source_enabled", True))

    def interval_sec(self) -> int:
        return 1_800        # 30분마다 확인 — 배치가 끝났는지 보는 것뿐이라 싸다

    async def collect(self) -> list[Signal]:
        from ..nightly import latest_picks

        date, picks = latest_picks()
        # **observed_at 은 배치 시각(그날 17:30 KST)이다 — 지금이 아니다.**
        # 종전에는 30분마다 재수집하며 now 로 재스탬프해서 신호 나이가 항상
        # ≤1800초였다: TTL 86400 은 영원히 안 닿고 half-life 감쇠도 하한
        # 0.6575 에 고정돼 죽은 값이었다(감사 2026-08-01 M3). 배치가 며칠
        # 실패해도 묵은 픽이 만점으로 계속 재주입됐다. 배치 시각으로 고정하면
        # 감쇠·만료가 실제로 작동한다 — 새 배치가 없으면 다음 날 저녁 자연
        # 만료되고, 아침(약 15.5시간)에는 0.667×0.64≈0.43 로 승격선(0.35)을
        # 아직 넘는다.
        try:
            batch_at = datetime.fromisoformat(f"{date}T17:30:00+09:00")
        except (TypeError, ValueError):
            batch_at = None
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
                **({"observed_at": batch_at} if batch_at else {}),
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


class FlowSource:
    """외국인/STV 수급 — **이 시스템에서 두 관문을 모두 통과한 최초의 선별 신호.**

    frgn_stv = 외국인 순매수 수량 / 거래량 (`flow_obs`, ka10086). 검증 이력
    (2026-08-03, docs/trading/measurement.md):
      ① IC 재현 — atr_pct 잔차화 후 t=4.9, 본페로니 통과. 정합 분모(수량/거래량)
        가 금액/거래대금을 이겼다(종가 근사 소거).
      ② 랭킹 하네스 — 수급 커버 표본(786종목×99일) 전 방식 재평가에서
        vs_liquid 가 **전 손절폭 양수**(+0.040~+0.056R), 승률 +1.0~2.2%p.

    ## 그래도 관측 전용이다 (max_tier=collect, model.OBSERVE_ONLY)

    효과 크기가 작고(+0.05R/건) 표본이 99일 하락 편중·일 군집 미보정·브래킷
    근사다. 이 소스만 지목한 종목은 수집전용까지만 올라간다 — promote 가 강제.

    ## 판정 — 사전 확정 (편입 2026-08-03, 결과를 본 뒤 바꾸지 않는다)

      시점   4주 뒤(2026-08-31경). 거래일 15일 미만이면 **판정하지 말고 연기 보고.**
      지표   `signals`(source='flow') 지목 종목의 익일 실현 — 같은 날 변동성
             정합(atr_pct 최근접) 무작위 대조군 대비 브래킷 R(하네스 방식,
             손절 1.0~2.5% 스윕).
      기준   하네스와 같다 — 대조군 대비 **전 손절폭 양수면 유지·승격(max_tier
             해제) 논의**, 절반 이하 손절폭에서만 이기면 소스 제거.
      표본 제외  ETF(이름 기준), 수급 관측이 없는 날. 그 외 제외 없음.

    ## 신호 생성

    `flow_obs` 최신일의 frgn_stv **양수** 상위 top_n(기본 5, 하네스와 동일).
    ETF 는 이름으로 제외(유니버스 오염이 측정 둘을 왜곡한 전례). API 콜 0 —
    야간 배치(nightly, 평일 17:30)가 채운 원장을 읽기만 한다.
    """

    name = model.FLOW

    def _cfg(self) -> dict:
        return settings.CONFIG.get("scout", {}).get("flow", {})

    def enabled(self) -> bool:
        return bool(self._cfg().get("enabled", True))

    def interval_sec(self) -> int:
        return 1_800        # 원장이 하루 1회 갱신되므로 30분 확인이면 충분하다

    async def collect(self) -> list[Signal]:
        from ...data.exclude import is_excluded
        from .. import store

        day, rows = store.latest_flows()
        if not day:
            return []
        # **observed_at 은 그 수급이 확정된 날의 배치 시각이다 — 지금이 아니다.**
        # 재수집 시각으로 재도장하면 감쇠·TTL 이 무력해진다(NightlySource 의
        # M3 사고와 같은 지점). 배치가 며칠 멎으면 신호도 다음 날 저녁 자연
        # 만료된다 — 묵은 수급으로 계속 지목하지 않는다.
        try:
            batch_at = datetime.fromisoformat(f"{day}T17:30:00+09:00")
        except ValueError:
            return []
        top_n = int(self._cfg().get("top_n", 5))
        picks = []
        for r in rows:
            if r["foreign_"] <= 0:
                continue               # 순매도는 매수 후보의 근거가 아니다
            if is_excluded(r.get("name") or ""):
                continue
            picks.append((float(r["foreign_"]) / float(r["volume"]), r))
        picks.sort(key=lambda p: (-p[0], p[1]["code"]))
        return [
            Signal(
                code=r["code"], name=r.get("name") or r["code"], source=self.name,
                kind=self.name,
                strength=model.FLOW_STRENGTH,
                raw=round(stv, 6),
                price=float(r.get("close") or 0),
                evidence={"date": day, "frgn_stv": round(stv, 6),
                          "foreign": r["foreign_"], "volume": r["volume"]},
                observed_at=batch_at,
            )
            for stv, r in picks[:top_n]
        ]


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
