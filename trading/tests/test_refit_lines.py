"""체결 후 손절폭 재검증 — 슬리피지가 대역 밖으로 민 폭을 되돌린다.

신호 단계의 대역 검사는 **모델 진입가**로 하고, 체결가는 그 뒤에 정해진다.
실측 2026-07-27~31: 107건 중 17건이 체결 후 1.0~2.5% 밖(최대 0.94%p 이탈).
"""
import pytest

from app.trade.ledger import refit_lines

LO, HI = 1.0, 2.5


def test_대역_안이면_손대지_않는다():
    """구조적 손절선(레인지 저점 등)을 이유 없이 옮기지 않는다."""
    # 모델 10,000 · 손절 9,820(1.8%) → 체결 10,020, 폭 2.00% — 여전히 대역 안
    assert refit_lines("long", 10_000, 10_020, 9_820, 10_270, LO, HI) is None


def test_슬리피지가_상한을_넘기면_경계로_당긴다():
    # 손절 9,750(2.5%) 인데 10,100 에 체결 → 폭 3.47% (상한 초과)
    stop, target = refit_lines("long", 10_000, 10_100, 9_750, 10_375, LO, HI)
    assert round(abs(10_100 - stop) / 10_100 * 100, 2) == HI
    # 원 설계 손익비 R = 375/250 = 1.5 가 유지된다
    assert round((target - 10_100) / (10_100 - stop), 2) == 1.5


def test_슬리피지가_하한을_밑돌면_경계로_넓힌다():
    # 손절 9,880(1.2%) 인데 9,950 에 체결 → 폭 0.70% (하한 미달, 비용을 못 이긴다)
    stop, target = refit_lines("long", 10_000, 9_950, 9_880, 10_180, LO, HI)
    assert round(abs(9_950 - stop) / 9_950 * 100, 2) == LO


def test_숏은_방향이_뒤집힌다():
    # 숏: 손절이 진입 위, 목표가 진입 아래
    stop, target = refit_lines("short", 10_000, 9_900, 10_250, 9_625, LO, HI)
    assert stop > 9_900 and target < 9_900
    assert round(abs(9_900 - stop) / 9_900 * 100, 2) == HI


def test_체결가가_이미_손절선을_넘었으면_옮기지_않는다():
    """즉시 청산될 자리를 '한 번 더 잃을 자리' 로 바꾸지 않는다."""
    # 롱인데 손절선(9,800) 아래인 9,700 에 체결됐다
    assert refit_lines("long", 10_000, 9_700, 9_800, 10_300, LO, HI) is None
    # 숏도 대칭
    assert refit_lines("short", 10_000, 10_300, 10_200, 9_700, LO, HI) is None


def test_대역이_꺼져_있으면_아무것도_하지_않는다():
    assert refit_lines("long", 10_000, 10_100, 9_750, 10_375, 0, 0) is None


def test_목표가_없으면_폭만_고친다():
    stop, target = refit_lines("long", 10_000, 10_100, 9_750, 0, LO, HI)
    assert round(abs(10_100 - stop) / 10_100 * 100, 2) == HI
    assert target == 0        # 복원할 R 이 없으므로 건드리지 않는다


@pytest.mark.parametrize("bad", [
    ("long", 0, 10_100, 9_750, 10_375),      # 모델가 없음
    ("long", 10_000, 0, 9_750, 10_375),      # 체결가 없음
    ("long", 10_000, 10_100, 0, 10_375),     # 손절선 없음
])
def test_재료가_없으면_None(bad):
    assert refit_lines(*bad, LO, HI) is None
