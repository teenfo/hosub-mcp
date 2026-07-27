"""취합 — "서로 다른 정보원이 같은 종목을 가리켰는가".

여기서 지키는 것:
  - 같은 팩터를 세 번 세지 않는다 (그룹 내 max)
  - 서로 다른 정보원이 겹칠 때만 점수가 오른다 (그룹 간 합)
  - 동점 정렬이 결정적이다 — 현행 발굴은 API 응답 순서에 의존한다
  - 가중치를 학습하지 않는다 (근거가 무효로 판명됐다)
"""
from datetime import UTC, datetime, timedelta

import pytest

from app.scout import model, scoring
from app.scout.model import Signal

NOW = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)


def _s(code="000001", source=model.VOLUME, strength=0.8, at=NOW, **kw):
    return Signal(code=code, name=kw.pop("name", f"종목{code}"), source=source,
                  strength=strength, observed_at=at, **kw)


# --- ① 그룹 내 max — 같은 팩터를 세 번 세지 않는다 ---

def test_intraday_sources_do_not_stack():
    """volume·gainers·presurge 는 같은 팩터의 세 가지 뷰다.

    앞의 둘은 실제로 같은 change_pct 로 정렬한다. 더하면 이벤트 스터디가
    잡아낸 베타 노출을 3배로 증폭한다.
    """
    got = scoring.aggregate([
        _s(source=model.VOLUME, strength=0.9),
        _s(source=model.GAINERS, strength=0.7),
        _s(source=model.PRESURGE, strength=0.5),
    ], NOW)
    assert len(got) == 1
    assert got[0].score == pytest.approx(0.9)      # 합 2.1 이 아니라 max
    assert got[0].group_count == 1


def test_distinct_groups_add_up():
    """서로 다른 정보원이 같은 종목을 가리킬 때만 점수가 오른다."""
    got = scoring.aggregate([
        _s(source=model.VOLUME, strength=0.9),
        _s(source=model.NIGHTLY, strength=0.6),
        _s(source=model.NEWS, strength=0.5),
    ], NOW)
    assert got[0].score == pytest.approx(2.0)
    assert got[0].group_count == 3


def test_group_count_is_the_readable_number():
    """점수보다 '몇 개 정보원이 합의했나' 가 해석 가능하다."""
    one = scoring.aggregate([_s(source=model.VOLUME, strength=1.0)], NOW)[0]
    two = scoring.aggregate([_s(source=model.VOLUME, strength=0.3),
                             _s(source=model.NEWS, strength=0.3)], NOW)[0]
    assert one.score > two.score          # 점수는 단일 소스가 높지만
    assert one.group_count < two.group_count   # 합의는 두 소스 쪽이 넓다


def test_by_group_breakdown_is_exposed():
    """화면의 소스 기여 스택바 재료."""
    c = scoring.aggregate([
        _s(source=model.GAINERS, strength=0.4),
        _s(source=model.NIGHTLY, strength=0.6),
    ], NOW)[0]
    assert c.by_group == {"intraday": pytest.approx(0.4), "daily": pytest.approx(0.6)}


# --- ② 감쇠·만료가 취합에 반영된다 ---

def test_expired_signals_are_excluded():
    got = scoring.aggregate([_s(at=NOW - timedelta(seconds=400))], NOW)
    assert got == []


def test_decay_lowers_the_contribution():
    fresh = scoring.aggregate([_s(strength=1.0, at=NOW)], NOW)[0]
    stale = scoring.aggregate([_s(strength=1.0, at=NOW - timedelta(seconds=120))], NOW)[0]
    assert fresh.score > stale.score > 0


def test_manual_signal_does_not_decay():
    old = NOW - timedelta(days=30)
    c = scoring.aggregate([_s(source=model.MANUAL, strength=1.0, at=old)], NOW)[0]
    assert c.score == pytest.approx(1.0)


# --- ③ 정렬이 결정적이다 ---

def test_ties_break_on_symbol_code_not_input_order():
    """현행 발굴은 tie-break 가 없어 상위 5 선택이 API 응답 순서에 의존한다
    (discovery.py:205). 점수가 0~3 정수라 동점이 대량 발생한다."""
    sigs = [_s(code=c, strength=0.5) for c in ("000009", "000002", "000005")]
    assert [c.code for c in scoring.aggregate(sigs, NOW)] == \
           ["000002", "000005", "000009"]
    assert [c.code for c in scoring.aggregate(sigs[::-1], NOW)] == \
           ["000002", "000005", "000009"]


def test_sorted_by_score_descending():
    got = scoring.aggregate([_s(code="000001", strength=0.2),
                             _s(code="000002", strength=0.9)], NOW)
    assert [c.code for c in got] == ["000002", "000001"]


# --- ④ 가중치를 학습하지 않는다 ---

def test_weights_are_flat():
    """잔차 IC 로 학습한 합성 랭킹이 6개 방식 중 꼴찌였다(2026-07-27 실측).

    가중치의 근거가 무효로 판명됐으므로 전 소스 1.0 고정이다. 이 값이 바뀌면
    측정 없이 손으로 정한 것이다.
    """
    assert scoring.WEIGHT == 1.0
    same = [scoring.aggregate([_s(source=src, strength=0.5)], NOW)[0].score
            for src in model.SOURCES]
    assert same == [pytest.approx(0.5)] * len(model.SOURCES)


def test_max_possible_is_group_count():
    assert scoring.max_possible() == float(len(model.GROUPS))


# --- ⑤ 부가 정보 ---

def test_price_is_kept_for_the_tradability_gate():
    got = scoring.aggregate([_s(price=0.0), _s(source=model.GAINERS, price=71_200.0)],
                            NOW)
    assert got[0].price == 71_200.0


def test_evidence_lists_every_contributing_signal():
    """그룹 내 max 로 점수에서 빠진 소스도 근거에는 남아야 한다 —
    화면에서 '왜 올라왔나' 를 물으면 셋 다 보여야 한다."""
    c = scoring.aggregate([
        _s(source=model.VOLUME, strength=0.9, raw=12.3),
        _s(source=model.GAINERS, strength=0.2, raw=8.1),
    ], NOW)[0]
    assert {e["source"] for e in c.evidence()} == {model.VOLUME, model.GAINERS}
    assert c.sources == [model.GAINERS, model.VOLUME]


def test_zero_strength_signal_is_dropped():
    """목록 꼴찌(순위 세기 0)는 후보를 만들지 않는다."""
    assert scoring.aggregate([_s(strength=0.0)], NOW) == []


def test_empty_input_is_safe():
    assert scoring.aggregate([], NOW) == []
