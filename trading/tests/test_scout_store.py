"""신호 원장 — 감시목록에서 분리해야 측정이 가능해진다.

지금은 원장과 투영이 같은 표라서, `replace_scanned` 가 이미 감시 중인 코드를
건너뛰는 순간(watchlist.py:167) 소스 귀속이 첫 소스로 굳는다. 그래서 "거래대금
상위가 가리킨 종목의 성적" 을 물을 수 없다. 여기서 지키는 것은 그 분리다.

  - 같은 종목을 두 소스가 가리키면 **둘 다 남는가**
  - 만료된 신호가 현재 점수에서 빠지는가
  - **shadow 에서도 결정이 기록되는가** — 적용 안 한 결정이야말로
    "엔진이라면 이렇게 했을 것" 의 기록이다
"""
from datetime import UTC, datetime, timedelta

import pytest

from app.scout import model, store
from app.scout.model import Signal


@pytest.fixture(autouse=True)
def _tmp_db(monkeypatch, tmp_path):
    """실 DB 를 건드리지 않는다 — 실거래 중인 서비스와 같은 디스크다."""
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "scout.db")


def _sig(code="005930", source=model.VOLUME, strength=0.5, at=None, **kw):
    return Signal(code=code, name=kw.pop("name", f"종목{code}"), source=source,
                  strength=strength,
                  observed_at=at or datetime.now(UTC), **kw)


# --- ① 원장은 덮어쓰지 않는다 ---

def test_two_sources_pointing_at_one_symbol_both_survive():
    """이것이 감시목록과 분리한 이유다 — 지금은 첫 소스로 귀속이 굳는다."""
    store.record([_sig(source=model.VOLUME), _sig(source=model.GAINERS)])
    got = {s.source for s in store.live()}
    assert got == {model.VOLUME, model.GAINERS}


def test_repeat_reports_keep_only_the_latest_per_source():
    """같은 소스가 60초마다 같은 종목을 보고한다 — 전부 세면 그 소스만 부푼다.

    원장에는 다 남기되(사후 측정용) 현재 점수 계산에는 최신 하나만 쓴다.
    """
    now = datetime.now(UTC)
    store.record([_sig(strength=0.2, at=now - timedelta(seconds=120)),
                  _sig(strength=0.9, at=now - timedelta(seconds=1))])
    live = store.live(now)
    assert len(live) == 1
    assert live[0].strength == pytest.approx(0.9)      # 최신


def test_history_is_retained_even_when_not_live():
    """만료된 신호도 원장에는 남아야 사후 측정이 된다."""
    old = datetime.now(UTC) - timedelta(hours=2)
    store.record([_sig(at=old)])
    assert store.live() == []                          # 현재 점수에는 안 들어감
    with store._conn() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM signals").fetchone()["c"] == 1


def test_expired_signals_drop_out_of_live():
    now = datetime.now(UTC)
    store.record([
        _sig(code="000001", at=now - timedelta(seconds=10)),      # TTL 180
        _sig(code="000002", at=now - timedelta(seconds=400)),     # 만료
    ])
    assert {s.code for s in store.live(now)} == {"000001"}


def test_manual_signal_never_expires():
    old = datetime.now(UTC) - timedelta(days=30)
    store.record([_sig(code="000003", source=model.MANUAL, strength=1.0, at=old)])
    assert {s.code for s in store.live()} == {"000003"}


def test_evidence_round_trips():
    store.record([_sig(kind="news:공급계약",
                       evidence={"title": "대규모 공급계약", "url": "http://x"},
                       raw=87.0, price=71_200.0)])
    s = store.live()[0]
    assert s.kind == "news:공급계약"
    assert s.evidence["title"] == "대규모 공급계약"
    assert s.raw == 87.0 and s.price == 71_200.0


def test_record_empty_is_safe():
    assert store.record([]) == 0
    assert store.live() == []


def test_purge_keeps_manual_and_drops_old():
    now = datetime.now(UTC)
    store.record([_sig(code="000001", at=now - timedelta(days=90)),
                  _sig(code="000002", source=model.MANUAL, at=now - timedelta(days=90)),
                  _sig(code="000003", at=now - timedelta(days=1))])
    store.purge(days=60, now=now)
    with store._conn() as conn:
        left = {r["code"] for r in conn.execute("SELECT code FROM signals")}
    assert left == {"000002", "000003"}


# --- ② 결정 이력 — 장중 소스를 처음으로 측정 가능하게 만드는 것 ---

def _dec(**kw):
    return {"code": "005930", "name": "삼성전자", "action": "promote_collect",
            "from_tier": None, "to_tier": "collect", "score": 0.71,
            "sources": [model.VOLUME, model.NEWS], "reason": "2개 소스 합의"} | kw


def test_shadow_decisions_are_recorded_but_flagged_unapplied():
    """적용하지 않은 결정이야말로 '엔진이라면 이렇게 했을 것' 의 기록이다."""
    store.log_decisions([_dec()], mode="shadow", applied=False)
    d = store.recent_decisions()[0]
    assert d["mode"] == "shadow" and d["applied"] == 0
    assert d["action"] == "promote_collect"


def test_decision_keeps_every_contributing_source():
    """사후 귀속의 입력 — 한 소스만 남기면 기여도 측정이 다시 불가능해진다."""
    store.log_decisions([_dec()], mode="full", applied=True)
    assert store.recent_decisions()[0]["sources"] == sorted([model.VOLUME, model.NEWS])


def test_decisions_are_newest_first():
    store.log_decisions([_dec(code="000001")], mode="shadow", applied=False)
    store.log_decisions([_dec(code="000002")], mode="shadow", applied=False)
    assert [d["code"] for d in store.recent_decisions()] == ["000002", "000001"]


def test_log_decisions_empty_is_safe():
    assert store.log_decisions([], mode="shadow", applied=False) == 0
    assert store.recent_decisions() == []


def test_missing_optional_fields_do_not_break():
    store.log_decisions([{"code": "000001", "action": "hold"}],
                        mode="shadow", applied=False)
    d = store.recent_decisions()[0]
    assert d["sources"] == [] and d["score"] is None


# --- ②-2 원장은 '바뀔 때만' 쌓인다 ---
#
# shadow 는 결정을 반영하지 않으므로 상태가 영원히 그대로고, 그대로 두면 30초마다
# 같은 결론이 다시 쌓인다. 실측 2026-07-28 09:00~09:36: 1,140행이 쌓였는데 고유
# 종목은 27개였다(펌텍코리아 promote_trade 71회 · 강등 10종목 각 71회).
# 이러면 4주 뒤 소스별 기여도 측정이 "한 번의 판단" 이 아니라 "그 판단을 몇
# 사이클 유지했는가" 로 가중된 표본을 읽는다.

def test_repeating_the_same_decision_does_not_grow_the_ledger():
    assert store.log_decisions([_dec()], mode="shadow", applied=False) == 1
    for _ in range(5):
        assert store.log_decisions([_dec()], mode="shadow", applied=False) == 0
    assert len(store.recent_decisions()) == 1


def test_score_drift_alone_is_not_a_new_decision():
    """점수는 감쇠로 매 사이클 조금씩 움직인다 — 그걸 새 결정으로 세면 무의미하다.

    실측에서 펌텍코리아 점수가 0.666 → 0.6649 로 흐르며 71행이 쌓였다.
    """
    store.log_decisions([_dec(score=0.666)], mode="shadow", applied=False)
    assert store.log_decisions([_dec(score=0.6649)], mode="shadow", applied=False) == 0
    assert len(store.recent_decisions()) == 1


def test_a_changed_decision_is_recorded():
    """중복만 지운다 — 판단이 바뀌면 반드시 남아야 한다."""
    store.log_decisions([_dec()], mode="shadow", applied=False)
    store.log_decisions([_dec(action="drop", from_tier="collect", to_tier="none")],
                        mode="shadow", applied=False)
    assert [d["action"] for d in store.recent_decisions()] == \
           ["drop", "promote_collect"]


def test_the_same_action_returns_after_something_else_happened():
    """승격 → 강등 → 재승격은 세 건이다. 직전과만 비교하기 때문에 성립한다."""
    seq = [_dec(),
           _dec(action="drop", from_tier="collect", to_tier="none"),
           _dec()]
    for d in seq:
        assert store.log_decisions([d], mode="shadow", applied=False) == 1
    assert len(store.recent_decisions()) == 3


def test_dedup_is_per_symbol():
    """한 종목이 조용하다고 다른 종목의 새 결정까지 막으면 안 된다."""
    store.log_decisions([_dec(code="000001")], mode="shadow", applied=False)
    got = store.log_decisions([_dec(code="000001"), _dec(code="000002")],
                              mode="shadow", applied=False)
    assert got == 1
    assert {d["code"] for d in store.recent_decisions()} == {"000001", "000002"}


# --- ③ 소스 상태 — 실패를 숨기지 않는다 ---

def test_consecutive_failures_accumulate_for_backoff():
    assert store.mark_fail(model.NEWS, "timeout") == 1
    assert store.mark_fail(model.NEWS, "timeout") == 2
    assert store.mark_fail(model.NEWS, "timeout") == 3


def test_success_resets_the_failure_count():
    store.mark_fail(model.VOLUME, "boom")
    store.mark_fail(model.VOLUME, "boom")
    store.mark_ok(model.VOLUME, 12)
    h = store.health()[model.VOLUME]
    assert h["fails"] == 0 and h["signals"] == 12 and h["last_ok"]


def test_error_message_is_kept_for_the_screen():
    store.mark_fail(model.GAINERS, "HTTP 500 " + "x" * 500)
    msg = store.health()[model.GAINERS]["error_msg"]
    assert msg.startswith("HTTP 500") and len(msg) <= 300


def test_health_empty_before_first_poll():
    assert store.health() == {}
