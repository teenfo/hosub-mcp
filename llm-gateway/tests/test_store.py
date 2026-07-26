from app.store import CANCELLED, FAILED, QUEUED, RUNNING, SUCCEEDED, Store


def _job(store, **kw):
    base = dict(service="alpha", role="fast", model="small", lane="interactive",
                prompt="hi", system=None)
    base.update(kw)
    return store.create_job(**base)


def test_create_and_get(store):
    j = _job(store)
    assert j["status"] == QUEUED
    assert j["attempts"] == 0
    got = store.get_job(j["id"])
    assert got["prompt"] == "hi"
    assert got["metadata"] == {}


def test_metadata_roundtrip(store):
    j = _job(store, metadata={"session_id": 123, "note": "한글"})
    assert store.get_job(j["id"])["metadata"] == {"session_id": 123, "note": "한글"}


def test_claim_is_atomic(store):
    j = _job(store)
    assert store.claim(j["id"]) is True
    assert store.claim(j["id"]) is False          # 두 번째는 실패
    got = store.get_job(j["id"])
    assert got["status"] == RUNNING and got["attempts"] == 1


def test_requeue_keeps_attempts(store):
    j = _job(store)
    store.claim(j["id"])
    store.requeue(j["id"])
    got = store.get_job(j["id"])
    assert got["status"] == QUEUED
    assert got["attempts"] == 1                    # 시도 횟수는 유지
    assert got["started_at"] is None


def test_finish_and_usage(store):
    j = _job(store)
    store.claim(j["id"])
    store.finish(j["id"], status=SUCCEEDED, response="결과")
    got = store.get_job(j["id"])
    assert got["status"] == SUCCEEDED and got["response"] == "결과"
    assert got["finished_at"]

    store.record_usage(service="alpha", role="fast", model="small",
                       eval_count=10, duration_ms=100, status=SUCCEEDED)
    summary = store.usage_summary()
    assert summary[0]["service"] == "alpha" and summary[0]["calls"] == 1


def test_recover_running_on_startup(store):
    j = _job(store)
    store.claim(j["id"])                            # running 상태로 크래시했다고 가정
    assert store.get_job(j["id"])["status"] == RUNNING

    recovered = store.recover_running()
    assert recovered == 1
    assert store.get_job(j["id"])["status"] == QUEUED


def test_queued_jobs_ordered_by_priority_then_time(store):
    a = _job(store, prompt="a")
    b = _job(store, prompt="b", priority=5)
    c = _job(store, prompt="c")
    ids = [j["id"] for j in store.queued_jobs("interactive")]
    assert ids[0] == b["id"]                        # 높은 우선순위 먼저
    assert ids[1:] == [a["id"], c["id"]]            # 같은 우선순위는 생성순


def test_queue_position(store):
    a = _job(store)
    b = _job(store)
    assert store.queue_position(a["id"]) == 0
    assert store.queue_position(b["id"]) == 1


def test_cancel_only_own_queued_job(store):
    j = _job(store)
    assert store.cancel(j["id"], "beta") is False   # 남의 잡
    assert store.cancel(j["id"], "alpha") is True
    assert store.get_job(j["id"])["status"] == CANCELLED

    k = _job(store)
    store.claim(k["id"])
    assert store.cancel(k["id"], "alpha") is False  # 실행 중은 취소 불가


def test_list_filters_by_service(store):
    _job(store, service="alpha")
    _job(store, service="beta")
    assert len(store.list_jobs(service="alpha")) == 1
    assert len(store.list_jobs()) == 2


def test_counts_by_status(store):
    a = _job(store)
    _job(store, lane="batch", role="heavy", model="big")
    store.claim(a["id"])
    counts = store.counts_by_status()
    assert counts["interactive"][RUNNING] == 1
    assert counts["batch"][QUEUED] == 1


def test_persistence_across_instances(tmp_path):
    path = tmp_path / "p.db"
    s1 = Store(path)
    j = s1.create_job(service="alpha", role="fast", model="small",
                      lane="interactive", prompt="살아남아라", system=None)
    # 새 인스턴스(재시작 시뮬레이션)에서도 잡이 보인다
    s2 = Store(path)
    assert s2.get_job(j["id"])["prompt"] == "살아남아라"
