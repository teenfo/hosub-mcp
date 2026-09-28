"""청산 공식 공용화(2026-09-28) — 실전과 백테스트가 같은 공식을 쓰는가.

실전 호출부(desk.update_lines·ledger.refit_lines·max_hold_min·due_exits)의 동작
불변은 기존 test_desk·test_exit_quality 가 증명한다. 여기서는 ① 순수 함수 자체와
② 백테스트 live 청산 모델이 실전 규칙(추종·익절 상한·시간 손절·마감 정리·재검증)을
재현하는지를 본다.
"""
from collections import namedtuple

import pandas as pd

from app.backtest import runner
from app.trade import exit_policy as ep

Bar = namedtuple("Bar", "Index open high low close")
P = ep.Params(trailing=True, tighten_at=0.4, lock_gain_pct=60.0,
              trail_target=False, max_gain_pct=2.0, refit=True,
              min_stop_pct=1.0, max_stop_pct=2.5, eod_flat_at="15:20")


def _bar(hhmm, low, high, close, day="2026-09-01"):
    return Bar(pd.Timestamp(f"{day} {hhmm}:00"), close, high, low, close)


def _trade(entry=10_000.0, stop=9_800.0, target=10_300.0, at="09:30"):
    return runner.Trade("T", "orb", "long", pd.Timestamp(f"2026-09-01 {at}:00"),
                        entry, stop, target)


# --- 순수 함수 --------------------------------------------------------------
def test_손절_우선_판정():
    assert ep.line_hit("long", 9_790, 10_400, 9_800, 10_300) == "stop"
    assert ep.line_hit("long", 9_900, 10_400, 9_800, 10_300) == "target"
    assert ep.line_hit("short", 9_600, 10_210, 10_200, 9_700) == "stop"
    assert ep.line_hit("long", 9_900, 10_100, 9_800, 10_300) is None


def test_추종은_지침_예시대로_상승분을_확정한다():
    """desk docstring 예: 진입 1,000 · 목표 1,400 · 현재가 1,200 · x=90 → 1,180."""
    p = ep.Params(tighten_at=0.5, lock_gain_pct=90.0, max_gain_pct=0.0)
    stop, target = ep.trail_lines("long", 1_000, 900, 1_400, 900, 1_400, 1_200, p)
    assert (stop, target) == (1_180.0, 1_400.0)


def test_익절_상한이_목표를_끌어내린다():
    stop, target = ep.trail_lines("long", 10_000, 9_800, 10_300, 9_800, 10_300,
                                  10_000, P)
    assert target == 10_200.0          # max_gain 2% 천장


def test_손실_구간_반등은_시계를_리셋하지_않는다():
    assert ep.stop_moved_in_profit("long", 10_000, 9_800, 9_850) is False
    assert ep.stop_moved_in_profit("long", 10_000, 9_800, 10_050) is True


def test_재검증은_대역_밖만_경계로_당긴다():
    # 모델 10,000/손절 9,950(0.5%) → 체결 10,000 이면 폭 0.5% < 1.0% → 1.0% 로
    fit = ep.refit_lines("long", 10_000, 10_000, 9_950, 10_075, 1.0, 2.5)
    assert fit == (9_900.0, 10_150.0)
    assert ep.refit_lines("long", 10_000, 10_000, 9_850, 10_225, 1.0, 2.5) is None


def test_규칙별_보유시간이_전역을_덮는다():
    cfg = {"max_hold_min": 45, "orb": {"max_hold_min": 30}}
    assert ep.max_hold_min("orb", cfg) == 30
    assert ep.max_hold_min("pullback", cfg) == 45


# --- 백테스트 live 청산 한 봉 ------------------------------------------------
def test_live_는_익절_상한에_먼저_닿는다():
    """legacy 는 10,300 목표를 기다리지만 실전은 +2% 천장(10,200)에서 판다."""
    t = _trade()
    assert runner._live_step(t, _bar("09:31", 9_990, 10_050, 10_000), P, 30) is False
    assert t.target_live == 10_200.0
    assert runner._live_step(t, _bar("09:32", 10_100, 10_210, 10_150), P, 30) is True
    assert (t.exit_reason, t.exit) == ("target", 10_200.0)


def test_live_는_시간_손절을_재현한다():
    t = _trade()
    for ts in pd.date_range("2026-09-01 09:31", "2026-09-01 10:05", freq="1min"):
        if runner._live_step(t, _bar(ts.strftime("%H:%M"), 9_950, 10_020, 9_990), P, 30):
            break
    assert t.exit_reason == "timeout" and t.exit_ts.strftime("%H:%M") == "10:00"


def test_이익_구간_추종은_시계를_다시_돌린다():
    t = _trade()
    runner._live_step(t, _bar("09:31", 10_000, 10_150, 10_150), P, 30)
    assert t.stop_live > t.entry, "상승분 60% 확정 → 손절선이 진입가 위로"
    assert t.hold_since == pd.Timestamp("2026-09-01 09:31:00")


def test_live_는_마감_정리_시각에_판다():
    t = _trade(at="15:10")
    assert runner._live_step(t, _bar("15:19", 9_990, 10_010, 10_000), P, 0) is False
    assert runner._live_step(t, _bar("15:20", 9_990, 10_010, 10_005), P, 0) is True
    assert (t.exit_reason, t.exit) == ("eod", 10_005)


def test_추종_꺼지면_라인은_원본_그대로다():
    t = _trade()
    p = ep.Params(trailing=False, max_gain_pct=2.0)
    runner._live_step(t, _bar("09:31", 9_990, 10_050, 10_000), p, 30)
    assert t.stop_live is None and t.target_live is None


def test_legacy_기본값은_종전_결과를_그대로_낸다():
    """과거 스윕·리포트와의 비교 가능성 — 기본 호출은 바뀌지 않는다."""
    rows = [(f"09:{m:02d}", 101, 102, 100, 101) for m in range(15)]
    rows.append(("09:15", 100, 100.2, 98.9, 99.0))
    for m, px in [(16, 98.5), (17, 97.5), (18, 96.5), (19, 95.5), (20, 94.0)]:
        rows.append((f"09:{m:02d}", px + 0.5, px + 0.8, px, px + 0.2))
    idx = [pd.Timestamp(f"2026-07-20 {t}:00") for t, *_ in rows]
    df = pd.DataFrame([{"open": o, "high": h, "low": lo, "close": c, "volume": 1000}
                       for _, o, h, lo, c in rows], index=pd.DatetimeIndex(idx))
    cfg = {"orb": {"enabled": True, "range_start": "09:00",
                   "range_end": "09:15", "target_r": 1.5}}
    legacy = runner.run("TEST", df, cfg)
    assert [t.exit_reason for t in legacy.trades] == ["target"]
    live = runner.run("TEST", df, cfg, exit_model="live",
                      exit_params=ep.Params(trailing=False, max_gain_pct=0.0,
                                            refit=False))
    # 추종·상한·재검증을 끈 live 는 legacy 와 같은 청산이어야 한다(공식의 일관성)
    assert [(t.exit_reason, t.exit) for t in live.trades] == \
        [(t.exit_reason, t.exit) for t in legacy.trades]
