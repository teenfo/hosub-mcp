"""뉴스 영향 소급 측정 — LLM 이 매기는 두 필드가 수익률과 상관이 있는가.

`impact_horizon` 은 매 분석마다 만들어 저장하면서 **어떤 판정에도 쓰이지
않는다.** `impact_direction` 은 반대로 발굴 엔진의 news 어댑터가 악재를
걸러내는 데 쓴다 — 그 필터가 근거 있는지 재지 않으면 잡음을 필터라고 믿는 셈이다.

여기서 지키는 것:
  - 이벤트 시각(t0)이 **뉴스 발행 시각**이고, 진입은 그 다음 날 시가일 것
  - 시장 효과를 뺀 초과수익을 함께 낼 것 (뉴스는 시장이 움직인 날에 몰린다)
  - 봉이 없는 종목이 조용히 섞이지 않을 것
"""
from datetime import UTC, datetime

import pandas as pd
import pytest

from app import settings
from app.research import newsimpact as ni


# --- ① 이벤트 시각 ---

def test_event_date_uses_kst_publication_time():
    """t0 는 뉴스가 나온 시각이다. UTC 23:30 은 KST 로 다음 날이다."""
    assert ni.event_date({"published_at": "2026-07-27T10:00:00+09:00"}) == "2026-07-27"
    assert ni.event_date({"published_at": "2026-07-26T23:30:00+00:00"}) == "2026-07-27"


def test_event_date_handles_naive_and_broken():
    assert ni.event_date({"published_at": "2026-07-27 10:00:00"}) == "2026-07-27"
    assert ni.event_date({"published_at": "쓰레기"}) is None
    assert ni.event_date({}) is None


# --- ② 조인 — 진입은 다음 날 시가 ---

def _bars(dates, opens, closes):
    idx = pd.to_datetime(dates)
    return pd.DataFrame({"open": opens, "close": closes}, index=idx)


def _fwd(symbol="000001"):
    from app.research import panel as panel_mod

    df = _bars(["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06",
                "2026-07-07", "2026-07-08"],
               [100, 100, 110, 120, 130, 140],
               [100, 110, 120, 130, 140, 150])
    f = panel_mod.forward(df, horizons=(1, 3, 5)).reset_index(names="date")
    f["date"] = pd.to_datetime(f["date"]).dt.date.astype(str)
    f["symbol"] = symbol
    return f[["symbol", "date", *ni.TARGETS]]


def test_join_matches_event_date_to_forward_returns():
    items = [{"ticker": "000001", "published_at": "2026-07-01T10:00:00+09:00",
              "impact_horizon": "immediate", "impact_direction": "positive",
              "category": "공급계약", "score": 80}]
    df = ni.join(items, _fwd(), market={})
    assert len(df) == 1
    # t0=07-01 → 진입 07-02 시가 100 → 07-02 종가 110 = +10%
    assert df["fwd_1"].iloc[0] == pytest.approx(10.0)
    assert df["horizon"].iloc[0] == "immediate"


def test_excess_removes_the_market_move():
    """뉴스는 시장이 크게 움직인 날에 몰린다 — 그날 시장을 뉴스의 공으로 세지 않는다."""
    items = [{"ticker": "000001", "published_at": "2026-07-01T10:00:00+09:00",
              "impact_horizon": "short", "impact_direction": "positive"}]
    df = ni.join(items, _fwd(), market={"2026-07-01": 4.0})
    assert df["fwd_1"].iloc[0] == pytest.approx(10.0)     # 원시는 그대로 보고
    assert df["exc_fwd_1"].iloc[0] == pytest.approx(6.0)  # 초과수익만 규칙의 공


def test_symbols_without_bars_are_dropped_not_zeroed():
    """봉이 없는 종목을 0으로 채우면 없는 성적을 만들어 내는 것이다."""
    items = [{"ticker": "999999", "published_at": "2026-07-01T10:00:00+09:00",
              "impact_horizon": "long", "impact_direction": "positive"}]
    assert ni.join(items, _fwd(), market={}).empty


def test_join_skips_items_without_ticker_or_time():
    items = [{"ticker": "", "published_at": "2026-07-01T10:00:00+09:00"},
             {"ticker": "000001", "published_at": ""}]
    assert ni.join(items, _fwd(), market={}).empty


def test_join_empty_is_safe():
    assert ni.join([], pd.DataFrame(), {}).empty


# --- ③ 집계 ---

def _df(rows):
    """rows: [(horizon, direction, fwd_1, fwd_3, fwd_5)]"""
    out = pd.DataFrame([{
        "symbol": "000001", "date": f"2026-07-{i % 28 + 1:02d}",
        "horizon": h, "direction": d, "category": "공급계약", "score": 70,
        "fwd_1": a, "fwd_3": b, "fwd_5": c,
        "exc_fwd_1": a, "exc_fwd_3": b, "exc_fwd_5": c,
    } for i, (h, d, a, b, c) in enumerate(rows)])
    return out


def test_horizon_curve_shape_is_reported():
    """impact_horizon 이 재는 것은 크기가 아니라 1일→3일→5일 곡선의 모양이다."""
    rows = ([("immediate", "positive", 3.0, 3.0, 3.0)] * 40      # 1일에 끝남
            + [("long", "positive", 1.0, 2.0, 3.0)] * 40)         # 계속 쌓임
    out = ni.analyze(_df(rows))
    by = {b["key"]: b for b in out["by_horizon"]}
    assert by["immediate"]["fwd_1"] == 3.0 and by["immediate"]["fwd_5"] == 3.0
    assert by["long"]["fwd_1"] == 1.0 and by["long"]["fwd_5"] == 3.0
    assert all(b["reliable"] for b in out["by_horizon"])


def test_small_bucket_flagged_unreliable():
    out = ni.analyze(_df([("immediate", "positive", 1.0, 1.0, 1.0)] * 5))
    assert out["by_horizon"][0]["n"] == 5
    assert out["by_horizon"][0]["reliable"] is False
    assert ni.MIN_BUCKET >= 30


def test_direction_split_lets_us_check_the_news_adapter_filter():
    """발굴 엔진이 악재를 거르는 근거가 여기서 나온다."""
    rows = ([("short", "positive", 2.0, 2.0, 2.0)] * 40
            + [("short", "negative", -2.0, -2.0, -2.0)] * 40)
    out = ni.analyze(_df(rows))
    by = {b["key"]: b for b in out["by_direction"]}
    assert by["positive"]["exc_fwd_1"] == 2.0
    assert by["negative"]["exc_fwd_1"] == -2.0
    assert set(out["direction_ko"]) == set(ni.DIRECTIONS)


def test_unknown_bucket_values_are_kept_not_dropped():
    """LLM 이 스키마 밖 값을 내면 조용히 사라지면 안 된다 — 그것도 정보다."""
    out = ni.analyze(_df([("이상한값", "positive", 1.0, 1.0, 1.0)] * 3))
    assert [b["key"] for b in out["by_horizon"]] == ["이상한값"]


def test_analyze_empty_is_safe():
    assert ni.analyze(pd.DataFrame()) == {"rows": 0}


def test_caveats_state_the_entry_assumption():
    joined = " ".join(ni.CAVEATS)
    assert "t+1 시가" in joined and "초과수익" in joined


# --- ④ 수집 — 페이지 넘김과 토큰 ---

def _install_httpx(monkeypatch, pages, seen=None):
    import httpx

    class _Res:
        def __init__(self, body):
            self._b = body

        def raise_for_status(self):
            return None

        def json(self):
            return {"items": self._b}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **k):
            if seen is not None:
                seen.append(k)
            off = k.get("params", {}).get("offset", 0)
            return _Res(pages[off // ni.PAGE] if off // ni.PAGE < len(pages) else [])

    monkeypatch.setattr(httpx, "Client", _Client)


def test_fetch_pages_until_short_page(monkeypatch):
    """limit 상한이 500이라 수천 건을 한 번에 못 받는다 — 페이지를 넘겨야 한다."""
    seen = []
    pages = [[{"id": i} for i in range(ni.PAGE)], [{"id": 9001}]]
    _install_httpx(monkeypatch, pages, seen)
    monkeypatch.setattr(settings, "TNM_TOKEN", "tnm-token")
    got = ni.fetch_analyses()
    assert len(got) == ni.PAGE + 1
    assert [s["params"]["offset"] for s in seen] == [0, ni.PAGE]


def test_fetch_uses_the_tnm_token_not_our_own(monkeypatch):
    """서비스마다 토큰이 다르다 — 자기 것을 보내면 401 이다."""
    seen = []
    _install_httpx(monkeypatch, [[]], seen)
    monkeypatch.setattr(settings, "INTERNAL_TOKEN", "trading-own")
    monkeypatch.setattr(settings, "TNM_TOKEN", "tnm-token")
    ni.fetch_analyses()
    assert seen[0]["headers"]["X-Internal-Token"] == "tnm-token"


def test_no_token_means_no_fetch(monkeypatch):
    monkeypatch.setattr(settings, "TNM_TOKEN", "")
    assert ni.fetch_analyses() == []
    assert ni.run_once()["ok"] is False


# --- ⑤ 배선 ---

def test_job_knows_news():
    from app.backtest import job

    assert "news" in job.JOBS


def test_latest_before_first_run(monkeypatch, tmp_path):
    monkeypatch.setattr(ni, "OUT_FILE", tmp_path / "none.json")
    assert ni.latest()["run_ts"] is None


def test_save_and_latest_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr(ni, "OUT_FILE", tmp_path / "ni.json")
    ni.save({"ok": True, "run_ts": "2026-07-27T23:00:00+09:00", "rows": 7})
    assert ni.latest()["rows"] == 7
