"""매물대(Volume Profile) — 가격대별 누적 거래량. 문서(2026-08-01 §3.2) 반영.

시간축 거래량 차트가 "언제 터졌나" 를 보여준다면, 매물대는 **어느 가격에서
손바뀜이 있었나** 를 보여준다. 두꺼운 가격대는 시장 참여자들의 체감 평균
단가다 — 위에서 내려오면 지지(추가 매수·저점 매수 심리), 아래에서 올라가면
저항(본전 청산 심리)으로 작동한다는 것이 문서의 주장이다.

## 관측 전용이다

지지·저항이 실제로 작동하는지는 **측정된 적이 없다.** 체결강도·수급과 같은
규약을 따른다: 원장(`profile_obs`)에 매일 쌓고, 4주 뒤 "진입가가 매물대
상단/하단 어디에 있었나" 가 실현 R 과 상관을 보이면 그때 게이트를 논한다.
규칙·점수·승격 어디에도 이 값을 넣지 않는다.

## 재료는 저장 분봉이다 — API 콜 0

감시목록 종목의 1분봉은 이미 DB 에 있다(장중 백필 + 마감 확정). 가격은
전형가(고+저+종)/3 로 대표하고 거래량을 그 칸에 쌓는다 — 봉 하나가 여러
칸에 걸치는 것을 무시하는 근사이지만, 칸 폭이 봉 범위보다 넉넉해(기본
24칸/10일 범위) 오차는 칸 경계에서만 난다.
"""
import math

import pandas as pd


def volume_profile(df: pd.DataFrame, bins: int = 24) -> dict:
    """1분봉 → 매물대. 반환: POC·밸류에어리어(70%)·상위 매물대 목록.

    값을 못 내면 `ok=False` — 0 으로 채우면 '계산했는데 0' 과 '못 쟀다' 가
    섞인다(이 저장소의 반복 규약).
    """
    if df is None or df.empty or len(df) < 30 or bins < 3:
        return {"ok": False}
    lo = float(df["low"].min())
    hi = float(df["high"].max())
    if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
        return {"ok": False}
    typical = (df["high"] + df["low"] + df["close"]) / 3
    width = (hi - lo) / bins
    idx = ((typical - lo) / width).clip(0, bins - 1).astype(int)
    vol = df["volume"].groupby(idx).sum()
    total = float(vol.sum())
    if total <= 0:
        return {"ok": False}

    centers = {i: lo + (i + 0.5) * width for i in range(bins)}
    poc_i = int(vol.idxmax())

    # 밸류에어리어 — POC 에서 시작해 이웃 중 두꺼운 쪽을 하나씩 편입, 70% 도달까지.
    # (표준 정의. 양쪽을 동시에 넓히면 얇은 쪽이 억지로 들어온다)
    va = {poc_i}
    acc = float(vol.get(poc_i, 0))
    lo_i = hi_i = poc_i
    while acc < total * 0.70:
        below = float(vol.get(lo_i - 1, 0)) if lo_i - 1 >= 0 else -1.0
        above = float(vol.get(hi_i + 1, 0)) if hi_i + 1 < bins else -1.0
        if below < 0 and above < 0:
            break
        if above >= below:
            hi_i += 1
            va.add(hi_i)
            acc += max(above, 0.0)
        else:
            lo_i -= 1
            va.add(lo_i)
            acc += max(below, 0.0)

    close = float(df["close"].iloc[-1])
    top = sorted(((centers[int(i)], float(v) / total) for i, v in vol.items()),
                 key=lambda x: -x[1])[:5]
    return {
        "ok": True,
        "poc": round(centers[poc_i], 2),                 # 최다 거래 가격대
        "va_high": round(centers[hi_i] + width / 2, 2),  # 밸류에어리어 상단
        "va_low": round(centers[lo_i] - width / 2, 2),   # 하단
        "close": round(close, 2),
        "in_va": int(centers[lo_i] - width / 2 <= close <= centers[hi_i] + width / 2),
        "above_poc": int(close >= centers[poc_i]),
        "total_volume": int(total),
        "bins": bins,
        # 상위 매물대 5칸 — (가격, 전체 대비 비중). 화면·사후 대조용 근거
        "levels": [{"price": round(p, 2), "share": round(s, 4)} for p, s in top],
    }
