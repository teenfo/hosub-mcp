"""VPIN 근사 — 독성 주문흐름의 세기. 문서(2026-08-01 §6) 반영.

VPIN(Volume-Synchronized Probability of Informed Trading)은 정보 우위 세력의
공격적 편방향 체결("독성 주문흐름")이 얼마나 쏠려 있는지를 재는 지표다.
문서의 주장: 이 값이 역사적 고점에 닿으면 시장 조성자가 유동성을 거두고
순간 급락(Flash Crash)이 임박한다 — 즉 **위험 회피** 신호다.

## 왜 '근사' 인가 — 정직하게 적는다

원 정의는 틱 전량을 거래량 버킷으로 자르고 Lee-Ready 로 체결 방향을 나눈다.
우리 인프라(REST 폴링 + 감시목록 WS)로는 전 종목 틱을 받을 수 없다. 대신
**BVC(Bulk Volume Classification)** 를 쓴다 — 체결을 개별로 나누지 않고
봉 단위 가격 변화의 표준화 값 Φ(ΔP/σ)로 매수/매도 비중을 나누는 방식이며,
원 논문(Easley·López de Prado·O'Hara)이 틱 룰보다 정확하다고 보고한 바로
그 분류법이다. **1분봉이면 계산 가능하고, 그 분봉은 이미 DB 에 있다 — 콜 0.**

버킷 경계도 근사다: 봉 하나가 버킷 경계에 걸치면 쪼개지 않고 통째로 현재
버킷에 넣는다(봉 거래량 ≪ 버킷 크기라 오차 작음).

## 관측 전용이다

절대 수준의 문턱은 없다 — 문서도 '역사적 고점 대비' 로만 말한다. 매일
`vpin_obs` 에 쌓아 분포부터 만든다. 4주 뒤 "높은 VPIN 날의 다음 날" 이
실제로 나빴는지 잰 뒤에야 필터를 논한다.
"""
import math
from statistics import NormalDist

import pandas as pd

_PHI = NormalDist().cdf


def vpin(df: pd.DataFrame, buckets: int = 50) -> dict:
    """1분봉(하루치) → VPIN. 값을 못 내면 `ok=False`.

    VPIN = mean(|매수량 − 매도량|) / 버킷크기, 버킷은 거래량 시간으로 자른다.
    0~1 — 1 에 가까울수록 체결이 한 방향으로 쏠렸다.
    """
    if df is None or df.empty or len(df) < 30 or buckets < 2:
        return {"ok": False}
    dp = df["close"].diff()
    v = df["volume"].astype(float)
    sd = float(dp.std())
    total = float(v.iloc[1:].sum())
    if not math.isfinite(sd) or sd <= 0 or total <= 0:
        return {"ok": False}      # 가격이 안 움직였거나 거래가 없다 — 성립 안 함

    bucket_v = total / buckets
    imbalances: list[float] = []
    acc = buy_acc = 0.0
    for d, vol in zip(dp.iloc[1:], v.iloc[1:]):
        frac = _PHI(float(d) / sd) if math.isfinite(d) else 0.5
        acc += vol
        buy_acc += vol * frac
        if acc >= bucket_v:
            imbalances.append(abs(2 * buy_acc - acc))    # |매수 − 매도|
            acc = buy_acc = 0.0
    if not imbalances:
        return {"ok": False}
    val = sum(imbalances) / len(imbalances) / bucket_v
    return {
        "ok": True,
        "vpin": round(min(val, 1.0), 4),
        "buckets": len(imbalances),          # 실제로 채워진 버킷 수
        "bucket_volume": int(bucket_v),
    }
