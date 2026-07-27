"""키움 API 호출 계측 — 부하 표시용 스냅샷."""
import time

from app.kiwoom.client import ApiUsage, RateLimiter


def test_counts_and_rps():
    u = ApiUsage()
    for _ in range(20):
        u.record(200)
    snap = u.snapshot(RateLimiter(max_rps=4))
    assert snap["calls_1m"] == 20 and snap["calls_1h"] == 20
    assert snap["calls_total"] == 20
    assert snap["rps_10s"] == 2.0                 # 20건/10초
    assert snap["max_rps"] == 4 and snap["usage_pct"] == 50
    assert snap["errors_1h"] == 0 and snap["last_error"] == ""


def test_errors_and_rate_limit():
    u = ApiUsage()
    u.record(200)
    u.record(429)
    u.record(500)
    snap = u.snapshot(RateLimiter())
    assert snap["errors_1h"] == 2
    assert snap["rate_limited_1h"] == 1 and snap["rate_limited_total"] == 1
    assert snap["last_error"] == "HTTP 500"


def test_window_prunes_old_calls():
    u = ApiUsage(window_sec=1)
    u._calls.append(time.time() - 10)             # 창 밖 과거 호출
    u.record(200)
    snap = u.snapshot(RateLimiter())
    assert snap["calls_1h"] == 1                  # 오래된 것은 제거
    assert snap["calls_total"] == 1


def test_throttle_wait_reported():
    lim = RateLimiter(max_rps=4)
    lim.waited_sec = 3.456
    assert ApiUsage().snapshot(lim)["throttle_wait_sec"] == 3.5


def test_snapshot_without_limiter():
    snap = ApiUsage().snapshot(None)
    assert snap["max_rps"] == 0 and snap["usage_pct"] == 0
