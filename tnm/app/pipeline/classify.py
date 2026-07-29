"""LLM 분류 (요청서 5장) — 프롬프트·스키마 검증·환각 검사. 순수 함수만.

LLM 은 분류·요약·서술만 한다 (P1). 매매 의견·목표주가·원문에 없는 수치는
시스템 프롬프트로 금지하고, reason 의 수치는 원문 대조로 사후 검사한다.
"""
from __future__ import annotations

import json
import re

from ..ollama import SchemaError

# 거래정지·정정공시는 config 의 alerts.urgent_categories 가 가리키는 값이다.
# 여기에 없으면 분류기가 그 값을 낼 수 없어 긴급 판정(notify/rules.py:32)이
# **항상 False** 가 되고, 해당 공시는 '기타'(w_category 0.2)로 떨어져 점수
# ≈18~20 → 임계 60 미달 → 알림도 편입도 되지 않는다.
# 보유 중 거래정지는 청산 자체가 불가능해지는 사건이라 사각으로 둘 수 없다.
CATEGORIES = ("실적", "공급계약", "증설투자", "지배구조", "자금조달",
              "소송규제", "인사", "거래정지", "정정공시",
              "시황해설", "단순재탕", "기타")
DIRECTIONS = ("positive", "negative", "neutral", "unclear")
# 'unclear' 를 넣는다. 실측(2026-07-29) 분류 실패 원인 1위가 `impact_horizon
# 위반: 'unclear'` 247건이었다 — impact_direction 에는 '모르겠다' 가 있는데
# horizon 에는 없어서, 모델이 같은 말을 쓰면 항목이 통째로 버려졌다.
# 게다가 impact_horizon 은 1년치 소급 측정에서 **1일 초과수익이 0과 구분되는
# 버킷이 0/3**(t=0.89/1.39/−0.96)으로 예측력이 확인되지 않은 필드다. 예측력이
# 없는 칸의 enum 이 좁아서 분석 자체를 버리는 것은 손해만 있다.
HORIZONS = ("immediate", "short", "long", "unclear")

# 모델이 실제로 내놓은 값 → 정본. 프롬프트에 "그대로 쓰라"고 적어도 계속
# 벗어나므로(실측 175건/7일) 결정론적으로 흡수한다.
#
# 애매한 것은 전부 '기타'(w_category 0.2)로 보낸다. '기술개발'을 '증설투자'
# (1.0)로 올리면 일반 산업 기사가 공급계약과 같은 무게를 갖는다 — 과대평가는
# 누락보다 비싸다. 프롬프트도 "애매하면 기타"라고 이미 지시하고 있고, 여기서
# 하는 일은 그 지시를 강제하는 것뿐이다.
CATEGORY_ALIASES = {
    "규제": "소송규제", "소송": "소송규제", "제재": "소송규제",
    "상장폐지": "거래정지", "매매거래정지": "거래정지",
    "기재정정": "정정공시", "정정": "정정공시",
    "실적발표": "실적", "잠정실적": "실적",
    "수주": "공급계약", "계약": "공급계약",
    "증자": "자금조달", "유상증자": "자금조달", "자기주식취득": "자금조달",
    "임원변경": "인사", "인사이동": "인사",
    # 아래는 정본에 대응이 없다 — 보수적으로 기타
    "기술개발": "기타", "연구개발": "기타", "기술혁신": "기타",
    "신제품": "기타", "신제품출시": "기타", "기술·서비스": "기타",
    "투자": "기타", "사업확장": "기타", "ESG": "기타", "esg": "기타",
}
DIRECTION_ALIASES = {"mixed": "unclear", "혼재": "unclear", "unknown": "unclear",
                     "positive/negative": "unclear"}
HORIZON_ALIASES = {"medium": "short", "mid": "short", "unknown": "unclear",
                   "n/a": "unclear", "none": "unclear"}


def _coerce(value, allowed: tuple[str, ...], aliases: dict[str, str]) -> str | None:
    """모델 출력 → 정본 값. 못 맞추면 None (호출자가 SchemaError 를 낸다).

    모델이 'A | B' 처럼 둘을 내놓는 경우가 있다(실측 11건). 첫 번째가 주된
    판단이므로 앞에서부터 훑어 처음 맞는 것을 쓴다.
    """
    if not isinstance(value, str):
        return None
    for tok in (t.strip() for t in re.split(r"[|/,]", value)):
        if tok in allowed:
            return tok
        mapped = aliases.get(tok) or aliases.get(tok.lower())
        if mapped:
            return mapped
    return None

SYSTEM_PROMPT = (
    "너는 금융 뉴스·공시를 분류하는 분석 보조다.\n"
    "금지사항: 매수/매도/보유 의견 생성 금지, 목표주가 산출 금지, "
    "원문에 없는 수치 생성 금지.\n"
    f"category 는 반드시 다음 {len(CATEGORIES)}개 중 하나를 '그대로' 써야 한다 — "
    "새 카테고리를 만들거나 변형하지 마라. 애매하면 '기타' 를 쓴다:\n"
    f"  {', '.join(CATEGORIES)}\n"
    "  (예: 신기술·특허·R&D → 증설투자 또는 기타, 주가·수급 해설 → 시황해설,\n"
    "   이미 보도된 내용의 재보도 → 단순재탕,\n"
    "   거래정지·매매거래정지·상장폐지 관련 → 거래정지,\n"
    "   기공시 내용의 정정·기재정정·[정정] 표기 → 정정공시)\n"
    "출력: 아래 스키마의 JSON 객체만 출력한다. 설명 문장이나 마크다운 코드펜스 금지.\n"
    "{\n"
    f'  "category": "{" | ".join(CATEGORIES)}",\n'
    '  "is_material": true|false,\n'
    f'  "impact_direction": "{" | ".join(DIRECTIONS)}",\n'
    f'  "impact_horizon": "{" | ".join(HORIZONS)}",\n'
    '  "confidence": 0.0~1.0,\n'
    '  "reason": "원문 근거를 인용한 한 문장",\n'
    '  "summary": "2~3문장 요약",\n'
    '  "contains_numbers": true|false\n'
    "}"
)


def build_user_msg(stock_name: str, source: str, title: str,
                   published_at: str, body: str, max_chars: int = 6000) -> str:
    src_label = {"dart": "공시(DART)", "naver": "뉴스(네이버)", "rss": "뉴스(RSS)"}.get(source, source)
    return (f"종목명: {stock_name}\n소스: {src_label}\n제목: {title}\n"
            f"발행일시: {published_at}\n본문:\n{(body or '')[:max_chars]}")


def extract_json(content: str) -> dict:
    """응답에서 JSON 객체 추출 — 코드펜스·잡설이 섞여도 관대하게 파싱."""
    s = (content or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", s)
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        raise SchemaError("JSON 객체 없음")
    try:
        payload = json.loads(s[start:end + 1])
    except ValueError as e:
        raise SchemaError(f"JSON 파싱 실패: {e}") from e
    if not isinstance(payload, dict):
        raise SchemaError("JSON 객체가 아님")
    return payload


def validate(payload: dict) -> dict:
    """필수 키·enum·타입 검증 (요청서 5.4). 위반 시 SchemaError → 재시도."""
    out: dict = {}
    cat = _coerce(payload.get("category"), CATEGORIES, CATEGORY_ALIASES)
    if cat is None:
        # 별칭에도 없는 값은 그대로 실패시킨다 — 재시도 힌트가 붙고, 계속
        # 나오면 tnm_llm_calls 에 남아 별칭에 추가할 근거가 된다.
        raise SchemaError(f"category 위반: {payload.get('category')!r}")
    out["category"] = cat
    im = payload.get("is_material")
    if not isinstance(im, bool):
        raise SchemaError(f"is_material 위반: {im!r}")
    out["is_material"] = im
    d = _coerce(payload.get("impact_direction"), DIRECTIONS, DIRECTION_ALIASES)
    if d is None:
        raise SchemaError(f"impact_direction 위반: {payload.get('impact_direction')!r}")
    out["impact_direction"] = d
    h = _coerce(payload.get("impact_horizon"), HORIZONS, HORIZON_ALIASES)
    if h is None:
        raise SchemaError(f"impact_horizon 위반: {payload.get('impact_horizon')!r}")
    out["impact_horizon"] = h
    try:
        conf = float(payload.get("confidence"))
    except (TypeError, ValueError) as e:
        raise SchemaError(f"confidence 위반: {payload.get('confidence')!r}") from e
    if not 0.0 <= conf <= 1.0:
        raise SchemaError(f"confidence 범위 위반: {conf}")
    out["confidence"] = round(conf, 2)
    for key in ("reason", "summary"):
        v = payload.get(key)
        if not isinstance(v, str) or not v.strip():
            raise SchemaError(f"{key} 누락")
        out[key] = v.strip()
    out["contains_numbers"] = bool(payload.get("contains_numbers", False))
    return out


_NUM_RE = re.compile(r"\d[\d,\.]*")


def _numbers(text: str) -> set[str]:
    out = set()
    for tok in _NUM_RE.findall(text or ""):
        t = tok.replace(",", "").rstrip(".")
        if t:
            out.add(t)
    return out


def check_hallucination(reason: str, source_text: str) -> bool:
    """reason 에 원문에 없는 수치가 있으면 True (경고 플래그 — 요청서 5.4)."""
    return bool(_numbers(reason) - _numbers(source_text))


def retry_hint(user_msg: str, error: str) -> str:
    """재시도용 사용자 메시지 — 직전 위반을 알려 같은 실수를 반복하지 않게 한다."""
    return (f"{user_msg}\n\n[이전 응답 오류] {error}\n"
            f"category 는 반드시 다음 중 하나여야 한다: {', '.join(CATEGORIES)}. "
            "다른 키도 스키마를 정확히 지켜 JSON 객체만 출력하라.")
