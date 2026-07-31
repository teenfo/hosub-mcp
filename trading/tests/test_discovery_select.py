"""발굴 후보 선정 — 점수 표본 + 무작위 표본.

유입 깔때기 실측 2026-07-31: 상장 3,925 → 유동성 1,165 → min_score 2점 이상 8종목.
늘린 자리를 점수 순으로 채우지 않는 근거는 1.5단계 실측(무작위가 모든 랭킹을 이겼다).
"""
from app.discovery import _select


def _pool(n, score=lambda i: 0.0):
    return [{"code": "%06d" % i, "name": f"종목{i}", "close": 1000 + i,
             "score": score(i), "reasons": []} for i in range(n)]


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


# --- 무작위 표본이 실제로 관측 팔이 되는가 --------------------------------------

def test_무작위_표본은_승격선을_넘는_강도를_받는다(monkeypatch):
    """강도 0 이면 신호는 남지만 후보가 되지 않아 관측 팔이 존재하지 않는다."""
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
    monkeypatch.setattr("app.discovery.latest_picks", slow.latest_picks,
                        raising=False)

    sigs = asyncio.run(slow.NightlySource().collect())
    by = {s.code: s for s in sigs}
    assert by["000002"].strength == model.RANDOM_STRENGTH
    assert by["000002"].kind == "nightly:random"
    # 사유가 있는 후보가 자리를 먼저 가져간다
    assert by["000001"].strength > by["000002"].strength
    assert by["000001"].kind == "nightly"
