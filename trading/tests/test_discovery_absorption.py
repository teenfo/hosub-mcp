"""완전 통합(2026-08-01) 회귀 — 발굴·스캐너는 감시목록에 **직접 쓰지 않는다**.

종전 이 파일은 "full 이면 물러난다" 를 검증했다. 이제 물러날 경로 자체가
삭제됐으므로, 검증할 것은 **삭제가 유지되는가**다 — 누가 편의로 직접 쓰기를
되살리면 여기서 잡힌다. 편입은 발굴 엔진(scout)의 단일 통로다.
"""
import app.discovery as disc
from app.data import watchlist
from app.signals import scanner as scanner_mod


def test_감시목록에는_직접_쓰기_계열이_없다():
    """replace_* 가 하나라도 되살아나면 전량 교체·귀속 고정·tier 증발이 돌아온다."""
    for fn in ("replace_auto", "replace_scanned", "replace_active",
               "replace_gainers"):
        assert not hasattr(watchlist, fn), fn


def test_발굴에는_자동편입_경로가_없다():
    import inspect

    assert not hasattr(disc.Discovery, "apply_auto_watch")
    # 호출 형태로 검사한다 — 삭제 이력을 적은 주석까지 막을 이유는 없다
    assert "watchlist.replace_auto(" not in inspect.getsource(disc)


def test_스캐너는_필터만_하고_쓰지_않는다():
    import inspect
    src = inspect.getsource(scanner_mod)
    for fn in ("replace_active", "replace_gainers", "replace_scanned"):
        assert f"watchlist.{fn}(" not in src, fn


def test_엔진_모드_기본값은_full():
    """실서버가 3거래일 관찰을 거쳐 도달한 상태의 성문화 — 되돌아가면 잡는다."""
    from app import settings
    assert settings.CONFIG["scout"]["mode"] == "full"
