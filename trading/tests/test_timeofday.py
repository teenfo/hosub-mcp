"""진입 시간대 분석 — 버킷 경계·집계·실거래 원장 읽기."""
import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from app.research import timeofday

KST = ZoneInfo("Asia/Seoul")


def test_버킷_경계():
    # 장외는 None, 세션 안은 30분 내림
    assert timeofday.bucket_of("2026-07-27T08:59:00+09:00") is None
    assert timeofday.bucket_of("2026-07-27T09:00:00+09:00") == "09:00"
    assert timeofday.bucket_of("2026-07-27T09:29:59+09:00") == "09:00"
    assert timeofday.bucket_of("2026-07-27T09:30:00+09:00") == "09:30"
    assert timeofday.bucket_of("2026-07-27T15:29:00+09:00") == "15:00"
    assert timeofday.bucket_of("2026-07-27T15:30:00+09:00") is None
    assert timeofday.bucket_of("깨진 값") is None
    assert timeofday.bucket_of(None) is None


def test_버킷은_naive_를_KST_로_본다():
    # 원장 opened 는 KST 문자열(오프셋 유무 혼재) — naive 는 그대로 읽어야 한다
    assert timeofday.bucket_of("2026-07-27T10:05:00") == "10:00"
    # tz-aware UTC 는 KST 로 환산된다 (01:00Z == 10:00 KST)
    assert timeofday.bucket_of(
        datetime(2026, 7, 27, 1, 0, tzinfo=ZoneInfo("UTC"))) == "10:00"


def test_버킷_목록은_13개():
    bs = timeofday.buckets()
    assert len(bs) == 13
    assert bs[0] == "09:00" and bs[-1] == "15:00"


def test_집계_승률과_평균():
    rows = [
        {"bucket": "09:00", "win": True, "r": 1.5},
        {"bucket": "09:00", "win": False, "r": -1.0},
        {"bucket": "13:00", "win": False, "r": -1.0},
    ]
    agg = timeofday._agg(rows, "r")
    assert agg["09:00"]["n"] == 2
    assert agg["09:00"]["win_rate"] == 50.0
    assert agg["09:00"]["avg_r"] == 0.25
    assert agg["13:00"]["win_rate"] == 0.0
    assert "09:30" not in agg          # 빈 버킷은 싣지 않는다


def test_실거래_원장_읽기(tmp_path, monkeypatch):
    from app.research import cases

    db = tmp_path / "backtest.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE cases (pos_id TEXT PRIMARY KEY, opened TEXT, rule TEXT,"
            " pnl_pct REAL, pnl_krw REAL, mfe_pct REAL, mae_pct REAL)")
        conn.executemany(
            "INSERT INTO cases VALUES (?,?,?,?,?,?,?)",
            [
                ("a", "2026-07-27T09:05:00+09:00", "orb", 1.2, 500, 1.5, -0.3),
                ("b", "2026-07-27T09:20:00+09:00", "orb", -1.0, -400, 0.1, -1.4),
                ("c", "2026-07-27T13:40:00+09:00", "gap", 0.5, 120, 0.9, -0.2),
                # 장외 진입·손익 미확정은 표본에서 빠진다
                ("d", "2026-07-27T08:30:00+09:00", "orb", 9.9, 999, None, None),
                ("e", "2026-07-27T10:00:00+09:00", "orb", None, None, None, None),
            ])
    monkeypatch.setattr(cases, "DB_PATH", db)
    st = timeofday.live_stats()
    assert st["n"] == 3
    assert st["buckets"]["09:00"]["n"] == 2
    assert st["buckets"]["09:00"]["win_rate"] == 50.0
    assert st["buckets"]["13:30"]["n"] == 1
    assert st["buckets"]["09:00"]["avg_mae_pct"] == -0.85


def test_원장이_없으면_빈_결과(tmp_path, monkeypatch):
    from app.research import cases

    monkeypatch.setattr(cases, "DB_PATH", tmp_path / "없는.db")
    assert timeofday.live_stats() == {"n": 0}


def test_결과_파일_왕복(tmp_path, monkeypatch):
    out = tmp_path / "timeofday.json"
    monkeypatch.setattr(timeofday, "OUT_FILE", out)
    assert timeofday.latest() == {"generated_at": None}
    out.write_text(json.dumps({"generated_at": "2026-08-01T12:00:00+09:00",
                               "live": {"n": 0}}), encoding="utf-8")
    assert timeofday.latest()["live"] == {"n": 0}
