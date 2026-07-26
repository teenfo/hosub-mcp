"""중요도 스코어링 (요청서 6장) — 룰 기반·결정론. LLM 은 입력값만 제공한다 (P4).

score = round(100 × W_source × W_category × W_novelty × confidence)
is_material=false 면 non_material_factor(0.5) 를 곱한다.
계수는 config.yaml score 섹션 — 코드 수정 없이 조정 가능해야 한다.
각 항 값은 score_detail 로 반환해 사후 추적한다.
"""
from __future__ import annotations


def compute(source: str, analysis: dict, novelty: str, cfg: dict) -> tuple[int, dict]:
    w_source = float(cfg.get("w_source", {}).get(source, 0.7))
    category = analysis.get("category", "기타")
    w_category = float(cfg.get("w_category", {}).get(category, 0.2))
    w_novelty = float(cfg.get("w_novelty", {}).get(novelty, 1.0))
    confidence = max(0.0, min(1.0, float(analysis.get("confidence", 0.0))))
    score = round(100 * w_source * w_category * w_novelty * confidence)
    non_material = not bool(analysis.get("is_material", False))
    factor = float(cfg.get("non_material_factor", 0.5))
    if non_material:
        score = round(score * factor)
    detail = {
        "w_source": w_source, "w_category": w_category, "w_novelty": w_novelty,
        "confidence": confidence, "category": category, "novelty": novelty,
        "non_material_factor": factor if non_material else 1.0,
    }
    return int(score), detail
