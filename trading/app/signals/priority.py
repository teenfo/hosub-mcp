"""신호 발주 우선순위 — 한 사이클에 여러 신호가 나올 때 '누가 먼저 자금을 쓰는가'.

문제: 엔진이 감시목록을 순회하며 신호를 발견하는 즉시 발주하면, 발주 순서가
종목코드 순(감시목록 dict 순서)으로 정해진다. 예수금이 넉넉한 장 초반에는
앞자리 종목 한두 개가 자금을 대부분 소진하고, 뒤에 나온 더 좋은 신호는
'잔고 부족'으로 밀린다 — 신호 품질과 무관한 구조적 편중.

해결: 한 패스에서 나온 신호를 모두 모은 뒤 이 점수로 정렬해 발주한다.
패스 자체는 어차피 감시목록을 한 바퀴 도는 동안 진행되므로, 정렬 때문에
생기는 지연은 '패스 소요 시간'이 상한이지 '감시 주기'가 아니다.

점수는 결정론 — 같은 입력이면 항상 같은 순서. LLM·난수 개입 없음.
"""

DEFAULT_PRIORITY = 0.5      # config 에 priority 가 없는 규칙의 기본 가중치
MAX_RR = 3.0                # 손익비 보너스 상한 (극단값이 순서를 지배하지 않게)


def score(rule: str, entry: float, stop: float, target: float,
          rules_cfg: dict) -> float:
    """발주 우선순위 점수(높을수록 먼저).

    = 규칙 가중치(config `rules.<name>.priority`) × 100 + 손익비(RR) × 10

    규칙 가중치가 1차 기준이다(자체 백테스트 기대값 기반 — 예: ORB +0.44R >
    momentum +0.27R). 같은 규칙끼리는 손익비가 높은 신호가 먼저 간다.
    """
    cfg = rules_cfg.get(rule)
    prio = DEFAULT_PRIORITY
    if isinstance(cfg, dict):
        try:
            prio = float(cfg.get("priority", DEFAULT_PRIORITY))
        except (TypeError, ValueError):
            prio = DEFAULT_PRIORITY
    dist = abs(entry - stop)
    rr = abs(target - entry) / dist if dist > 0 else 0.0
    return round(prio * 100 + min(rr, MAX_RR) * 10, 4)


def order(cands: list[dict], rules_cfg: dict) -> list[dict]:
    """후보 신호를 발주 순서대로 정렬한다(원본 리스트는 건드리지 않음).

    각 후보에 `priority` 키를 채워 넣어 화면·로그에서 근거를 볼 수 있게 한다.
    동점은 종목코드 → 규칙명 순으로 갈라 매 사이클 같은 순서를 보장한다.
    """
    for c in cands:
        c["priority"] = score(c.get("rule", ""), c.get("entry", 0) or 0,
                              c.get("stop", 0) or 0, c.get("target", 0) or 0,
                              rules_cfg)
    return sorted(cands, key=lambda c: (-c["priority"], c.get("symbol", ""),
                                        c.get("rule", "")))
