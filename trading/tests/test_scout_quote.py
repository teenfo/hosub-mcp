"""승격 직전 현재가 실측 — 게이트가 낡은 값으로 판정하지 않게.

## 무엇을 고쳤나 (2026-07-28 실측)

매매 tier 승격은 `trade_tier_cap` 체결가능성 게이트로 결정한다. 그런데 그
게이트가 보는 `Candidate.price` 는 소스마다 신선도가 다르다.

  volume/gainers/presurge  스캔 응답의 현재가   — 60초 이내
  nightly                  picks["close"]       — **발굴 배치일 종가**
  news                     TNM 항목 가격        — 수집 시점

펌텍코리아 승격 결정에 `체결가능 48,600원` 이라 적혀 있었고 그 시각 실제
현재가는 **47,050원(−3.19%)** 이었다. 48,600 은 7/27 종가다. 오늘 71번의
승격 판단을 전부 어제 가격으로 했다.

**예측이 아니라 판정 재료의 신선도 문제**다. 1.5단계가 남기기로 한
"유동성·체결가능성 게이트" 의 정확성 그 자체다.

## 체결강도는 기록만 한다

같은 호출(ka10006)이 `cntr_str` 를 공짜로 준다. 어느 소스도 안 쓰는 새
정보라 좋은지 나쁜지 **모른다**. 승격 조건에 넣으면 발굴 3규칙 점수가 저지른
일을 반복하는 것이다 — 그것도 '그럴듯해서' 넣었고 1년 뒤 베타 프록시로
판명됐다. 원장에 싣기만 하고 4주 뒤 측정에서 처음 묻는다.
"""
from datetime import UTC, datetime

import pytest

from app import settings
from app.kiwoom.quote import parse_quote
from app.scout import promote
from app.scout.promote import COLLECT, Current
from app.scout.scoring import Candidate

NOW = datetime(2026, 7, 28, 1, 0, tzinfo=UTC)      # 10:00 KST — 장중


@pytest.fixture(autouse=True)
def _cap(monkeypatch):
    monkeypatch.setattr(settings, "tradable_price_cap", lambda default: 30_000.0)


def _c(sources, price=10_000, quote=None, code="000001"):
    return Candidate(code=code, name="테스트", score=0.9, price=price,
                     sources=list(sources), by_group={"intraday": 0.9},
                     quote=quote)


def _collect(code="000001"):
    """수집 tier 에 이미 있고 체류를 마친 상태 — 매매 승격 직전."""
    return Current(tier={code: COLLECT}, since={}, protected=frozenset(),
                   names={})


def _plan(cands, cur=None, **over):
    return promote.plan(cands, cur or _collect(), NOW, promote.DEFAULTS | over)


def _trade(rows):
    return [r for r in rows if r["action"] == "promote_trade"]


# --- ① ka10006 파싱 ---

def _raw(price="-47050", flu="-3.19", strength="118.53"):
    """실응답 구조 — 하락이면 close_pric 에 **부호가 붙어** 온다."""
    return {"return_code": 0, "close_pric": price, "flu_rt": flu,
            "cntr_str": strength, "trde_qty": "19878"}


def test_quote_takes_absolute_price_and_signed_change():
    q = parse_quote(_raw())
    assert q["price"] == 47_050          # 부호는 가격이 아니라 방향이다
    assert q["change_pct"] == -3.19
    assert q["cntr_str"] == 118.53


def test_quote_returns_none_on_failure_not_zero():
    """실패를 '가격 0' 으로 돌려주면 '못 산다' 와 '모른다' 가 섞인다."""
    assert parse_quote({"return_code": "1", "return_msg": "오류"}) is None
    assert parse_quote(_raw(price="0")) is None


# --- ② 신선도 판정 ---

@pytest.mark.parametrize("sources,fresh", [
    (["volume"], True), (["gainers"], True), (["presurge"], True),
    (["nightly"], False), (["news"], False), (["manual"], False),
    (["nightly", "volume"], True),          # 하나라도 장중이면 현재가가 실려 있다
    ([], False),                            # 알 수 없으면 낡은 것으로 본다
])
def test_price_freshness_by_source(sources, fresh):
    assert _c(sources).price_fresh is fresh


def test_measured_quote_makes_any_candidate_fresh():
    assert _c(["nightly"], quote={"price": 47_050}).price_fresh is True


def test_measured_quote_wins_over_the_stale_price():
    c = _c(["nightly"], price=48_600, quote={"price": 47_050})
    assert c.live_price == 47_050


# --- ③ 게이트 — 이것이 이번 작업의 목적이다 ---

def test_stale_only_candidate_is_not_promoted_to_trade():
    """실측을 못 받았으면 이번 사이클은 넘긴다. 30초 뒤 다시 온다."""
    rows = _plan([_c(["nightly"], price=10_000)])
    assert _trade(rows) == []


def test_stale_candidate_promotes_once_the_quote_arrives():
    rows = _plan([_c(["nightly"], price=10_000, quote={"price": 10_100})])
    assert len(_trade(rows)) == 1
    assert "10,100원" in _trade(rows)[0]["reason"]     # 실측값이 사유에 남는다


def test_stale_price_still_blocks_promotion():
    """가격 상한은 없어졌지만 **'낡은 값으로 판단하지 않는다'** 는 남는다.

    가격 자체를 상한과 비교하지 않게 됐어도, 실측 시세를 못 받은 상태로 매매를
    여는 것은 여전히 위험하다 — 승격 결정의 근거(등락률·체결강도)가 전부 그
    시세에 실려 있다. 실측이 오면 가격이 얼마든 열린다.
    """
    stale = _c(["nightly"], price=29_000)
    assert _trade(_plan([stale])) == []                       # 실측 없으면 보류
    stale.quote = {"price": 310_000}
    assert len(_trade(_plan([stale]))) == 1                    # 실측이 오면 가격 무관


def test_intraday_candidate_still_promotes_without_a_quote():
    """장중 소스는 스캔 응답의 현재가를 싣는다 — 추가 호출을 요구하지 않는다."""
    assert len(_trade(_plan([_c(["volume"], price=10_000)]))) == 1


# --- ④ 관측값은 기록만 된다 ---

def test_quote_fields_are_recorded_on_the_decision():
    rows = _plan([_c(["volume"], quote={"price": 10_000, "change_pct": -3.19,
                                        "cntr_str": 118.53})])
    d = _trade(rows)[0]
    assert d["change_pct"] == -3.19 and d["cntr_str"] == 118.53


def test_execution_strength_does_not_change_the_decision():
    """**측정 전에는 판단에 쓰지 않는다.** 체결강도가 바닥이어도 승격은 그대로다.

    이 테스트가 곧 규약이다 — 나중에 게이트로 올리려면 여기를 고쳐야 하고,
    그때는 4주 측정 결과가 있어야 한다.
    """
    weak = _plan([_c(["volume"], quote={"price": 10_000, "cntr_str": 20.0})])
    strong = _plan([_c(["volume"], quote={"price": 10_000, "cntr_str": 200.0})])
    assert [r["action"] for r in weak] == [r["action"] for r in strong]


def test_change_pct_does_not_change_the_decision():
    """등락률도 마찬가지다. 이미 장중 소스 필터에서 세 번 쓰인다 —
    승격에서 또 보면 같은 팩터를 네 번째로 세는 것이다."""
    down = _plan([_c(["volume"], quote={"price": 10_000, "change_pct": -20.0})])
    up = _plan([_c(["volume"], quote={"price": 10_000, "change_pct": 20.0})])
    assert [r["action"] for r in down] == [r["action"] for r in up]


def test_decision_without_a_quote_carries_no_observation_fields():
    """실측이 없으면 빈 값을 지어내지 않는다 — 0 과 '모름' 은 다르다."""
    d = _plan([_c(["volume"])])[0]
    assert "change_pct" not in d and "cntr_str" not in d


# --- 시세 조회를 한 콜로 묶는다 (2026-07-30 실측 대응) ---

def test_watch_info_parses_to_the_same_shape_as_quote():
    """단건(ka10006)과 묶음(ka10095)의 반환 모양이 같아야 한다.

    다르면 호출부에 분기가 생기고, 그 분기가 곧 '어느 쪽이 맞나' 문제가 된다.
    """
    from app.kiwoom.quote import parse_watch_info

    got = parse_watch_info({"return_code": 0, "atn_stk_infr": [
        {"stk_cd": "005930", "stk_nm": "삼성전자", "cur_prc": "-208500",
         "flu_rt": "-5.23", "cntr_str": "107.0", "trde_qty": "65555523",
         "trde_prica": "13726155"},
        {"stk_cd": "000660", "stk_nm": "SK하이닉스", "cur_prc": "+1401000",
         "flu_rt": "+2.10", "cntr_str": "105.8", "trde_qty": "1000",
         "trde_prica": "17560826"},
    ]})
    assert set(got) == {"005930", "000660"}
    # 가격의 +/- 는 전일대비 방향이다 — 절댓값으로 읽어야 한다
    assert got["005930"]["price"] == 208500 and got["005930"]["change_pct"] == -5.23
    assert got["000660"]["price"] == 1401000
    assert got["005930"]["cntr_str"] == 107.0
    assert got["005930"]["trde_prica"] == 13726155


def test_watch_info_skips_unusable_rows():
    """가격 0·코드 없음은 '조회됨' 으로 세면 안 된다 — 승격 게이트가 오판한다."""
    from app.kiwoom.quote import parse_watch_info

    got = parse_watch_info({"return_code": 0, "atn_stk_infr": [
        {"stk_cd": "999999", "stk_nm": "", "cur_prc": "0"},
        {"stk_cd": "", "cur_prc": "1000"},
        {"stk_cd": "005930", "stk_nm": "삼성전자", "cur_prc": "-208500"},
    ]})
    assert list(got) == ["005930"]


def test_watch_info_error_is_empty_not_partial():
    from app.kiwoom.quote import parse_watch_info

    assert parse_watch_info({"return_code": 1, "return_msg": "오류"}) == {}
    assert parse_watch_info({}) == {}


def test_chunking_respects_the_measured_limit():
    """89종목은 실측했고 그 위는 안 재봤다 — 모르는 한도에 기대지 않는다."""
    from app.scout.engine import WATCH_INFO_CHUNK, _chunks

    assert WATCH_INFO_CHUNK <= 89
    codes = [f"{i:06d}" for i in range(200)]
    parts = list(_chunks(codes, WATCH_INFO_CHUNK))
    assert sum(len(p) for p in parts) == 200
    assert all(len(p) <= WATCH_INFO_CHUNK for p in parts)
