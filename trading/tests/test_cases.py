"""매매 케이스 원장 — 청산 거래를 사후 측정치와 함께 남긴다.

이번 세션의 측정(MFE/MAE · 진입 위치 · 목표 도달)이 전부 일회성 스크립트로
사라졌던 것을 원장으로 만든다. **사후 사실만** 담고 주장을 하지 않는다.
"""
import json

import pytest

from app import settings
from app.research import cases


def _bars(prices):
    """(ts, high, low, close) 리스트 — high/low 는 close 중심 ±0."""
    return [(f"2026-08-03T09:{i:02d}:00+09:00", p, p, p)
            for i, p in enumerate(prices)]


POS = {"id": "x1", "symbol": "005930", "name": "삼성전자", "rule": "orb",
       "side": "long", "entry": 1000.0, "stop": 980.0, "target": 1030.0,
       "opened": "2026-08-03T09:00:00+09:00",
       "closed": "2026-08-03T09:20:00+09:00", "exit_reason": "stop"}


# --- 순수 계산 ---------------------------------------------------------------

def test_MFE_는_양수_MAE_는_음수():
    """절댓값으로 뭉개면 '한 번도 안 밀린 건' 과 '1% 밀린 건' 이 같아 보인다."""
    e = cases.excursions(POS, _bars([1000, 1015, 990, 1005]))
    assert e["mfe_pct"] == 1.5
    assert e["mae_pct"] == -1.0


def test_숏은_방향이_뒤집힌다():
    short = {**POS, "side": "short", "stop": 1020.0, "target": 970.0}
    e = cases.excursions(short, _bars([1000, 985, 1010]))
    assert e["mfe_pct"] == 1.5      # 내려간 것이 유리
    assert e["mae_pct"] == -1.0


def test_목표와_손절_대비_비율():
    e = cases.excursions(POS, _bars([1000, 1015, 975]))
    assert e["target_pct"] == 3.0 and e["stop_pct"] == 2.0
    assert e["mfe_over_target"] == pytest.approx(0.5)     # 1.5 / 3.0
    assert e["mae_over_stop"] == pytest.approx(1.25)      # 2.5 / 2.0 → 관통


def test_진입_위치는_직전_30분_범위_기준():
    pre = _bars([900] * 5 + [1100] * 5)                   # 저 900 · 고 1100
    e = cases.excursions(POS, _bars([1000, 1010]), pre)
    assert e["entry_pos"] == pytest.approx(0.5)


def test_직전_봉이_모자라면_위치를_내지_않는다():
    e = cases.excursions(POS, _bars([1000, 1010]), _bars([900, 1100]))
    assert "entry_pos" not in e


def test_분봉이_없으면_측정치가_없다():
    assert cases.excursions(POS, []) == {"bars": 0}


# --- 라벨 --------------------------------------------------------------------

def test_라벨은_해석이_아니라_사실이다():
    c = {"mfe_pct": -0.5, "mfe_3min": -0.5, "mae_over_stop": 1.4,
         "mfe_over_target": -0.2, "exit_reason": "stop", "hour": 10}
    got = cases.lessons_for(c)
    assert "한 번도 진입가 위로 못 갔다" in got
    assert "진입 후 3분 안에 유리하게 못 갔다" in got
    assert "손절선을 관통했다(불리 이동 > 손절폭)" in got


def test_목표에_닿고도_목표청산이_아니면_라벨():
    """실측 5거래일: 목표선에 닿은 25건 중 목표청산은 20건이었다."""
    c = {"mfe_over_target": 1.2, "exit_reason": "timeout"}
    assert "목표에 닿았는데 목표로 청산되지 않았다" in cases.lessons_for(c)
    c2 = {"mfe_over_target": 1.2, "exit_reason": "target"}
    assert not any("목표로 청산되지 않았다" in x for x in cases.lessons_for(c2))


def test_비용을_못_넘은_유리_이동():
    assert any("왕복 비용" in x
               for x in cases.lessons_for({"mfe_pct": 0.2}))


def test_손절폭_대역_이탈은_설정을_읽는다(monkeypatch):
    monkeypatch.setitem(settings.RULES, "min_stop_pct", 1.0)
    monkeypatch.setitem(settings.RULES, "max_stop_pct", 2.5)
    assert any("대역" in x for x in cases.lessons_for({"stop_pct": 3.1}))
    assert not any("대역" in x for x in cases.lessons_for({"stop_pct": 1.8}))


def test_상단_20퍼센트_매수():
    assert any("상단 20%" in x for x in cases.lessons_for({"entry_pos": 0.92}))
    assert not any("상단 20%" in x for x in cases.lessons_for({"entry_pos": 0.4}))


# --- 적재·조회 ---------------------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(cases, "DB_PATH", tmp_path / "backtest.db")


def _case(pos_id, day, rule="orb", hour=9, pnl=-1.0, lessons=None):
    return {"pos_id": pos_id, "d": day, "symbol": "005930", "name": "삼성전자",
            "rule": rule, "hour": hour, "pnl_pct": pnl, "bars": 20,
            "mfe_pct": 1.0, "mae_pct": -1.5, "mfe_over_target": 0.4,
            "mae_over_stop": 0.9, "entry_pos": 0.7, "mfe_3min": 0.2,
            "exit_reason": "stop", "lessons": lessons or ["테스트 라벨"]}


def test_적재는_포지션_id_기준_멱등(db):
    assert cases.save([_case("a", "2026-08-03")]) == 1
    assert cases.save([_case("a", "2026-08-03", pnl=-2.0)]) == 1
    rows = cases.similar("orb", 9)
    assert len(rows) == 1 and rows[0]["pnl_pct"] == -2.0   # 대사 후 갱신된다


def test_선례는_오늘을_제외한다(db):
    """오늘 거래를 오늘의 선례로 인용하면 같은 사실을 두 번 세는 것이다."""
    cases.save([_case("a", "2026-08-03"), _case("b", "2026-08-04")])
    rows = cases.similar("orb", 9, exclude_day="2026-08-04")
    assert [r["pos_id"] for r in rows] == ["a"]


def test_선례는_같은_규칙_인접_시간대만(db):
    cases.save([_case("a", "2026-08-03", rule="orb", hour=9),
                _case("b", "2026-08-03", rule="orb", hour=14),
                _case("c", "2026-08-03", rule="pullback", hour=9)])
    assert [r["pos_id"] for r in cases.similar("orb", 9)] == ["a"]


def test_라벨은_JSON_으로_돌아온다(db):
    cases.save([_case("a", "2026-08-03", lessons=["A", "B"])])
    assert cases.similar("orb", 9)[0]["lessons"] == ["A", "B"]


def test_누적_요약(db):
    cases.save([
        _case("a", "2026-08-03"),
        {**_case("b", "2026-08-03"), "mfe_pct": -0.5},          # 못 오른 건
        {**_case("c", "2026-08-03"), "mfe_over_target": 1.2,
         "exit_reason": "timeout"},                             # 닿았는데 미청산
        {**_case("d", "2026-08-03"), "mae_over_stop": 1.4},     # 관통
    ])
    s = cases.stats()
    assert s["n"] == 4
    assert s["never_up"] == 1
    assert s["target_reached"] == 1 and s["target_exited"] == 0
    assert s["stop_breached"] == 1


def test_미측정_케이스는_요약에서_빠진다(db):
    """분봉이 없다고 케이스를 버리지는 않지만, 측정치 평균에 섞지도 않는다."""
    cases.save([_case("a", "2026-08-03"), {**_case("b", "2026-08-03"), "bars": 0}])
    assert cases.stats()["n"] == 1


def test_케이스가_없으면_빈_요약(db):
    assert cases.stats() == {"n": 0}


def test_커버리지는_미측정_건을_센다(db):
    """분봉이 없어 못 잰 건을 세지 않으면 평균이 조용히 '분봉 있는 종목' 쪽으로
    치우친다. 1분봉은 유니버스 전체가 아니라 감시목록·로스터에만 있다."""
    cases.save([_case("a", "2026-08-03"),
                {**_case("b", "2026-08-03"), "bars": 0},
                {**_case("c", "2026-08-04"), "bars": 0}])
    cov = cases.coverage()
    assert cov["n"] == 3 and cov["measured"] == 1 and cov["unmeasured"] == 2
    assert cov["measured_pct"] == pytest.approx(33.3)
    assert cov["first_day"] == "2026-08-03" and cov["last_day"] == "2026-08-04"


def test_소급_적재는_날짜별_건수를_돌려준다(db, monkeypatch):
    """합계만 돌려주면 '분봉이 없어 못 만든 날' 이 침묵으로 사라진다."""
    from datetime import datetime as _dt

    from app.trade import ledger

    class _Conn:
        def execute(self, *a):
            return [("2026-08-03",), ("2026-08-04",)]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ledger, "_conn", lambda: _Conn())
    monkeypatch.setattr(cases, "build_day", lambda day, now=None: 7)
    got = cases.build_range(30, _dt(2026, 8, 5, tzinfo=cases.KST))
    assert got == {"2026-08-03": 7, "2026-08-04": 7}
