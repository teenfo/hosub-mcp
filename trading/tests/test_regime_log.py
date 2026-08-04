"""국면 판정 이력 — 지금 기록을 시작해야 40거래일 뒤에 판정할 수 있다.

여기서 지키는 것:
  - 야간 리포트만이 아니라 **결정론 대조군 2개를 같은 레코드에** 남기는가
    (대조군이 없으면 "맞았다"가 아무 의미도 없다)
  - 30초 루프가 하루 1,000행을 쌓지 않는가
  - **기록 실패가 매매를 막지 않는가**
  - 중립을 적중 계산에서 빼는가 ('항상 중립' 이 100%가 되면 안 된다)
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.data import regime_log

KST = ZoneInfo("Asia/Seoul")
T = datetime(2026, 7, 27, 9, 5, tzinfo=KST)


@pytest.fixture(autouse=True)
def _tmp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(regime_log, "DB_PATH", tmp_path / "regime_log.db")


def _rec(night="강세", base="중립", gap="중립", eff="강세", now=T):
    return regime_log.record(night, base, gap, eff, now=now)


# --- ① 대조군을 같은 레코드에 남긴다 ---

def test_all_four_signals_are_stored_together():
    """야간 리포트만 남기면 "맞았다"를 검증할 방법이 없다.

    같은 시각의 결정론 기준선 둘이 함께 있어야 비교가 성립한다.
    """
    _rec(night="강세", base="약세", gap="중립", eff="강세")
    r = regime_log.entries()[0]
    assert (r["night_bias"], r["base_regime"], r["gap_bias"], r["effective"]) == \
           ("강세", "약세", "중립", "강세")
    assert r["date"] == "2026-07-27"


def test_signal_list_covers_the_controls():
    assert set(regime_log.SIGNALS) == {"night_bias", "base_regime", "gap_bias",
                                       "effective"}


# --- ② 30초 루프가 하루 1,000행을 쌓지 않는다 ---

def test_unchanged_values_are_not_appended():
    assert _rec() is True
    assert _rec() is False                       # 같은 값 — 건너뛴다
    assert _rec() is False
    assert len(regime_log.entries()) == 1


def test_any_change_is_appended():
    _rec(gap="중립")
    assert _rec(gap="약세", eff="중립") is True   # 시가갭이 바뀌면 남긴다
    assert len(regime_log.entries()) == 2


def test_same_values_on_a_new_day_are_appended():
    """날짜가 바뀌면 값이 같아도 그날의 판정으로 남아야 한다."""
    _rec(now=T)
    assert _rec(now=T + timedelta(days=1)) is True
    assert {r["date"] for r in regime_log.entries()} == {"2026-07-27", "2026-07-28"}


# --- ②-b 장중 방향 관측(2026-08-04) — 판정 미사용, 전이만 append ---

def test_장중_방향_전이는_판정값이_같아도_append_된다():
    """반사실 평가에는 '전환 감지 시각'이 필요하다 — 8/4 실측(강세 판정에
    갇혀 인버스 3건 차단)의 재발 시각을 원장이 답할 수 있어야 한다."""
    assert regime_log.record("강세", "약세", "중립", "강세", now=T,
                             intraday_bias="중립", intraday_med=0.1) is True
    assert regime_log.record("강세", "약세", "중립", "강세", now=T,
                             intraday_bias="약세", intraday_med=-0.8) is True
    r = regime_log.entries()[0]
    assert (r["intraday_bias"], r["intraday_med"]) == ("약세", -0.8)


def test_장중_중앙값만_변하면_append_하지_않는다():
    """원시 %는 매 사이클 미세하게 변한다 — dedup 은 bias(전이)만 본다."""
    regime_log.record("강세", "약세", "중립", "강세", now=T,
                      intraday_bias="약세", intraday_med=-0.8)
    assert regime_log.record("강세", "약세", "중립", "강세", now=T,
                             intraday_bias="약세", intraday_med=-0.9) is False
    assert len(regime_log.entries()) == 1


def test_기존_4값_호출은_그대로_동작한다():
    """intraday 미지정(None) 호출 — 하위 호환. NULL 은 '못 쟀다'로 남는다."""
    assert _rec() is True
    r = regime_log.entries()[0]
    assert r["intraday_bias"] is None and r["intraday_med"] is None


# --- ③ 기록 실패가 매매를 막지 않는다 ---

def test_write_failure_is_swallowed(monkeypatch, tmp_path):
    """국면 이력은 연구용이다. 여기서 예외가 나가면 신호 평가가 멈춘다."""
    monkeypatch.setattr(regime_log, "DB_PATH", tmp_path / "nope" / "x.db")

    def _boom(*a, **k):
        raise OSError("디스크 없음")

    monkeypatch.setattr(regime_log.Path, "mkdir", _boom)
    assert _rec() is False          # 예외가 아니라 False


# --- ④ 하루 한 판정으로 보는 관점 ---

def test_daily_takes_the_last_value_of_each_day():
    _rec(gap="중립", eff="강세", now=T)
    _rec(gap="약세", eff="중립", now=T + timedelta(hours=2))
    _rec(night="약세", eff="약세", now=T + timedelta(days=1))
    d = regime_log.daily()
    assert [r["date"] for r in d] == ["2026-07-27", "2026-07-28"]
    assert d[0]["effective"] == "중립"       # 그날의 마지막 값
    assert d[1]["effective"] == "약세"


# --- ⑤ 적중률 — 중립을 빼지 않으면 '항상 중립' 이 만점이 된다 ---

def _rows(preds):
    """preds: [(date, night, base, gap, eff)]"""
    return [{"date": d, "night_bias": n, "base_regime": b, "gap_bias": g,
             "effective": e} for d, n, b, g, e in preds]


def test_neutral_is_excluded_from_hit_rate():
    rows = _rows([("2026-07-01", "중립", "강세", "중립", "강세"),
                  ("2026-07-02", "중립", "강세", "중립", "강세")])
    market = {"2026-07-01": 1.0, "2026-07-02": 1.0}
    out = regime_log.score(rows, market)
    assert out["night_bias"]["calls"] == 0          # 방향을 건 적이 없다
    assert out["night_bias"]["hit_rate"] is None    # 100%도 0%도 아니다
    assert out["night_bias"]["neutral"] == 2
    assert out["base_regime"]["hit_rate"] == 100.0


def test_hit_rate_counts_direction_only():
    rows = _rows([("2026-07-01", "강세", "약세", "중립", "강세"),   # 시장 +
                  ("2026-07-02", "약세", "약세", "중립", "약세")])  # 시장 −
    out = regime_log.score(rows, {"2026-07-01": 1.5, "2026-07-02": -2.0})
    assert out["night_bias"]["hit_rate"] == 100.0    # 둘 다 맞음
    assert out["base_regime"]["hit_rate"] == 50.0    # 하나만 맞음


def test_days_without_market_data_are_skipped():
    rows = _rows([("2026-07-01", "강세", "강세", "중립", "강세"),
                  ("2026-07-04", "강세", "강세", "중립", "강세")])   # 휴장
    out = regime_log.score(rows, {"2026-07-01": 1.0})
    assert out["night_bias"]["calls"] == 1


def test_small_sample_is_flagged_unreliable():
    rows = _rows([(f"2026-07-{i:02d}", "강세", "강세", "중립", "강세")
                  for i in range(1, 6)])
    market = {f"2026-07-{i:02d}": 1.0 for i in range(1, 6)}
    out = regime_log.score(rows, market)
    assert out["night_bias"]["hit_rate"] == 100.0
    assert out["night_bias"]["reliable"] is False     # 5일로는 아무 말도 못 한다
    assert regime_log.MIN_DAYS >= 40


def test_score_is_empty_safe():
    out = regime_log.score([], {})
    assert all(v["calls"] == 0 and v["hit_rate"] is None for v in out.values())


# --- ⑥ 배선 — 실제로 엔진이 남기는가 ---

def test_engine_records_on_regime_evaluation(monkeypatch, tmp_path):
    """네 값이 계산되는 유일한 지점에서 남아야 한다.

    다른 데서 부르면 야간 리포트만 있고 대조군이 빈 레코드가 생긴다.
    """
    monkeypatch.setattr(regime_log, "DB_PATH", tmp_path / "r.db")
    from app.signals.engine import SignalEngine

    e = SignalEngine()
    monkeypatch.setattr(e, "_base_regime", lambda: "약세")
    monkeypatch.setattr(e, "_gap_and_drift", lambda: ("중립", "약세", -0.8))
    monkeypatch.setattr(e, "_read_night_bias", lambda: "강세")
    assert e._effective_regime() == "강세"
    r = regime_log.entries()[0]
    assert r["night_bias"] == "강세" and r["base_regime"] == "약세"
    assert r["gap_bias"] == "중립" and r["effective"] == "강세"
    # 장중 방향 관측이 같은 레코드에 실린다 — 그러나 합성(effective)에는
    # 반영되지 않는다(약세 관측인데 유효국면 강세 그대로 = 관측 전용 증명)
    assert (r["intraday_bias"], r["intraday_med"]) == ("약세", -0.8)
