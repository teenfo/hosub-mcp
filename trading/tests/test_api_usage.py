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


# --- 429 자동 감속(적응형 스로틀) ---

def test_penalize_halves_rps():
    lim = RateLimiter(max_rps=4)
    assert lim.effective_rps == 4 and not lim.throttled
    lim.penalize()
    assert lim.effective_rps == 2 and lim.throttled and lim.penalties == 1
    lim.penalize()
    assert lim.effective_rps == 1
    for _ in range(5):
        lim.penalize()
    assert lim.effective_rps == lim.MIN_RPS          # 하한 아래로는 안 내려감


def test_interval_follows_effective_rps():
    lim = RateLimiter(max_rps=4)
    assert lim.interval == 0.25
    lim.penalize()
    assert lim.interval == 0.5                       # 2 rps → 0.5초 간격


def test_recovers_after_quiet_period():
    lim = RateLimiter(max_rps=4)
    lim.penalize()                                   # 2 rps
    lim._maybe_recover()                             # 아직 회복 시간 전
    assert lim.effective_rps == 2
    lim.throttled_until = 0                          # 무사고 구간 경과 시뮬
    lim._maybe_recover()
    assert lim.effective_rps == 2.5                  # 단계적 회복
    for _ in range(10):
        lim.throttled_until = 0
        lim._maybe_recover()
    assert lim.effective_rps == 4 and not lim.throttled   # 상한까지 원복


def test_usage_snapshot_reports_throttle():
    lim = RateLimiter(max_rps=4)
    lim.penalize()
    u = ApiUsage()
    u.record(429)
    snap = u.snapshot(lim)
    assert snap["auto_throttled"] is True
    assert snap["max_rps"] == 2 and snap["configured_rps"] == 4
    assert snap["penalties"] == 1 and snap["rate_limited_1h"] == 1
