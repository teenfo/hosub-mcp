"""신규성 판정 (FR-05) — 순수 함수. 임계값은 config(dedup 섹션) 주입."""
from __future__ import annotations


def judge(similarity: float | None, cfg: dict) -> str:
    """동일 종목 최근 창(기본 7일) 내 최대 코사인 유사도 → novelty.

    ≥ duplicate_threshold(0.92) → 'duplicate' (LLM 생략)
    ≥ follow_up_threshold(0.85) → 'follow_up' (LLM 수행, 스코어 감점)
    그 외 → 'new'
    """
    if similarity is None:
        return "new"
    if similarity >= float(cfg.get("duplicate_threshold", 0.92)):
        return "duplicate"
    if similarity >= float(cfg.get("follow_up_threshold", 0.85)):
        return "follow_up"
    return "new"
