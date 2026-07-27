"""일봉 → 전 구간 피처 패널 (벡터화).

features.compute_features 는 '마지막 날 1행'을 낸다. 이벤트 스터디는 과거 전
구간이 필요한데, 날짜마다 그 함수를 부르면 3,941종목 × 190일 = 75만 호출이라
현실적이지 않다. 여기서는 같은 정의를 rolling 연산으로 한 번에 계산한다.

두 경로가 갈라지면 연구 결과가 실제 발굴과 달라진다. tests/test_event_study.py
가 '마지막 행이 compute_features 와 일치하는가'를 못박아 그 갈라짐을 막는다.
"""
import pandas as pd

from ..signals.indicators import atr, rsi

# 이벤트 스터디에서 순위상관(IC)을 볼 연속 피처. 점수(score)만으로는 같은 점수
# 안에서 줄을 세울 수 없으므로, 어떤 연속값이 실제로 예측력이 있는지 함께 본다.
IC_FEATURES = ("score", "vol_ratio20", "near_high60_pct", "rsi14",
               "atr_pct", "ret_20d", "disparity20")
MIN_ROWS = 60      # compute_features 와 동일 — 60행 미만이면 피처가 서지 않는다


def panel(df: pd.DataFrame, cfg: dict | None = None) -> pd.DataFrame:
    """일봉(오름차순, index=ts) → 날짜별 피처 패널.

    각 행은 **그날 종가까지의 정보만** 쓴다(미래 참조 없음). 익일 수익률은
    forward() 가 따로 붙인다.
    """
    cfg = cfg or {}
    if len(df) < MIN_ROWS:
        return pd.DataFrame()
    c, v, h, lo = df["close"], df["volume"], df["high"], df["low"]

    # avg20 은 '오늘을 뺀' 직전 20일 평균 — compute_features 의 v.iloc[-21:-1]
    avg20 = v.shift(1).rolling(20).mean()
    high60 = h.rolling(60).max()          # 오늘 포함 (원본과 동일)
    ma5 = c.rolling(5).mean()
    ma20 = c.rolling(20).mean()
    ma60 = c.rolling(60).mean()
    aligned = (ma5 > ma20) & (ma20 > ma60)
    # '최근 5일 내 신규 형성' = 오늘 정배열인데 직전 5일이 전부 정배열은 아니었다
    prev5_all = aligned.astype(float).shift(1).rolling(5).min() == 1.0
    aligned_new = aligned & ~prev5_all.fillna(False)

    vol_surge = (avg20 > 0) & (v >= cfg.get("vol_surge_ratio", 3.0) * avg20)
    near_high = (high60 > 0) & (c >= high60 * cfg.get("near_high_ratio", 0.97))

    out = pd.DataFrame(index=df.index)
    out["close"] = c
    out["open"] = df["open"]
    # 고가·저가는 forward() 의 '변동폭' 목적어 재료다. 종전에는 버려서
    # 익일 종가 방향밖에 잴 수 없었다 — 발굴이 먹이는 건 장중 규칙인데.
    out["high"] = h
    out["low"] = lo
    out["vol_surge"] = vol_surge.astype(int)
    out["near_high"] = near_high.astype(int)
    out["ma_aligned_new"] = aligned_new.astype(int)
    out["score"] = out[["vol_surge", "near_high", "ma_aligned_new"]].sum(axis=1)
    out["liquid"] = (
        (c >= cfg.get("min_price", 1_000))
        & (c * v >= cfg.get("min_trade_value_krw", 1_000_000_000))
    ).astype(int)
    # 연속 피처 — 같은 점수 안의 순위를 가를 후보들
    out["vol_ratio20"] = (v / avg20).where(avg20 > 0, 0.0)
    out["near_high60_pct"] = (c / high60 * 100).where(high60 > 0, 0.0)
    out["rsi14"] = rsi(c, 14)
    out["atr_pct"] = (atr(df, 14) / c * 100).where(c > 0, 0.0)
    out["ret_1d"] = (c / c.shift(1) - 1) * 100      # 국면 판정용 시장 수익률 재료
    out["ret_20d"] = (c / c.shift(20) - 1) * 100
    out["disparity20"] = (c / ma20 * 100).where(ma20 > 0, 0.0)
    # 피처가 서지 않는 앞 구간은 버린다(60행 규칙)
    return out.iloc[MIN_ROWS - 1:].dropna()


# 이벤트 스터디가 재는 '목적어'. 발굴이 무엇을 공급해야 하는가에 따라 답이 달라진다.
TARGETS = {
    "fwd_1": "익일 방향 (시가→종가)",
    "fwd_up_1": "익일 최대 상승 (시가→고가)",
    "fwd_range_1": "익일 변동폭 (고가−저가)",
}


def forward(p: pd.DataFrame, horizons=(1, 3, 5)) -> pd.DataFrame:
    """익일 이후 결과를 붙인다 — **실제로 잡을 수 있는 시점 기준**.

    발굴 배치는 t일 장 마감 후(17:30)에 돌아 t+1일 후보를 낸다. 따라서 진입은
    빨라야 t+1 시가다. t 종가 → t+1 시가의 갭은 배치가 끝난 뒤에 열리므로
    잡을 수 없다 — gap_pct 로 따로 재서 '엣지가 못 먹는 구간에 있었는지' 를
    드러낸다.

    목적어가 셋인 이유: 발굴이 먹이는 것은 orb(09:00~09:15 범위 돌파)·momentum
    같은 **장중** 규칙이다. 그 규칙들이 필요로 하는 건 익일 종가가 어느 쪽으로
    끝나는가가 아니라 **체결 가능한 R 을 낼 만큼 움직이는가**다. 방향만 재면
    잘못된 것을 최적화하게 된다.

      fwd_k       : t+1 시가 → t+k 종가 (%)      ← 방향
      fwd_up_1    : t+1 시가 → t+1 고가 (%)      ← 롱 진입이 잡을 수 있었던 상한
      fwd_range_1 : (t+1 고가 − t+1 저가) / 시가 ← 변동폭
      gap_pct     : t 종가 → t+1 시가 (%)        ← 실현 불가(참고)

    진입가는 셋 다 t+1 시가로 같다 — 그래야 서로 비교된다.
    """
    nxt_open = p["open"].shift(-1)
    out = p.copy()
    out["gap_pct"] = (nxt_open / p["close"] - 1) * 100
    for k in horizons:
        out[f"fwd_{k}"] = (p["close"].shift(-k) / nxt_open - 1) * 100
    if "high" in p.columns and "low" in p.columns:
        nxt_high, nxt_low = p["high"].shift(-1), p["low"].shift(-1)
        out["fwd_up_1"] = (nxt_high / nxt_open - 1) * 100
        out["fwd_range_1"] = (nxt_high - nxt_low) / nxt_open * 100
    return out
