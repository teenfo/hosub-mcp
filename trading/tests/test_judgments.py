"""판정 입력 스냅샷 — 원장 보관 기간(signals 60일·features 30일)보다 오래 남는 근거."""
import pytest

from app.research import judgments, vpingate


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(judgments, "DIR", tmp_path / "judgments")
    return tmp_path


def test_저장한_입력을_그대로_되읽는다():
    rows = [{"code": "005930", "day": "2026-10-23", "diff_r": {"1.0": 0.2}}]
    judgments.save("flow", rows, criteria="전 손절폭 양수", summary={"n": 1},
                   day="2026-10-26")
    got = judgments.load("flow", "2026-10-26")
    assert got["rows"] == rows and got["criteria"] == "전 손절폭 양수"
    assert got["summary"] == {"n": 1} and got["name"] == "flow"


def test_같은_날_다시_돌리면_덮어쓴다():
    judgments.save("flow", [{"a": 1}], day="2026-10-26")
    judgments.save("flow", [{"a": 2}], day="2026-10-26")
    assert judgments.load("flow", "2026-10-26")["rows"] == [{"a": 2}]


def test_이력은_날짜순이고_이름이_겹치지_않는다():
    judgments.save("flow", [{"a": 2}], day="2026-11-30")
    judgments.save("flow", [{"a": 1}], day="2026-10-26")
    judgments.save("flow_x", [{"a": 9}], day="2026-10-27")     # 접두사가 같은 다른 판정
    assert [h["day"] for h in judgments.history("flow")] == ["2026-10-26", "2026-11-30"]


def test_이름과_날짜_형식을_검증한다():
    with pytest.raises(ValueError):
        judgments.save("../etc", [], day="2026-10-26")
    with pytest.raises(ValueError):
        judgments.save("flow", [], day="26-10-2026")


def test_없는_스냅샷은_None():
    assert judgments.load("flow", "2026-01-01") is None


def test_vpingate_는_판정_입력을_관측_단위로_담는다():
    pos = [{"id": "p1", "symbol": "H", "opened": "2026-09-02T10:00:00+09:00",
            "entry": 10_000.0, "exit": 9_800.0, "stop": 9_800.0, "rule": "orb",
            "side": "long", "env": "mock"}]
    rows = [{"d": "2026-09-01", "code": f"C{i:02d}", "vpin": 0.1} for i in range(25)]
    rows.append({"d": "2026-09-01", "code": "H", "vpin": 0.9})
    pairs = []
    vpingate.evaluate(pos, rows, collect=pairs)
    assert pairs == [{"id": "p1", "symbol": "H", "day": "2026-09-02",
                      "vpin_day": "2026-09-01", "r": -1.0, "high": True, "env": "mock"}]
