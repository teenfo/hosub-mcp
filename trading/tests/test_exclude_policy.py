"""제외 정책 단일화 — 같은 종목을 두 경로가 다르게 판정하던 결함의 회귀 방지.

2026-07-27 실측 손실. `signals/scanner._is_excluded` 는 대소문자 정규화가 없고
`KOACT`·`액티브` 키워드도 없어 ETF 를 통과시켰고, `discovery.is_excluded` 는
같은 종목을 차단했다. 그 틈으로 **462900 KoAct 바이오헬스케어액티브**가
실매수됐다(orb, 09:22 13,815 → 12:51 13,600, −1.84%).

반대 방향 오탐도 있었다 — scanner 는 `리츠`를 부분일치로 봐서 `메리츠금융지주`
같은 정상 종목을 배제했다.

여기서 지키는 것은 "두 경로가 항상 같은 답을 낸다" 이다.
"""
import pytest

from app.scout import nightly as discovery
from app.data import exclude
from app.signals import scanner


def _both(name):
    """두 경로의 판정을 함께 돌려준다 — 어긋나면 그 자체가 결함이다."""
    return scanner._is_excluded(name), discovery.is_excluded(name, {})


# --- ① 두 경로가 같은 함수를 쓴다 ---

def test_scanner_and_discovery_share_one_policy():
    assert scanner._is_excluded is exclude.is_excluded
    assert discovery.is_excluded is exclude.is_excluded


@pytest.mark.parametrize("name", [
    "KoAct 바이오헬스케어액티브", "KODEX 레버리지", "TIGER 미국나스닥100",
    "삼성전자", "메리츠금융지주", "신일전자", "삼성전자우", "현대차2우B",
])
def test_two_paths_never_disagree(name):
    a, b = _both(name)
    assert a == b, f"{name}: scanner={a} discovery={b}"


# --- ② 실제 손실을 낸 종목이 차단된다 ---

def test_blocks_the_etf_that_was_actually_bought():
    """2026-07-27 실매수 건. 대소문자·공백 변형 전부 막아야 한다."""
    for variant in ("KoAct 바이오헬스케어액티브", "KOACT 바이오헬스케어액티브",
                    "koact바이오헬스케어액티브", "KoAct바이오헬스케어액티브"):
        assert exclude.is_excluded(variant), variant


@pytest.mark.parametrize("name", [
    "KODEX 200", "TIGER 차이나전기차SOLACTIVE", "KBSTAR 머니마켓액티브",
    "ARIRANG 고배당주", "PLUS 고배당주", "히어로즈 CD금리액티브",
    "삼성 인버스 2X", "미래에셋 레버리지", "KODEX 국채선물10년",
    "하나금융15호스팩", "TIGER 미국채30년커버드콜", "삼성 KODEX ETN",
])
def test_blocks_non_common_stocks(name):
    assert exclude.is_excluded(name), name


@pytest.mark.parametrize("name", [
    "삼성전자우", "현대차2우B", "LG화학우", "아모레퍼시픽우",
    "대신증권우(전환)", "CJ제일제당 우선주",
])
def test_blocks_preferred_shares(name):
    """등락률 상위에 저유동 우선주가 잘 걸린다 — scanner 가 갖고 있던 규칙 보존."""
    assert exclude.is_excluded(name), name


# --- ③ 정상 종목을 막지 않는다 (반대 방향 오탐) ---

@pytest.mark.parametrize("name", [
    "메리츠금융지주", "메리츠증권", "메리츠화재",     # '리츠' 부분일치 함정
    "삼성전자", "SK하이닉스", "현대차", "신일전자", "대한항공",
    "우리금융지주", "우리기술",                       # '우' 로 시작하지만 우선주 아님
    "삼성바이오로직스", "국도화학", "JB금융지주", "동서",
])
def test_does_not_block_normal_stocks(name):
    assert not exclude.is_excluded(name), name


def test_reits_matched_only_as_suffix():
    """접미로만 봐야 한다 — 부분일치로 두면 메리츠 계열이 통째로 날아간다."""
    assert exclude.is_excluded("ESR켄달스퀘어리츠")
    assert not exclude.is_excluded("메리츠금융지주")


# --- ④ 설정으로 덮을 수 있다 ---

def test_config_can_override_lists():
    cfg = {"exclude_keywords": ["테스트전용"], "exclude_suffixes": [],
           "exclude_prefixes": []}
    assert exclude.is_excluded("테스트전용종목", cfg)
    assert not exclude.is_excluded("KODEX 레버리지", cfg)   # 목록을 통째로 대체


def test_default_cfg_reads_discovery_section():
    """cfg 를 안 주면 config.yaml nightly 섹션을 쓴다 — 설정 자리가 하나다."""
    assert exclude.is_excluded("KODEX 레버리지") is True


# --- ⑤ 방어 ---

@pytest.mark.parametrize("name", ["", None, "   "])
def test_empty_name_is_not_excluded(name):
    assert not exclude.is_excluded(name)


def test_filters_actually_use_the_policy():
    """필터 함수들이 정책을 실제로 호출하는지 — 배선이 끊기면 잡는다."""
    from app import settings

    items = [{"code": "462900", "name": "KoAct 바이오헬스케어액티브", "price": 13_800,
              "change_pct": 5.0, "trade_value": 99_999_999, "volume": 500_000},
             {"code": "005930", "name": "삼성전자", "price": 70_000,
              "change_pct": 5.0, "trade_value": 99_999_999, "volume": 500_000}]
    cfg = {"min_change_pct": 3.0, "min_trade_value": 1, "min_price": 1_000,
           "top_n": 10, "trade_max_price": 1_000_000}
    before = dict(settings.WATCHLIST)
    settings.WATCHLIST.clear()
    try:
        out = scanner.filter_candidates(items, cfg)
        assert [x["code"] for x in out] == ["005930"]
        out = scanner.filter_gainers(items, cfg | {"min_trade_value_krw": 0})
        assert [x["code"] for x in out] == ["005930"]
    finally:
        settings.WATCHLIST.update(before)
