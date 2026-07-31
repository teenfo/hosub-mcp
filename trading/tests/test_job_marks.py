"""하루 1회 배치의 완료 표시 — 프로세스가 아니라 디스크에 묻는다.

종전에는 루프의 지역변수(`done_for`)였다. 재시작하면 초기화된다 —
실측 2026-07-31: 배포 5회에 마감 백필이 5번 돌아 398종목 × 5 ≈ 2,000콜을 버렸다.
"""
import pytest

from app.data import store


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "market.db")


def test_표시하기_전에는_안_끝난_것(db):
    assert store.job_done("eod_backfill", "2026-08-03") is False


def test_표시하면_끝난_것(db):
    store.mark_job_done("eod_backfill", "2026-08-03")
    assert store.job_done("eod_backfill", "2026-08-03") is True


def test_재시작을_넘어_남는다(db):
    """이 테스트가 회귀의 핵심이다 — 새 연결이 같은 답을 해야 한다."""
    store.mark_job_done("eod_backfill", "2026-08-03")
    with store._conn() as conn:          # 프로세스가 죽었다 살아난 상황과 같다
        n = conn.execute("SELECT COUNT(*) FROM job_marks").fetchone()[0]
    assert n == 1
    assert store.job_done("eod_backfill", "2026-08-03") is True


def test_날짜가_다르면_다시_돈다(db):
    store.mark_job_done("eod_backfill", "2026-08-03")
    assert store.job_done("eod_backfill", "2026-08-04") is False


def test_배치마다_따로_센다(db):
    """마감 백필이 끝났다고 수급까지 끝난 것이 아니다."""
    store.mark_job_done("eod_backfill", "2026-08-03")
    assert store.job_done("flows", "2026-08-03") is False
    store.mark_job_done("flows", "2026-08-03")
    assert store.job_done("flows", "2026-08-03") is True


def test_두_번_표시해도_한_행(db):
    store.mark_job_done("flows", "2026-08-03")
    store.mark_job_done("flows", "2026-08-03")
    with store._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM job_marks").fetchone()[0] == 1
