"""수집한 문서를 분석 파이프라인에 태우는 공용 경로.

## 왜 공용으로 뺐는가

지금 소스가 넷이다 — DART 공시 · 구글 RSS 뉴스 · 네이버 뉴스 · 증권사 리포트.
앞으로도 늘어난다. **스크래핑 경로만 새로 생기고 그 뒤는 전부 같다**:

    수집 → (종목 귀속) → raw_items → 임베딩 → 중복제거 → 분류(LLM) → 점수
                                                         → 알림 · 감시목록 편입

새 소스를 붙일 때 다시 짜야 하는 것은 **파서뿐**이고, 그 뒤는 이 모듈을 부르면
된다. 소스마다 적재 규칙이 조금씩 다르게 구현되면 "왜 이 소스만 분석이 안 되지"
같은 문제가 소스 수만큼 생긴다.

## 종목 귀속이 이 경로의 핵심 관문이다

`raw_items.watchlist_id` 는 NOT NULL 이다 — 모든 분석 항목은 종목에 매인다.
그래서 관심종목 밖 종목의 문서는 **그 종목을 먼저 등록해야** 분석이 된다.
DART 전종목 모드가 이미 같은 문제를 풀었고(discover.py), 여기서는 그 판단을
소스와 무관하게 쓸 수 있도록 일반화했다.

등록에 상한을 두는 이유는 DART 때와 같다. 등록할수록 그 종목에 뉴스 폴링이
붙어 호출이 선형으로 늘어난다. 상한에 걸린 문서는 버리지 않고 **호출자가
재시도할 수 있도록 남긴다** — 다음 사이클에 자리가 나면 그때 들어간다.
"""
from __future__ import annotations

import logging

from . import db

log = logging.getLogger("tnm.ingest")

DEFAULTS = {
    "enabled": True,
    "max_per_cycle": 15,   # 한 사이클에 새로 등록할 종목 수
    "max_total": 250,      # 이 출처로 등록된 관심종목 총량
    "tier": "other",       # 12시간 주기. 'trade'(30분)로 넣으면 호출이 폭증한다
}


def _cfg(cfg: dict | None, key: str):
    return (cfg or {}).get(key, DEFAULTS[key])


async def attach_docs(items: list[dict], source: str, *, origin: str,
                      cfg: dict | None = None) -> dict:
    """문서를 raw_items 에 적재한다. 종목이 관심종목에 없으면 등록한다.

    items: `[{ticker, name, row}]` — row 는 raw_items 행
        (`source_uid`·`title`·`body`·`url`·`published_at`·`content_hash`·`norm_body`).
        `source_uid` 는 소스 안에서 고유해야 한다(테이블 제약: (source, source_uid)).
    source: raw_items.source 값. 점수 산식의 `w_source` 키이기도 하다.
    origin: 이 소스가 종목을 등록할 때 쓸 tnm_watchlist.origin.
        출처별 상한·성적을 따로 재려면 소스마다 달라야 한다.

    반환 카운트에서 `deferred` 는 **버린 것이 아니라 미룬 것**이다 — 상한이나
    사용자 제외 때문에 지금은 종목이 없어서 적재하지 못한 문서 수이고,
    호출자가 다음 사이클에 다시 넣어야 한다.
    """
    counts = {"docs": len(items), "stocks": 0, "inserted": 0,
              "registered": 0, "deferred": 0, "errors": 0}
    if not items or not _cfg(cfg, "enabled"):
        return counts | ({"skipped": "비활성"} if items else {})

    wids = await db.watch_ids_by_ticker()
    wanted: dict[str, str] = {}       # ticker → name (등록 후보)
    for it in items:
        t = str(it.get("ticker") or "")
        if t and t not in wids:
            wanted.setdefault(t, str(it.get("name") or t))

    if wanted:
        # 이미 등록된 적이 있는 ticker 는 건드리지 않는다. 사용자가 제외한
        # 종목을 소스가 되살리면 그 결정을 덮는 것이다(add_discovered 규약).
        known = await db.known_tickers()
        fresh = [(t, n) for t, n in wanted.items() if t not in known]
        room = int(_cfg(cfg, "max_total")) - await db.count_origin(origin)
        take = fresh[:max(0, min(int(_cfg(cfg, "max_per_cycle")), room))]
        if take:
            counts["registered"] = await db.add_discovered(
                [{"ticker": t, "name": n} for t, n in take],
                tier=str(_cfg(cfg, "tier")), origin=origin)
            log.info("%s 신규 종목 등록: %d건 (%s)", source, counts["registered"],
                     ", ".join(f"{t} {n}" for t, n in take[:5]))
            wids = await db.watch_ids_by_ticker()
        elif fresh:
            log.info("%s 종목 등록 상한 — %d건 보류", source, len(fresh))

    by_wid: dict[int, list[dict]] = {}
    for it in items:
        wid = wids.get(str(it.get("ticker") or ""))
        if wid is None:
            counts["deferred"] += 1
            continue
        by_wid.setdefault(wid, []).append(it["row"])
    counts["stocks"] = len(by_wid)
    for wid, rows in by_wid.items():
        try:
            counts["inserted"] += await db.insert_raw_items(wid, source, rows)
        except Exception as e:  # noqa: BLE001 — 종목 단위 격리
            counts["errors"] += 1
            log.warning("%s 적재 실패 watchlist_id=%s: %s", source, wid, e)
    return counts


def doc_body(title: str, parts: list[str]) -> str:
    """분류 LLM 이 읽을 본문을 만든다.

    별도 칼럼에만 있는 값(목표주가·투자의견 등)은 분류·점수에 반영되지 않는다 —
    LLM 이 보는 것은 본문 텍스트뿐이다. 소스별로 중요한 값을 문장으로 옮겨
    넣어야 한다.
    """
    return " / ".join([title, *[p for p in parts if p]])
