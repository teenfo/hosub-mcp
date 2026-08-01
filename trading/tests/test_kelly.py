"""켈리 기준 — f = p - q/b. 음수면 "베팅하지 말라" 는 뜻이다.

실측 2026-07-31 (5거래일 107건): 승률 35.5% · 평균이익 +1.302% ·
평균손실 -1.714% → b = 0.759 → f = -0.494.
"""
import pytest

from app.research import kelly


def test_문서_예시가_재현된다():
    """문서 8.2: 승률 40% · 익절 +6% · 손절 -2% → b=3 → f=0.2."""
    k = kelly.kelly(0.4, 6.0, 2.0)
    assert k["b"] == 3.0
    assert k["f"] == pytest.approx(0.2)


def test_우리_실측은_음수다():
    """이 값이 이 파일의 존재 이유다."""
    k = kelly.kelly(0.355, 1.302, 1.714)
    assert k["b"] == pytest.approx(0.7596, abs=1e-3)
    assert k["f"] < 0
    assert k["f"] == pytest.approx(-0.494, abs=1e-3)


def test_f가_0이_되는_조건을_함께_돌려준다():
    """목표를 정할 때 쓰는 숫자다 — '얼마면 되나' 에 답해야 한다."""
    k = kelly.kelly(0.355, 1.302, 1.714)
    # 현재 손익비 유지 시 필요한 승률
    assert k["need_win_rate"] == pytest.approx(1 / (1 + k["b"]), abs=1e-3)
    # 현재 승률 유지 시 필요한 손익비
    assert k["need_payoff"] == pytest.approx(0.645 / 0.355, abs=1e-2)
    # 그 값이 설계 목표 1.5R 을 넘는다 — 목표에 100% 도달해도 부족하다
    assert k["need_payoff"] > 1.5


def test_손절폭_크기는_양수로_받는다():
    """부호로 받으면 호출부마다 규약이 갈린다."""
    assert kelly.kelly(0.4, 6.0, 2.0) == kelly.kelly(0.4, 6.0, -2.0)


@pytest.mark.parametrize("args", [
    (0.0, 6.0, 2.0), (1.0, 6.0, 2.0),     # 전패·전승은 손익비가 성립하지 않는다
    (0.4, 0.0, 2.0), (0.4, 6.0, 0.0),
])
def test_성립하지_않으면_ok_False(args):
    """0 으로 채우면 '계산했는데 0' 과 '못 쟀다' 가 섞인다."""
    assert kelly.kelly(*args) == {"ok": False}


# --- 거래 기록에서 -------------------------------------------------------------

def _t(*pnls):
    return [{"pnl_pct": p} for p in pnls]


def test_거래_기록에서_계산():
    k = kelly.from_trades(_t(2.0, 2.0, -1.0, -1.0))
    assert k["ok"] and k["n"] == 4
    assert k["p"] == 0.5 and k["b"] == 2.0
    assert k["f"] == pytest.approx(0.25)


def test_본전은_패로_센다():
    """왕복 비용을 이미 뺀 값이라 0 은 이익이 아니다."""
    k = kelly.from_trades(_t(2.0, 0.0, -1.0, -1.0))
    assert k["p"] == 0.25


def test_한쪽만_있으면_성립하지_않는다():
    assert kelly.from_trades(_t(1.0, 2.0))["ok"] is False
    assert kelly.from_trades(_t(-1.0, -2.0))["ok"] is False


def test_표본이_없으면_ok_False():
    assert kelly.from_trades([])["ok"] is False
    assert kelly.from_trades([{"pnl_pct": None}])["ok"] is False


# --- 문장 ---------------------------------------------------------------------

def test_음수면_문장이_그렇게_말한다():
    k = kelly.from_trades(_t(1.302, -1.714, -1.714))
    s = kelly.line(k, target_r=1.5)
    assert "음수" in s and "투입할수록 잃는다" in s
    assert "판단을 보류" in s


def test_설계_목표로_도달할_수_없으면_그렇게_적는다():
    k = kelly.from_trades(_t(1.302, -1.714, -1.714))     # need_payoff = 2.0
    assert "설계 목표" in kelly.line(k, target_r=1.5)
    assert "설계 목표" not in kelly.line(k, target_r=3.0)


def test_양수면_최적_투입을_적는다():
    k = kelly.from_trades(_t(2.0, 2.0, 2.0, -1.0))
    s = kelly.line(k)
    assert "최적 투입" in s and "음수" not in s


def test_값이_없으면_빈_문장():
    assert kelly.line({"ok": False}) == ""
