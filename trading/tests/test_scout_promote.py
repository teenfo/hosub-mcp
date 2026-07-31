"""승격·강등 판정 — 이 단계에서 1.5단계 실측이 실제로 적용된다.

가장 중요한 회귀 감지는 **점수로 매매 tier 를 열지 않는가** 다. 원안은
promote_trade: 0.65 점수 임계였는데, 점수 순 상위 N 이 매수가능 무작위보다
0.22R 나빴다는 것이 실측됐다(191거래일 / 3,907종목). 이 파일이 그 결론을
코드로 붙잡아 둔다.
"""
from datetime import UTC, datetime, timedelta

import pytest

from app import settings
from app.scout import promote
from app.scout.promote import COLLECT, NONE, TRADE, Current
from app.scout.scoring import Candidate

# 2026-07-27(월) 10:00 KST = 장중. 강등은 장중에만 일어나므로 픽스처 시각이
# 세션 안이어야 한다 — 밖이면 강등 테스트가 전부 무효가 된다.
NOW = datetime(2026, 7, 27, 1, 0, tzinfo=UTC)
AFTER_CLOSE = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)     # 19:00 KST
LONG_AGO = NOW - timedelta(hours=2)


def _c(code="000001", score=0.9, price=10_000, groups=1, **kw):
    return Candidate(code=code, name=kw.pop("name", f"종목{code}"), score=score,
                     price=price,
                     sources=kw.pop("sources", ["volume"]),
                     by_group=kw.pop("by_group", {"intraday": score}) if groups == 1
                     else {"intraday": score / 2, "news": score / 2})


def _cur(tier=None, since=None, protected=(), names=None, held=(),
         trade_since=None, last_signal=None, demoted_at=None):
    """`protected` 는 drop 금지(보유+수동), `held` 는 그중 demote 도 금지(보유만)."""
    return Current(tier=dict(tier or {}), since=dict(since or {}),
                   protected=frozenset(protected) | frozenset(held),
                   held=frozenset(held), names=dict(names or {}),
                   trade_since=dict(trade_since or {}),
                   last_signal=dict(last_signal or {}),
                   demoted_at=dict(demoted_at or {}))


@pytest.fixture(autouse=True)
def _cap(monkeypatch):
    """체결가능성 한도를 고정 — 자산 동기화 상태에 좌우되지 않게."""
    monkeypatch.setattr(settings, "tradable_price_cap", lambda default: 30_000.0)


def _plan(cands, cur, now=NOW, **over):
    return promote.plan(cands, cur, now, promote.DEFAULTS | over)


def _by(rows, action):
    return [r for r in rows if r["action"] == action]


# --- ① 매매 승격은 점수가 아니라 게이트로 한다 ---

def test_high_score_alone_does_not_open_trading():
    """점수만으로 매매 tier 에 올리면 실측을 무시하는 것이다.

    새 종목은 아무리 점수가 높아도 먼저 수집전용으로만 들어간다.
    """
    rows = _plan([_c(score=3.0)], _cur())
    assert [r["action"] for r in rows] == ["promote_collect"]
    assert rows[0]["to_tier"] == COLLECT


def test_expensive_symbol_reaches_trade_tier():
    """**가격 상한은 여기서 보지 않는다** (사용자 결정 2026-07-29).

    발굴엔진은 가격과 무관하게 매매 tier 로 올리고, 살 수 있는지는 매매 데스크가
    승인대기 단계에서 판정한다. 여기서 막으면 비싼 종목은 신호가 났다는 사실조차
    화면에 남지 않아 사람이 판단할 기회가 없다.
    """
    cur = _cur(tier={"000001": COLLECT}, since={"000001": LONG_AGO})
    rows = _plan([_c(score=3.0, price=412_000)], cur)
    assert [r["action"] for r in _by(rows, "promote_trade")] == ["promote_trade"]


def test_price_cap_can_be_restored_by_config(monkeypatch):
    """되돌릴 수 있어야 한다 — 배포 없이 예전 동작으로."""
    from app.scout import promote as promote_mod

    monkeypatch.setattr(promote_mod, "cfg",
                        lambda: {**promote_mod.DEFAULTS, "price_cap": True})
    cur = _cur(tier={"000001": COLLECT}, since={"000001": LONG_AGO})
    assert _by(_plan([_c(score=3.0, price=412_000)], cur), "promote_trade") == []


def test_affordable_symbol_promotes_after_dwell():
    cur = _cur(tier={"000001": COLLECT}, since={"000001": LONG_AGO})
    rows = _plan([_c(score=0.4, price=12_000)], cur)
    assert [r["action"] for r in rows] == ["promote_trade"]
    assert rows[0]["from_tier"] == COLLECT and rows[0]["to_tier"] == TRADE


def test_unknown_price_is_treated_as_untradable():
    """가격을 모르면 보수적으로 — 매매로 올리지 않는다."""
    cur = _cur(tier={"000001": COLLECT}, since={"000001": LONG_AGO})
    assert _by(_plan([_c(price=0.0)], cur), "promote_trade") == []


def test_probation_blocks_immediate_trade_promotion():
    """검증 안 된 소스가 60초 만에 실거래로 이어지는 것을 막는다.

    부수적으로 '감시했지만 매매하지 않은' 대조군 표본이 만들어진다.
    """
    cur = _cur(tier={"000001": COLLECT},
               since={"000001": NOW - timedelta(minutes=1)})
    assert _by(_plan([_c()], cur), "promote_trade") == []


# --- ② 히스테리시스 — 경계에서 진동하지 않는다 ---

def test_promote_threshold_is_above_demote_threshold():
    assert promote.DEFAULTS["promote_collect"] > promote.DEFAULTS["demote_below"]


def test_score_between_thresholds_holds_steady():
    """승격선 아래·강등선 위 = 아무 일도 없어야 한다. 이게 진동 방지의 실체다."""
    mid = (promote.DEFAULTS["promote_collect"] + promote.DEFAULTS["demote_below"]) / 2
    assert _plan([_c(score=mid)], _cur()) == []          # 새 종목은 안 올라오고
    cur = _cur(tier={"000001": COLLECT}, since={"000001": LONG_AGO})
    assert _by(_plan([_c(score=mid, price=0.0)], cur), "drop") == []   # 있던 건 안 빠진다


def test_vanished_signal_drops_the_symbol():
    cur = _cur(tier={"000001": COLLECT}, since={"000001": LONG_AGO},
               names={"000001": "종목000001"})
    rows = _plan([], cur)
    assert [r["action"] for r in rows] == ["drop"]
    assert rows[0]["name"] == "종목000001"       # 후보에서 사라져도 이름은 남는다


def test_min_dwell_prevents_immediate_drop():
    """60초마다 들락날락하면 WS 재구독이 폭주한다."""
    cur = _cur(tier={"000001": COLLECT}, since={"000001": NOW - timedelta(minutes=1)})
    assert _plan([], cur) == []


# --- ③ 강등에서 제외되는 것 ---

def test_held_and_manual_symbols_are_never_dropped():
    """보유 종목이 감시목록에서 빠지면 청산 감시가 최대 15분 묵은 가격으로 돈다.

    실거래 손실로 이어졌던 결함이다(watchlist._held 참조).
    """
    cur = _cur(tier={"000001": TRADE, "000002": COLLECT},
               since={"000001": LONG_AGO, "000002": LONG_AGO},
               protected={"000001", "000002"})
    assert _plan([], cur) == []


def test_protected_symbols_do_not_count_against_overflow_drops():
    cur = _cur(tier={f"00000{i}": COLLECT for i in range(1, 5)},
               since={f"00000{i}": LONG_AGO for i in range(1, 5)},
               protected={"000001", "000002"})
    cands = [_c(code=f"00000{i}", score=0.9) for i in range(1, 5)]
    rows = _plan(cands, cur, max_total=3)
    assert {r["code"] for r in _by(rows, "drop")} <= {"000003", "000004"}


# --- ④ 상한 ---

def test_total_cap_blocks_new_entries():
    cur = _cur(tier={"000009": COLLECT}, since={"000009": LONG_AGO})
    rows = _plan([_c(code="000009", score=0.9), _c(code="000001", score=3.0)],
                 cur, max_total=1)
    assert _by(rows, "promote_collect") == []


def test_overflow_drops_lowest_score_first():
    """상한을 런타임에 낮추면 이미 초과 상태일 수 있다 — 점수 하위부터 뺀다.

    점수의 유일한 매매 관련 용도가 이것이다: 누구를 올릴지가 아니라
    자리가 모자랄 때 누구를 뺄지.
    """
    codes = ["000001", "000002", "000003"]
    cur = _cur(tier={c: COLLECT for c in codes},
               since={c: LONG_AGO for c in codes})
    cands = [_c(code="000001", score=2.0), _c(code="000002", score=0.9),
             _c(code="000003", score=0.5)]
    rows = _plan(cands, cur, max_total=2)
    assert [r["code"] for r in _by(rows, "drop")] == ["000003"]


def test_trade_cap_demotes_to_collect_not_out():
    """매매 상한 초과분은 감시에서 빼는 게 아니라 수집전용으로 내린다."""
    codes = ["000001", "000002"]
    cur = _cur(tier={c: TRADE for c in codes}, since={c: LONG_AGO for c in codes})
    cands = [_c(code="000001", score=2.0), _c(code="000002", score=0.4)]
    rows = _plan(cands, cur, max_trade=1)
    demoted = _by(rows, "demote")
    assert [r["code"] for r in demoted] == ["000002"]
    assert demoted[0]["to_tier"] == COLLECT


def test_trade_cap_blocks_new_trade_promotions():
    cur = _cur(tier={"000001": COLLECT, "000009": TRADE},
               since={"000001": LONG_AGO, "000009": LONG_AGO})
    rows = _plan([_c(code="000001"), _c(code="000009")], cur, max_trade=1)
    assert _by(rows, "promote_trade") == []


# --- ④-2 보호 종목도 매매 상한을 차지한다 (2026-07-31 교착) ---
#
# 승격은 매매 tier **전체**를 세고, 강등은 `live`(보호 제외)만 셌다. 그 비대칭이
# 들어올 수도 나갈 수도 없는 상태를 만들었다:
#   실측 — 매매 tier 28 · 보호 22 · 비보호 6 · max_trade 25
#   승격: 28 >= 25 → 차단  /  강등: 6 - 25 = -19 → 0건
# `promote_trade` 결정이 7/29 이후 0건이었고 같은 종목만 며칠씩 돌았다
# (5일 106거래 / 고유 50종목, 상위 5종목이 27%·−34,642원).

def test_보유_종목이_매매_상한을_차지한다():
    """보유가 상한을 채우면 나머지는 0까지 밀린다 — 내릴 수 없는 쪽이 이긴다.

    보유는 매매 tier 에서 내리면 분봉 백필 대상에서 빠져(`_collect_targets`)
    청산 감시의 가격 폴백이 낡는다.
    """
    codes = ["000001", "000002", "000003"]
    cur = _cur(tier={c: TRADE for c in codes},
               since={c: LONG_AGO for c in codes},
               protected={"000001", "000002"}, held={"000001", "000002"})
    cands = [_c(code=c, score=0.9) for c in codes]
    rows = _plan(cands, cur, max_trade=2)
    # 보유 2 + 나머지 1 = 3 > 상한 2 → 1건이 내려간다
    assert [r["code"] for r in _by(rows, "demote")] == ["000003"]


def test_보유만으로_상한이_찼으면_나머지는_전부_내려간다():
    cur = _cur(tier={"000001": TRADE, "000002": TRADE, "000003": TRADE},
               since={c: LONG_AGO for c in ("000001", "000002", "000003")},
               protected={"000001", "000002"}, held={"000001", "000002"})
    cands = [_c(code=c, score=0.9) for c in ("000001", "000002", "000003")]
    rows = _plan(cands, cur, max_trade=1)
    assert [r["code"] for r in _by(rows, "demote")] == ["000003"]


def test_수동은_매매_자리를_예약하지_않는다():
    """사용자 결정 2026-07-31 — 수동은 관심 표시이지 매매 우선권이 아니다.

    감시목록에서는 빠지지 않지만(정보 수집 계속) 상한이 모자라면 수집전용으로
    내려간다. 실측: 매매 tier 28 중 22가 수동/seed 로 자리를 영구 점유했다.
    """
    codes = ["000001", "000002", "000003"]
    cur = _cur(tier={c: TRADE for c in codes},
               since={c: LONG_AGO for c in codes},
               protected=set(codes))          # 전부 수동 — held 는 비어 있다
    cands = [_c(code=c, score=0.9) for c in codes]
    rows = _plan(cands, cur, max_trade=1)
    assert len(_by(rows, "demote")) == 2, "수동이어도 상한 초과분은 내려간다"


def test_수동은_감시목록에서는_빠지지_않는다():
    """점수가 0이어도 drop 은 막는다 — 뉴스·공시가 계속 붙어야 한다."""
    cur = _cur(tier={"000001": COLLECT}, since={"000001": LONG_AGO},
               protected={"000001"})
    assert _by(_plan([], cur), "drop") == []


def test_상한_안이면_보호가_있어도_강등하지_않는다():
    """반대 방향 회귀 — 보호를 더한다고 멀쩡한 tier 를 깎으면 안 된다."""
    cur = _cur(tier={"000001": TRADE, "000002": TRADE},
               since={c: LONG_AGO for c in ("000001", "000002")},
               protected={"000001"})
    cands = [_c(code=c, score=0.9) for c in ("000001", "000002")]
    assert _by(_plan(cands, cur, max_trade=5), "demote") == []


def test_교착이_풀린다_승격과_강등이_같은_수를_본다():
    """상한 초과 상태에서 강등이 나와야 다음 사이클에 승격 자리가 생긴다."""
    trade = [f"0000{i:02d}" for i in range(1, 6)]      # 매매 5 (보호 3)
    cur = _cur(tier={**{c: TRADE for c in trade}, "000099": COLLECT},
               since={**{c: LONG_AGO for c in trade}, "000099": LONG_AGO},
               protected=set(trade[:3]))
    cands = [_c(code=c, score=0.9) for c in [*trade, "000099"]]
    rows = _plan(cands, cur, max_trade=4)
    assert len(_by(rows, "demote")) == 1, "5 > 4 이므로 비보호 하위 1건이 내려간다"


# --- ⑤ 결정에 근거가 실린다 ---

def test_decisions_carry_sources_and_reason():
    """사후 귀속의 입력 — 어느 소스가 올렸는지가 남아야 측정이 된다."""
    c = _c(sources=["volume", "news"], score=0.8, groups=2)
    rows = _plan([c], _cur())
    assert rows[0]["sources"] == ["volume", "news"]
    assert rows[0]["reason"]
    assert rows[0]["score"] == 0.8


def test_plan_writes_nothing():
    """순수 함수여야 shadow 가 안전하다."""
    cur = _cur(tier={"000001": COLLECT}, since={"000001": LONG_AGO})
    before = dict(cur.tier)
    _plan([_c(code="000002", score=3.0)], cur)
    assert cur.tier == before


# --- ⑥ 장 마감 후에는 강등하지 않는다 ---
# 장중 소스는 시장이 닫히면 보고할 수가 없다. 그때의 '신호 없음' 은
# "더는 후보가 아니다" 가 아니라 "확인할 수 없다" 다 — TTL 을 넣은 이유와 같다.

def test_in_session_covers_korean_hours():
    assert promote.in_session(NOW) is True                     # 월 10:00 KST
    assert promote.in_session(AFTER_CLOSE) is False            # 월 19:00 KST
    # 토요일 10:00 KST
    assert promote.in_session(datetime(2026, 8, 1, 1, 0, tzinfo=UTC)) is False


def test_no_demotion_after_close():
    """실측 2026-07-27 22:01 shadow: 감시목록 72종목 중 22종목에 강등 결정이
    나왔다 — 전부 장중 소스 편입분이다. 매일 저녁 전멸하고 아침에 다시 들어오는
    회전이 되는데, 그건 이 엔진이 없애려던 바로 그 문제다."""
    cur = _cur(tier={"000001": COLLECT}, since={"000001": LONG_AGO},
               names={"000001": "가"})
    assert _plan([], cur, now=AFTER_CLOSE) == []               # 마감 후: 아무 일 없음
    assert _by(_plan([], cur, now=NOW), "drop")                # 장중: 강등된다


def test_promotion_still_works_after_close():
    """야간 발굴·뉴스는 마감 후에 나온다 — 편입까지 막으면 안 된다."""
    rows = _plan([_c(score=3.0)], _cur(), now=AFTER_CLOSE)
    assert [r["action"] for r in rows] == ["promote_collect"]


def test_cap_overflow_also_waits_for_the_session():
    """상한 초과 정리도 다음 장으로 미룬다 — 마감 후엔 매매가 없어 급할 것이 없다."""
    codes = ["000001", "000002", "000003"]
    cur = _cur(tier={c: COLLECT for c in codes}, since={c: LONG_AGO for c in codes})
    cands = [_c(code=c, score=2.0 - i) for i, c in enumerate(codes)]
    assert _by(_plan(cands, cur, now=AFTER_CLOSE, max_total=2), "drop") == []
    assert _by(_plan(cands, cur, now=NOW, max_total=2), "drop")


# --- ⑥ 기회비용 회전 — 침묵한 자리를 대기 후보에게 넘긴다 (2026-07-31) ---
#
# **성과 기반 강등은 측정으로 기각됐다.** 종목의 과거 손익은 다음 거래를
# 예측하지 않는다(상관 −0.050, n=57, t=−0.37. 손실 이력 종목의 다음 거래
# −0.611% vs 이익 이력 −0.607%, 차이 0.004%p). 그 위에 규칙을 세우면 노이즈를
# 따라가는 것이다 — 1.5단계에서 랭킹 6종이 전부 무작위보다 나빴던 것과 같다.
#
# 대신 신호 유무로 판정한다. 실측: 매매 tier 40종목 중 16종목(40%)이 그날 신호
# 0건이었고, 매매 tier **밖에서 나온 신호는 0건**이었다 — 자리가 모자라 놓친
# 기회는 없고 앉아 있는 종목이 신호를 안 낸다.

OLD = NOW - timedelta(hours=12)          # 침묵 상한(390분)을 넘긴 체류
RECENT = NOW - timedelta(minutes=30)     # 아직 기회를 다 쓰지 않음


def _rot(tier_codes, waiting=(), **kw):
    """매매 tier + 대기(수집) 후보로 판을 만든다."""
    tier = {c: TRADE for c in tier_codes} | {c: COLLECT for c in waiting}
    since = {c: LONG_AGO for c in tier}
    cands = [_c(code=c, score=0.9) for c in tier]
    return cands, _cur(tier=tier, since=since, **kw)


def test_침묵한_자리를_대기_후보에게_넘긴다():
    cands, cur = _rot(["000001", "000002"], waiting=["000009"],
                      trade_since={"000001": OLD, "000002": OLD},
                      last_signal={"000002": NOW - timedelta(minutes=5)})
    rows = _plan(cands, cur, max_trade=2)
    assert [r["code"] for r in _by(rows, "demote")] == ["000001"]
    assert "침묵" in _by(rows, "demote")[0]["reason"]


def test_자리가_남으면_회전하지_않는다():
    """비워 봐야 순손실이다 — 경쟁이 있을 때만 돈다."""
    cands, cur = _rot(["000001"], waiting=["000009"],
                      trade_since={"000001": OLD})
    assert _by(_plan(cands, cur, max_trade=10), "demote") == []


def test_대기_후보가_없으면_회전하지_않는다():
    cands, cur = _rot(["000001", "000002"],
                      trade_since={"000001": OLD, "000002": OLD})
    assert _by(_plan(cands, cur, max_trade=2), "demote") == []


def test_자리를_받은_직후에는_침묵으로_치지_않는다():
    """기회를 다 쓰기 전에 빼면 탐색이 아니라 학대다."""
    cands, cur = _rot(["000001", "000002"], waiting=["000009"],
                      trade_since={"000001": RECENT, "000002": RECENT})
    assert _by(_plan(cands, cur, max_trade=2), "demote") == []


def test_신호를_낸_종목은_침묵이_아니다():
    cands, cur = _rot(["000001", "000002"], waiting=["000009"],
                      trade_since={"000001": OLD, "000002": OLD},
                      last_signal={"000001": NOW - timedelta(minutes=5),
                                   "000002": NOW - timedelta(minutes=5)})
    assert _by(_plan(cands, cur, max_trade=2), "demote") == []


def test_자리를_받기_전에_낸_신호는_안_쳐준다():
    """이전 체류의 신호로 이번 자리를 정당화하면 안 된다."""
    cands, cur = _rot(["000001", "000002"], waiting=["000009"],
                      trade_since={"000001": OLD, "000002": OLD},
                      last_signal={"000001": OLD - timedelta(hours=1)})
    assert [r["code"] for r in _by(cands and _plan(cands, cur, max_trade=2),
                                   "demote")] == ["000001"]


def test_보유는_회전_대상이_아니다():
    """매매 tier 에서 내리면 분봉 백필에서 빠져 청산 감시 폴백이 낡는다."""
    cands, cur = _rot(["000001", "000002"], waiting=["000009"],
                      held={"000001"},
                      trade_since={"000001": OLD, "000002": OLD})
    assert [r["code"] for r in _by(_plan(cands, cur, max_trade=2),
                                   "demote")] == ["000002"]


def test_회전은_사이클당_상한이_있다():
    """소스 장애로 대량 회전하는 것을 막는다 — MAX_SHRINK 와 같은 성격."""
    codes = [f"0000{i:02d}" for i in range(1, 6)]
    cands, cur = _rot(codes, waiting=["000091", "000092", "000093", "000094"],
                      trade_since={c: OLD for c in codes})
    assert len(_by(_plan(cands, cur, max_trade=5, max_rotate=2), "demote")) == 2


def test_회전을_끌_수_있다():
    cands, cur = _rot(["000001", "000002"], waiting=["000009"],
                      trade_since={"000001": OLD, "000002": OLD})
    assert _by(_plan(cands, cur, max_trade=2, silent_dwell_min=0), "demote") == []


def test_밀려난_직후에는_재승격되지_않는다():
    """쿨다운이 없으면 다음 사이클에 곧장 복귀해 회전이 무의미해진다."""
    cur = _cur(tier={"000001": COLLECT}, since={"000001": LONG_AGO},
               demoted_at={"000001": NOW - timedelta(minutes=10)})
    assert _by(_plan([_c(code="000001")], cur), "promote_trade") == []


def test_쿨다운이_지나면_다시_올라온다():
    cur = _cur(tier={"000001": COLLECT}, since={"000001": LONG_AGO},
               demoted_at={"000001": NOW - timedelta(hours=12)})
    assert len(_by(_plan([_c(code="000001")], cur), "promote_trade")) == 1


def test_상한_초과_강등도_침묵부터_뺀다():
    """점수는 '여전히 눈에 띄는가' 이지 '값어치가 있는가' 가 아니다."""
    cands, cur = _rot(["000001", "000002"],
                      trade_since={"000001": OLD, "000002": OLD},
                      last_signal={"000002": NOW - timedelta(minutes=5)})
    # 000002 가 점수는 같지만 신호를 냈다 → 침묵한 000001 이 먼저 밀린다
    rows = _plan(cands, cur, max_trade=1)
    assert [r["code"] for r in _by(rows, "demote")] == ["000001"]
