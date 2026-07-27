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
import logging

log = logging.getLogger(__name__)

# config 에 priority 가 없는 규칙의 기본값. **튜닝된 어떤 규칙보다 낮게** 둔다.
# 종전 0.5 는 gap·pullback 과 같은 값이라, priority 를 빠뜨린 규칙이 조용히
# 튜닝된 규칙과 동률이 됐다. 실측 2026-07-27: 신호 17건이 전부 65.0 으로 묶여
# 동점 처리(종목코드 오름차순)로 넘어갔다 — 이 모듈이 없애려던 바로 그 편중이다.
DEFAULT_PRIORITY = 0.1
MAX_RR = 3.0                # 손익비 보너스 상한 (극단값이 순서를 지배하지 않게)

_warned: set[str] = set()   # priority 누락 경고는 규칙당 1회만


def score(rule: str, entry: float, stop: float, target: float,
          rules_cfg: dict) -> float:
    """발주 우선순위 점수(높을수록 먼저).

    = 규칙 가중치(config `rules.<name>.priority`) × 100 + 손익비(RR) × 10

    규칙 가중치가 1차 기준이다(주간 스윕 avg_r 기반 — 실측 2026-07-25:
    orb +0.53R > momentum +0.25R > gap +0.04R > vwap_reclaim −0.43R >
    rsi_dip −0.66R > range_break_retest −1.63R).

    **RR 항은 현재 순서에 기여하지 않는다.** 모든 규칙이 `target_r: 1.5` 라
    RR 이 상수이기 때문이다. 규칙마다 다른 target_r 을 쓰기 시작하면 그때
    같은 규칙 안에서 손익비가 높은 신호를 앞세우는 본래 역할을 한다.
    """
    cfg = rules_cfg.get(rule)
    prio = DEFAULT_PRIORITY
    if isinstance(cfg, dict) and "priority" in cfg:
        try:
            prio = float(cfg["priority"])
        except (TypeError, ValueError):
            prio = DEFAULT_PRIORITY
    elif rule and rule not in _warned:
        # 조용히 기본값으로 떨어지면 튜닝된 규칙과 뒤섞인다 — 한 번은 알린다
        _warned.add(rule)
        log.warning("규칙 '%s' 에 priority 가 없어 기본값 %.2f 사용 — "
                    "config.yaml rules.%s.priority 를 지정하세요",
                    rule, DEFAULT_PRIORITY, rule)
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
