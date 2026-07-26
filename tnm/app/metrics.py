"""Shadow 검증 지표 (요청서 8장) — 주별 정밀도·재현율.

- 정밀도 = 알림 판정(점수≥임계값) ∩ 사람 '중요' / 알림 판정 전체(라벨된 것 중)
- 재현율 = 알림 판정 ∩ 사람 '중요' / 사람 '중요' 전체
전환 기준: 재현율 ≥ 0.9, 정밀도 ≥ 0.6 (재현율 우선 — 놓치는 쪽이 손실이 크다)
"""
from __future__ import annotations

TARGET_RECALL = 0.9
TARGET_PRECISION = 0.6


def week_metrics(counts: dict) -> dict:
    """주별 카운트(tp/fp/fn/labeled/important) → 지표. 순수 함수 (검증 가능)."""
    tp, fp, fn = counts.get("tp", 0), counts.get("fp", 0), counts.get("fn", 0)
    precision = round(tp / (tp + fp), 3) if (tp + fp) else None
    recall = round(tp / (tp + fn), 3) if (tp + fn) else None
    return {
        **counts,
        "precision": precision,
        "recall": recall,
        "pass": (precision is not None and recall is not None
                 and recall >= TARGET_RECALL and precision >= TARGET_PRECISION),
    }


async def weekly(db_mod, weeks: int = 4) -> list[dict]:
    """주별(발행 주, KST) 라벨 기반 지표 목록 (최신 주 먼저)."""
    rows = await db_mod.label_week_counts(weeks)
    return [week_metrics(r) for r in rows]
