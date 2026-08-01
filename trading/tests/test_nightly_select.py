"""발굴 후보 선정 — 점수 표본 + 조건식 표본 + 무작위 표본.

유입 깔때기 실측 2026-07-31: 상장 3,925 → 유동성 786 → min_score 2점 이상 8종목.
늘린 자리를 점수 순으로 채우지 않는 근거는 1.5단계 실측(무작위가 모든 랭킹을 이겼다).
조건식 팔의 근거는 그 반례다 — 외부 문서 조건식만 백테스트에서 무작위를 이겼다.
"""
from app.scout.nightly import _select, screen_pass


def _pool(n, score=lambda i: 0.0, screen=lambda i: 0):
    return [{"code": "%06d" % i, "name": f"종목{i}", "close": 1000 + i,
             "score": score(i), "reasons": [], "screen": screen(i)}
            for i in range(n)]


def test_점수_표본은_점수순_그리고_동점은_코드순():
    """동점 tie-break 가 없으면 상위 N 이 API 응답 순서에 의존한다."""
    pool = _pool(6, score=lambda i: 2.0 if i < 4 else 0.0)
    scored = list(reversed(pool[:4]))          # 일부러 뒤섞어 넣는다
    top = _select(scored, pool, {"top_n": 3, "random_fill": 0}, "2026-08-03")
    assert [p["code"] for p in top] == ["000000", "000001", "000002"]
    assert all(p["pick_kind"] == "score" for p in top)


def test_무작위_표본이_자리를_채운다():
    pool = _pool(100, score=lambda i: 2.0 if i < 2 else 0.0)
    top = _select(pool[:2], pool, {"top_n": 20, "random_fill": 30}, "2026-08-03")
    kinds = [p["pick_kind"] for p in top]
    assert kinds.count("score") == 2 and kinds.count("random") == 30


def test_무작위는_점수_표본과_겹치지_않는다():
    pool = _pool(50, score=lambda i: 3.0 if i < 5 else 0.0)
    top = _select(pool[:5], pool, {"top_n": 20, "random_fill": 40}, "2026-08-03")
    codes = [p["code"] for p in top]
    assert len(codes) == len(set(codes))       # 같은 종목이 두 번 실리지 않는다


def test_같은_날은_같은_표본_다른_날은_다른_표본():
    """재실행·재시작이 측정을 흔들면 대조군이 성립하지 않는다."""
    pool = _pool(200)
    cfg = {"top_n": 20, "random_fill": 30}
    a = [p["code"] for p in _select([], pool, cfg, "2026-08-03")]
    b = [p["code"] for p in _select([], pool, cfg, "2026-08-03")]
    c = [p["code"] for p in _select([], pool, cfg, "2026-08-04")]
    assert a == b
    assert a != c


def test_random_fill_0_이면_종전_동작():
    pool = _pool(50, score=lambda i: 2.0 if i < 3 else 0.0)
    top = _select(pool[:3], pool, {"top_n": 20, "random_fill": 0}, "2026-08-03")
    assert len(top) == 3 and all(p["pick_kind"] == "score" for p in top)


def test_모집단이_요청보다_작으면_있는_만큼만():
    pool = _pool(5)
    top = _select([], pool, {"top_n": 20, "random_fill": 40}, "2026-08-03")
    assert len(top) == 5


def test_모집단이_비면_빈_목록():
    assert _select([], [], {"top_n": 20, "random_fill": 40}, "2026-08-03") == []


# --- 조건식 표본 ----------------------------------------------------------------

def _feat(**kw):
    """네 조건을 모두 통과하는 피처. 테스트마다 한 축씩 깬다."""
    return {"ma_aligned": 1, "disparity20": 102.0, "near_high60_pct": 97.0,
            "trade_value": 20_000_000_000} | kw


def test_네_조건을_모두_만족해야_통과():
    assert screen_pass(_feat(), {}) is True


def test_정배열이_아니면_탈락():
    assert screen_pass(_feat(ma_aligned=0), {}) is False


def test_이격도는_위아래_모두_5퍼센트_이내():
    assert screen_pass(_feat(disparity20=105.0), {}) is True
    assert screen_pass(_feat(disparity20=95.0), {}) is True
    assert screen_pass(_feat(disparity20=105.1), {}) is False
    assert screen_pass(_feat(disparity20=94.9), {}) is False


def test_이평을_못_구하면_탈락():
    """`disparity20` 은 ma20 이 없으면 0 이 온다 — 0 을 '이평에 붙었다' 로 읽으면 안 된다."""
    assert screen_pass(_feat(disparity20=0.0), {}) is False


def test_신고가_근접_문턱():
    assert screen_pass(_feat(near_high60_pct=95.0), {}) is True
    assert screen_pass(_feat(near_high60_pct=94.9), {}) is False


def test_거래대금_문턱():
    assert screen_pass(_feat(trade_value=10_000_000_000), {}) is True
    assert screen_pass(_feat(trade_value=9_999_999_999), {}) is False


def test_문턱은_설정으로_바꾼다():
    cfg = {"screen": {"require_ma_aligned": False, "disparity_max_pct": 20,
                      "near_high60_min": 50, "min_trade_value_krw": 0}}
    assert screen_pass(_feat(ma_aligned=0, disparity20=85.0,
                             near_high60_pct=60.0, trade_value=1), cfg) is True


def test_조건식_표본이_자리를_채운다():
    pool = _pool(100, screen=lambda i: int(i >= 50))
    top = _select([], pool, {"top_n": 20, "screen_fill": 5, "random_fill": 0},
                  "2026-08-03")
    assert [p["pick_kind"] for p in top] == ["screen"] * 5
    assert all(int(p["code"]) >= 50 for p in top)


def test_통과분이_적으면_있는_만큼만():
    pool = _pool(100, screen=lambda i: int(i < 3))
    top = _select([], pool, {"top_n": 20, "screen_fill": 20, "random_fill": 0},
                  "2026-08-03")
    assert len(top) == 3


def test_조건식_통과가_없으면_그_팔은_비어_있다():
    """조건식이 아무것도 안 내놓는 날이 정상이다 — 채우려 들면 조건식이 아니다."""
    pool = _pool(50)
    top = _select([], pool, {"top_n": 20, "screen_fill": 20, "random_fill": 0},
                  "2026-08-03")
    assert top == []


def test_screen_fill_0_이면_팔이_사라진다():
    """되돌리기가 설정 한 줄이라는 것의 회귀 테스트."""
    pool = _pool(50, screen=lambda i: 1)
    top = _select([], pool, {"top_n": 20, "screen_fill": 0, "random_fill": 10},
                  "2026-08-03")
    assert all(p["pick_kind"] == "random" for p in top)


def test_배포_기본값은_꺼짐이다():
    """근거가 무너진 팔이 조용히 돌지 않게 못박는다.

    조건식이 무작위를 이긴 것은 **변동성을 통제하지 않았기 때문**이었다 —
    같은 날 비슷한 atr_pct 종목과 비교하면 격차가 +0.030R(t=+0.70)로 0 과
    구분되지 않는다. 되살리려면 그 대조군을 먼저 통과시켜야 하고, 그때
    이 테스트를 함께 고치면 된다.
    """
    from pathlib import Path

    import yaml

    cfg = yaml.safe_load(
        Path(__file__).resolve().parents[1].joinpath("config.yaml")
        .read_text(encoding="utf-8"))["nightly"]
    assert cfg["screen_fill"] == 0
    assert cfg["random_fill"] > 0          # 대조군 팔은 계속 돈다


def test_한_종목은_한_팔에만_속한다():
    """같은 종목이 두 팔에 실리면 4주 뒤 팔별 성적이 성립하지 않는다."""
    pool = _pool(60, score=lambda i: 3.0 if i < 2 else 0.0,
                 screen=lambda i: int(i < 30))
    top = _select(pool[:2], pool,
                  {"top_n": 20, "screen_fill": 10, "random_fill": 20}, "2026-08-03")
    codes = [p["code"] for p in top]
    assert len(codes) == len(set(codes))
    by = {}
    for p in top:
        by.setdefault(p["code"], set()).add(p["pick_kind"])
    assert all(len(v) == 1 for v in by.values())
    # 점수 표본이 조건식보다 먼저 자리를 가져간다
    assert {p["pick_kind"] for p in top if p["code"] in ("000000", "000001")} == {"score"}


def test_조건식_팔은_같은_날_재현된다():
    pool = _pool(200, screen=lambda i: 1)
    cfg = {"top_n": 20, "screen_fill": 20, "random_fill": 0}
    a = [p["code"] for p in _select([], pool, cfg, "2026-08-03")]
    b = [p["code"] for p in _select([], pool, cfg, "2026-08-03")]
    c = [p["code"] for p in _select([], pool, cfg, "2026-08-04")]
    assert a == b and a != c


def test_조건식_팔을_켜도_무작위_대조군은_같은_시드를_쓴다():
    """시드를 나눠 쓰면 팔 하나를 켜고 끄는 것만으로 대조군 구성이 통째로 바뀐다.

    조건식 통과분이 무작위 모집단에서 빠지므로 **구성원은 달라질 수 있다** —
    그건 한 종목이 한 팔에만 속하기 위한 대가다. 여기서 확인하는 것은 뽑는
    순서(시드)가 조건식 팔에 오염되지 않는다는 것이다.
    """
    pool = _pool(200, screen=lambda i: 0)   # 통과분 0 → 무작위 모집단이 동일하다
    off = _select([], pool, {"top_n": 20, "screen_fill": 0, "random_fill": 30},
                  "2026-08-03")
    on = _select([], pool, {"top_n": 20, "screen_fill": 20, "random_fill": 30},
                 "2026-08-03")
    assert [p["code"] for p in off] == [p["code"] for p in on]


# --- 무작위 표본이 실제로 관측 팔이 되는가 --------------------------------------

def test_무작위_표본은_승격선을_넘는_강도를_받는다(monkeypatch):
    """강도 0 이면 신호는 남지만 후보가 되지 않아 관측 팔이 존재하지 않는다.

    그리고 **점수 표본과 같은 강도**여야 한다. 감시목록이 상한에 차 있을 때
    강도는 화면 표시가 아니라 누가 매매 대상이 되는가를 정한다. 점수 표본은
    변동성 정합 대조군 대비 -0.168R(t=-10.70) 이라 앞자리를 줄 근거가 없다.
    """
    import asyncio

    from app.scout import model
    from app.scout.sources import slow

    monkeypatch.setattr(
        slow, "latest_picks", lambda: ("2026-08-03", [
            {"code": "000001", "name": "점수", "close": 1000, "score": 2.0,
             "reasons": [], "pick_kind": "score"},
            {"code": "000002", "name": "무작위", "close": 2000, "score": 0.0,
             "reasons": [], "pick_kind": "random"},
        ]), raising=False)
    monkeypatch.setattr("app.scout.nightly.latest_picks", slow.latest_picks,
                        raising=False)

    sigs = asyncio.run(slow.NightlySource().collect())
    by = {s.code: s for s in sigs}
    assert by["000002"].strength == model.NIGHTLY_STRENGTH
    assert by["000002"].kind == "nightly:random"
    assert by["000001"].kind == "nightly"
    # 두 팔이 같은 강도로 자리를 다툰다 — 점수에는 앞자리를 줄 근거가 없다
    assert by["000001"].strength == by["000002"].strength
    # 원시 점수는 사라지지 않는다 — 사후 대조에 쓴다
    assert by["000001"].raw == 2.0 and by["000002"].raw == 0.0


def test_조건식_표본도_구분되는_강도와_kind_를_받는다(monkeypatch):
    """세 팔이 신호 원장에서 끝까지 구분돼야 4주 뒤 팔별 판정이 가능하다."""
    import asyncio

    from app.scout import model
    from app.scout.sources import slow

    monkeypatch.setattr(
        slow, "latest_picks", lambda: ("2026-08-03", [
            {"code": "000001", "name": "점수", "close": 1000, "score": 2.0,
             "reasons": [], "pick_kind": "score"},
            {"code": "000002", "name": "조건식", "close": 2000, "score": 0.0,
             "reasons": [], "pick_kind": "screen"},
            {"code": "000003", "name": "무작위", "close": 3000, "score": 0.0,
             "reasons": [], "pick_kind": "random"},
        ]), raising=False)
    monkeypatch.setattr("app.scout.nightly.latest_picks", slow.latest_picks,
                        raising=False)

    by = {s.code: s for s in asyncio.run(slow.NightlySource().collect())}
    assert by["000002"].kind == "nightly:screen"
    assert by["000002"].evidence["pick_kind"] == "screen"
    # 세 팔이 같은 강도다 — 어느 팔이 나은지 측정된 적이 없다
    assert {s.strength for s in by.values()} == {model.NIGHTLY_STRENGTH}
    # 그래도 kind 로는 끝까지 구분된다 (사후 대조의 전제)
    assert len({s.kind for s in by.values()}) == 3
