"""VPIN 회피 게이트 반사실 — 사전 확정 기준이 코드대로 도는가."""
from app.research import vpingate as vg


def _p(sym, day, entry=10_000.0, exit_=10_100.0, stop=9_800.0, rule="orb",
       side="long", env="mock"):
    return {"symbol": sym, "opened": f"{day}T10:00:00+09:00", "entry": entry,
            "exit": exit_, "stop": stop, "rule": rule, "side": side, "env": env}


def _day(d, n=25, hi_codes=("H",)):
    """그날 관측 n종목 — hi_codes 가 최상위 VPIN."""
    rows = [{"d": d, "code": f"C{i:02d}", "vpin": 0.1 + i * 0.001} for i in range(n)]
    rows += [{"d": d, "code": c, "vpin": 0.9} for c in hi_codes]
    rows += [{"d": d, "code": "L", "vpin": 0.05}]
    return rows


def test_R_은_원본_손절_기준이고_클립된다():
    assert vg.trade_r(_p("A", "2026-09-01")) == 0.5
    assert vg.trade_r(_p("A", "2026-09-01", exit_=12_900.0)) == 5.0    # +29% 는 오염 아님 → 클립
    assert vg.trade_r(_p("A", "2026-09-01", exit_=13_100.0)) is None   # |이동|>30% 오염
    big = vg.trade_r(_p("A", "2026-09-01", stop=9_950.0, exit_=10_400.0))
    assert big == 5.0                                                  # ±5R 클립


def test_얇은_손절폭은_측정_불가():
    assert vg.trade_r(_p("A", "2026-09-01", stop=9_990.0)) is None     # 0.1% < 0.3%


def test_진입일_이전_관측일의_상위를_쓴다():
    rows = _day("2026-09-01")
    got = vg.evaluate([_p("H", "2026-09-02", exit_=9_800.0),
                       _p("L", "2026-09-02")], rows)
    assert got["high"] == {"n": 1, "avg_r": -1.0}
    assert got["rest"] == {"n": 1, "avg_r": 0.5}


def test_같은날_관측은_쓰지_않는다():
    """진입일 당일 VPIN 은 장 마감 관측이라 진입 시점엔 몰랐던 값 — 선견 편향."""
    rows = _day("2026-09-02")
    got = vg.evaluate([_p("H", "2026-09-02")], rows)
    assert got["skipped"]["no_prior_vpin"] == 1
    assert got["high"]["n"] == 0


def test_관측_없는_종목은_0으로_세지_않는다():
    rows = _day("2026-09-01")
    got = vg.evaluate([_p("ZZZ", "2026-09-02")], rows)
    assert got["skipped"]["no_prior_vpin"] == 1


def test_관측이_얇은_날은_분포를_만들지_않는다():
    rows = [{"d": "2026-09-01", "code": f"C{i}", "vpin": 0.1 * i} for i in range(5)]
    assert vg.high_sets(rows, 20.0) == {}


def test_외부_잔량_숏은_표본에서_뺀다():
    rows = _day("2026-09-01")
    got = vg.evaluate([_p("H", "2026-09-02", rule="external"),
                       _p("H", "2026-09-02", rule="residual"),
                       _p("H", "2026-09-02", side="short")], rows)
    assert got["skipped"]["rule_or_side"] == 3


def test_표본_미달이면_연기():
    rows = _day("2026-09-01")
    got = vg.evaluate([_p("H", "2026-09-02", exit_=9_800.0)] * 5, rows)
    assert got["verdict"].startswith("연기")


def test_통과_판정():
    """고VPIN 이 일관되게 나쁘고 표본이 충분하면 통과."""
    rows = []
    pos = []
    for i in range(40):
        d = f"2026-08-{(i % 28) + 1:02d}"
        rows += _day(d)
    days = sorted({r["d"] for r in rows})
    for i, d in enumerate(days[:-1]):
        nxt = days[i + 1]
        pos += [_p("H", nxt, exit_=9_800.0 + (i % 3) * 10),
                _p("H", nxt, exit_=9_850.0),
                _p("L", nxt, exit_=10_100.0 - (i % 3) * 10),
                _p("L", nxt, exit_=10_050.0)]
    got = vg.evaluate(pos, rows)
    assert got["high"]["n"] >= 30 and got["rest"]["n"] >= 30
    assert got["verdict"].startswith("통과")
