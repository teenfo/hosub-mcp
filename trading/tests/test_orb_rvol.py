"""ORB 거래량 확인(RVOL) — 돌파봉 거래량 / **개장 범위 봉 평균**.

실측 2026-07-31 (5거래일 · 894건, 비용 0.33% 차감, 목표 1.5R):
    필터 없음      건당 -0.251%  승률 41.7%
    RVOL >= 1.5   건당 +0.194%  승률 49.0%   <- 유일한 양수 변형
승률 +7.3%p 는 문헌이 보고한 개선폭(8~12%p)과 같은 자릿수다.

같은 측정에서 범위 상한 2.5% 는 3.5% 보다 나빴다(-0.203 vs -0.033) — 앞서 실거래
11건으로 내렸던 값을 286건이 뒤집어 되돌렸다.
"""
import pandas as pd

from app.signals import rules

CFG = {"range_start": "09:00", "range_end": "09:15", "target_r": 1.5}


def _win(vol):
    """09:00~09:14 범위 100~102, 봉당 거래량 vol."""
    idx = pd.date_range(pd.Timestamp("2026-07-20 09:00:00"), periods=15, freq="1min")
    return pd.DataFrame(
        [{"open": 101, "high": 102, "low": 100, "close": 101, "volume": vol}] * 15,
        index=idx)


def _break(vol):
    """09:15 상단 돌파봉."""
    return pd.DataFrame(
        [{"open": 102, "high": 103.2, "low": 101.9, "close": 103.0, "volume": vol}],
        index=pd.DatetimeIndex([pd.Timestamp("2026-07-20 09:15:00")]))


def test_거래량이_충분하면_통과():
    df = pd.concat([_win(1000), _break(1600)])          # RVOL 1.6
    assert rules.orb(df, {**CFG, "min_rvol": 1.5}) is not None


def test_거래량이_모자라면_차단():
    df = pd.concat([_win(1000), _break(1400)])          # RVOL 1.4
    assert rules.orb(df, {**CFG, "min_rvol": 1.5}) is None


def test_경계값은_통과():
    df = pd.concat([_win(1000), _break(1500)])          # RVOL 정확히 1.5
    assert rules.orb(df, {**CFG, "min_rvol": 1.5}) is not None


def test_설정이_0_이면_종전_동작():
    df = pd.concat([_win(1000), _break(1)])
    assert rules.orb(df, {**CFG, "min_rvol": 0}) is not None
    assert rules.orb(df, CFG) is not None               # 키 자체가 없어도 같다


def test_분모는_세션_평균이_아니라_개장_범위_평균이다():
    """`vol_ratio` 는 세션 누적 평균이라 장이 갈수록 분모가 커져 오후 돌파에
    관대해진다. RVOL 은 '그 종목의 개장 활동 대비 지금 몇 배' 라 시각과 무관하다.

    개장 범위 거래량이 작고 그 뒤로 거래가 폭증한 종목 — 세션 평균 기준이면
    돌파봉이 초라해 보이지만, 개장 대비로는 2배다.
    """
    mid = pd.DataFrame(
        [{"open": 101, "high": 101.5, "low": 100.5, "close": 101, "volume": 50_000}] * 5,
        index=pd.date_range(pd.Timestamp("2026-07-20 09:15:00"), periods=5, freq="1min"))
    df = pd.concat([_win(1000), mid, _break(2000)])     # 개장 대비 2.0 / 세션 대비 0.1
    assert rules.orb(df, {**CFG, "min_rvol": 1.5}) is not None
    assert rules.orb(df, {**CFG, "vol_ratio": 1.5}) is None


def test_개장_범위_거래량이_0_이면_차단():
    """분모가 없으면 통과시키지 않는다 — 0으로 나누는 대신 보수적인 쪽."""
    df = pd.concat([_win(0), _break(9999)])
    assert rules.orb(df, {**CFG, "min_rvol": 1.5}) is None


def test_범위_상한은_3_5_로_되돌아왔다():
    """실거래 11건으로 내린 값을 전 유니버스 286건이 뒤집었다."""
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load(Path(__file__).resolve().parents[1].joinpath("config.yaml")
                         .read_text(encoding="utf-8"))
    orb = cfg["rules"]["orb"]
    assert orb["max_range_pct"] == 3.5
    assert orb["min_rvol"] == 1.5
