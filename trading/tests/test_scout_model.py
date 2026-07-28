"""신호 규약 — 여섯 소스가 같은 언어로 말하는가.

여기서 지켜야 할 것은 "점수가 맞다"가 아니다. strength 에는 예측력이 없다는
것이 이미 실측됐다(2026-07-27, 발굴 점수 순 상위 N 이 매수가능 무작위보다
0.22R 나빴다). 지켜야 할 것은 **소스 간 비교 가능성**과 **한 번의 조회 실패가
목록을 지우지 못한다**는 것 둘이다.
"""
from datetime import UTC, datetime, timedelta

import pytest

from app.scout import model
from app.scout.model import Signal


def _sig(**kw) -> Signal:
    base = {"code": "005930", "name": "삼성전자", "source": model.VOLUME,
            "strength": 0.5}
    return Signal(**(base | kw))


# --- ① 정규화 — 단위가 다른 원시값을 0~1 로 ---

def test_rank_strength_endpoints():
    assert model.rank_strength(1, 10) == 1.0
    assert model.rank_strength(10, 10) == 0.0
    assert model.rank_strength(5, 10) == pytest.approx(0.5556, abs=1e-3)


def test_rank_strength_degenerate_total():
    """후보가 하나뿐인 날에도 터지지 않는다."""
    assert model.rank_strength(1, 1) == 1.0
    assert model.rank_strength(1, 0) == 1.0
    assert model.rank_strength(3, 1) == 0.0


def test_surge_strength_is_logarithmic():
    """300%와 400%의 차이보다 300%와 3000%의 차이가 커야 한다."""
    assert model.surge_strength(100) == 0.0        # 급증 아님
    assert model.surge_strength(50) == 0.0         # 감소는 0
    near = model.surge_strength(400) - model.surge_strength(300)
    far = model.surge_strength(3000) - model.surge_strength(2900)
    assert near > far > 0
    assert model.surge_strength(10_000) == 1.0     # 상한에서 포화


@pytest.mark.parametrize("surge", [214_510.95, 718_757.12, 8_738_190.0, 13_605_700.0])
def test_broken_surge_rate_scores_zero_not_maximum(surge):
    """분모가 깨진 값을 **최대 세기로 읽는 것**이 실제로 저지른 잘못이었다.

    실측 2026-07-28 09:00:25 개장 직후 값들이다 — NAVER 718,757%(등락률 0.00%),
    LG전자 8,738,190%. 포화 구간이 1.0 이라 쓰레기가 전부 만점을 받았고, 오늘
    presurge 신호 8건 중 5건이 세기 1.0 이었다. 급증률은 직전 기간 거래량 대비
    비율이라, 그 기간이 비어 있으면 비율이 폭발한다. '더 강한 신호' 가 아니다.
    """
    assert model.surge_strength(surge) == 0.0


def test_plausible_surges_still_pass():
    """상한이 정상 급증까지 자르면 소스를 죽인 것이다 — 같은 날 실측값으로 고정."""
    for surge in (315.86, 612.43, 925.26, 2621.95, 5185.13):
        assert model.surge_strength(surge) > 0.0


def test_score_and_news_strength():
    assert model.score_strength(3) == 1.0
    assert model.score_strength(0) == 0.0
    assert model.score_strength(2) == pytest.approx(2 / 3)
    assert model.news_strength(100) == 1.0
    assert model.news_strength(60) == pytest.approx(0.6)


def test_strength_is_clamped():
    """어떤 소스가 범위를 벗어난 값을 줘도 합산이 깨지면 안 된다."""
    assert _sig(strength=5.0).strength == 1.0
    assert _sig(strength=-3.0).strength == 0.0
    assert _sig(strength=float("nan")).strength == 0.0


def test_unknown_source_rejected():
    """오타 난 소스명이 조용히 새 그룹을 만들면 취합이 무너진다."""
    with pytest.raises(ValueError):
        _sig(source="없는소스")


# --- ② 그룹 — 같은 정보를 세 번 세지 않기 위한 구분 ---

def test_intraday_sources_share_a_group():
    """거래대금·등락률·급등조짐은 같은 팩터의 세 가지 뷰다.

    실제로 filter_candidates 와 filter_gainers 는 둘 다 change_pct 로 정렬한다.
    합산하면 이벤트 스터디가 잡아낸 베타 노출을 3배로 증폭한다.
    """
    groups = {_sig(source=s).group for s in
              (model.VOLUME, model.GAINERS, model.PRESURGE)}
    assert groups == {"intraday"}


def test_independent_sources_have_distinct_groups():
    assert _sig(source=model.NIGHTLY).group == "daily"
    assert _sig(source=model.NEWS).group == "news"
    assert _sig(source=model.MANUAL).group == "human"


def test_every_source_has_a_group():
    assert set(model.GROUP) == set(model.SOURCES)
    assert set(model.GROUP.values()) <= set(model.GROUPS)


# --- ③ TTL — "신호가 없다" 와 "아직 확인 못 했다" 는 다른 상태다 ---

def test_ttl_defaults_come_from_source():
    """호출자가 안 줘도 소스별 기본 유효기간이 붙는다."""
    assert _sig(source=model.VOLUME).ttl_sec == model.TTL_SEC[model.VOLUME]
    assert _sig(source=model.NIGHTLY).ttl_sec == model.TTL_SEC[model.NIGHTLY]


def test_signal_expires_after_ttl():
    now = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)
    s = _sig(observed_at=now, ttl_sec=180)
    assert s.alive(now + timedelta(seconds=179))
    assert not s.alive(now + timedelta(seconds=181))
    assert s.effective(now + timedelta(seconds=181)) == 0.0


def test_ttl_survives_one_failed_poll():
    """스캔 주기(60초)의 2~3배라야 1회 실패가 목록을 지우지 못한다.

    지금은 API 1회 실패 → 필터가 빈 리스트 → replace_* 가 tier 전체를 지운다.
    """
    for src in (model.VOLUME, model.GAINERS, model.PRESURGE):
        assert model.TTL_SEC[src] >= 120


def test_manual_never_expires():
    """사용자가 직접 지정한 종목은 만료되지 않는다."""
    s = _sig(source=model.MANUAL, strength=1.0,
             observed_at=datetime(2020, 1, 1, tzinfo=UTC))
    assert s.ttl_sec == 0
    assert s.alive()
    assert s.effective() == 1.0        # 감쇠도 없다


# --- ④ 감쇠 — 장중 순위는 빨리 늙고 공시는 천천히 늙는다 ---

def test_decay_halves_at_half_life():
    assert model.decay(0, 600) == 1.0
    assert model.decay(600, 600) == pytest.approx(0.5)
    assert model.decay(1200, 600) == pytest.approx(0.25)


def test_decay_disabled_when_half_life_zero():
    assert model.decay(10_000, 0) == 1.0


def test_intraday_decays_faster_than_news():
    assert model.HALF_LIFE_SEC[model.VOLUME] < model.HALF_LIFE_SEC[model.NEWS]
    assert model.HALF_LIFE_SEC[model.NEWS] < model.HALF_LIFE_SEC[model.NIGHTLY]


def test_half_life_never_exceeds_ttl():
    """반감기가 TTL 보다 길면 감쇠가 있으나 마나 해진다.

    장중 소스가 600초 반감기 + 180초 TTL 이던 초안에서는 만료 직전 감쇠가
    0.81 에 그쳤다 — 사실상 계단 함수였다. 그러면 폴이 끊겼을 때 목록이
    서서히 흐려지는 게 아니라 깜빡 꺼진다.
    """
    for src in model.SOURCES:
        ttl, half = model.TTL_SEC[src], model.HALF_LIFE_SEC[src]
        if ttl > 0:
            assert 0 < half <= ttl, f"{src}: 반감기 {half} > TTL {ttl}"


def test_missed_polls_fade_instead_of_blinking_out():
    """폴 1~2회를 놓쳐도 신호가 남되 약해져야 한다."""
    now = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)
    s = _sig(strength=1.0, observed_at=now)
    one, two = now + timedelta(seconds=60), now + timedelta(seconds=120)
    assert 0.7 < s.effective(one) < 1.0
    assert 0.5 < s.effective(two) < s.effective(one)
    assert s.effective(now + timedelta(seconds=200)) == 0.0     # 3회째엔 만료


def test_effective_combines_strength_and_decay():
    now = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)
    s = _sig(strength=0.8, observed_at=now)
    s2 = _sig(strength=0.8, source=model.NEWS, observed_at=now)
    half = model.HALF_LIFE_SEC[model.NEWS]
    assert s.effective(now) == pytest.approx(0.8)
    assert s2.effective(now + timedelta(seconds=half)) == pytest.approx(0.4)


def test_age_never_negative():
    """시계가 뒤로 가도(NTP 보정) 음수 나이로 감쇠가 1을 넘지 않게."""
    now = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)
    s = _sig(observed_at=now + timedelta(seconds=30))
    assert s.age_sec(now) == 0.0
    assert s.effective(now) == pytest.approx(s.strength)
