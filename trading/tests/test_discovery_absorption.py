"""discovery·scanner 흡수 — 감시목록 소유권이 엔진으로 넘어가는 지점.

발굴 엔진과 기존 경로가 **둘 다 감시목록에 쓰면** 서로를 덮어쓴다. 그렇다고
전환 전에 기존 경로를 끄면 아무도 쓰지 않아 매매가 통째로 멈춘다.
그래서 소유권은 `scout.owns_watchlist()` 한 줄로 갈리고, 그 값은 `full` 모드에서만
참이다 — **전환과 흡수가 같은 순간에 일어난다.**

여기서 고정하는 것은 그 규약이다. 회수되는 것이 '감시목록 쓰기' 뿐이고
수집·필터는 계속 돈다는 것도 함께 못박는다.
"""
import pytest

from app.scout import engine as scout


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(scout, "STATE_FILE", tmp_path / "engine.json")
    return tmp_path


# --------------------------------------------------------------------------
# 소유권 규약
# --------------------------------------------------------------------------
@pytest.mark.parametrize("mode,owns", [("shadow", False), ("collect", False),
                                       ("full", True)])
def test_소유권은_full_에서만_넘어간다(state, mode, owns):
    scout.set_state(mode=mode)
    assert scout.owns_watchlist() is owns


def test_전환_전에는_기존_경로가_계속_쓴다(state):
    """물러나는 시점을 잘못 잡으면 감시목록에 아무도 쓰지 않는다 —
    매매가 통째로 멈추는 실패다."""
    scout.set_state(mode="shadow")
    assert scout.owns_watchlist() is False


# --------------------------------------------------------------------------
# discovery — 편입만 회수하고 수집은 남는다
# --------------------------------------------------------------------------
async def test_full_이면_발굴이_감시목록에_쓰지_않는다(state, monkeypatch):
    from app import discovery as disc
    from app.data import watchlist

    scout.set_state(mode="full")
    called = []
    monkeypatch.setattr(watchlist, "replace_auto", lambda *a: called.append(a))
    monkeypatch.setattr(watchlist, "notify", _noop)
    wrote = await disc.Discovery().apply_auto_watch([_pick()], _cfg())
    assert wrote is False and called == [], "엔진이 소유하면 발굴은 물러난다"


async def test_shadow_면_발굴이_그대로_쓴다(state, monkeypatch):
    """물러나는 시점을 잘못 잡으면 감시목록에 아무도 쓰지 않는다."""
    from app import discovery as disc
    from app.data import watchlist

    scout.set_state(mode="shadow")
    called = []
    monkeypatch.setattr(watchlist, "replace_auto", lambda *a: called.append(a))
    monkeypatch.setattr(watchlist, "notify", _noop)
    wrote = await disc.Discovery().apply_auto_watch([_pick()], _cfg())
    assert wrote is True and called, "전환 전에는 기존 경로가 감시목록을 유지한다"


async def test_auto_watch_가_꺼져_있으면_모드와_무관하다(state, monkeypatch):
    from app import discovery as disc
    from app.data import watchlist

    scout.set_state(mode="shadow")
    monkeypatch.setattr(watchlist, "replace_auto", lambda *a: None)
    monkeypatch.setattr(watchlist, "notify", _noop)
    assert await disc.Discovery().apply_auto_watch(
        [_pick()], _cfg(auto_watch=False)) is False


# --------------------------------------------------------------------------
# scanner — 같은 규약
# --------------------------------------------------------------------------
def test_full_이면_스캐너가_보류를_알린다(state, caplog):
    from app.signals import scanner

    scout.set_state(mode="full")
    with caplog.at_level("INFO"):
        assert scanner._engine_owns() is True
    assert any("엔진(full)이 소유" in r.message for r in caplog.records)


def test_shadow_면_스캐너가_계속_쓴다(state):
    from app.signals import scanner

    scout.set_state(mode="shadow")
    assert scanner._engine_owns() is False


# --------------------------------------------------------------------------
# 헬퍼
# --------------------------------------------------------------------------
async def _noop(*a, **k):
    return None


def _pick():
    return {"code": "005930", "name": "삼성전자", "close": 70_000, "score": 3,
            "reasons": ["거래량 급증"]}


def _cfg(**kw):
    return {"auto_watch": True, "auto_watch_n": 5} | kw
