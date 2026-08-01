"""분봉 커버리지를 넓힐 대상 목록 — 거래대금 순, 날마다 같은 종목.

실측 2026-07-31: 1분봉 395종목 / 일봉 3,941종목. 406 은 기술적 상한이 아니라
마감 백필의 **대상 집합**(감시목록 + 로스터)이었다.
"""
import json

import pytest

from app.scout import nightly as discovery


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "DB_PATH", tmp_path / "discovery.db")
    return discovery


def _seed(db, day, rows):
    with db._conn() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO chart_obs VALUES (?,?,?,?,?)",
            [(day, c, c, 0.0, json.dumps({"trade_value": tv})) for c, tv in rows])


def test_거래대금_내림차순(db):
    _seed(db, "2026-08-03", [("000001", 100), ("000002", 900), ("000003", 500)])
    assert db.liquid_universe() == ["000002", "000003", "000001"]


def test_상한이_있으면_상위만(db):
    _seed(db, "2026-08-03", [("00000%d" % i, i * 10) for i in range(1, 6)])
    assert db.liquid_universe(2) == ["000005", "000004"]


def test_가장_최근_발굴일만_본다(db):
    _seed(db, "2026-08-03", [("000001", 100)])
    _seed(db, "2026-08-04", [("000002", 100)])
    assert db.liquid_universe() == ["000002"]


def test_거래대금이_같으면_코드순(db):
    """동점 tie-break 가 없으면 대상이 날마다 갈려 패널에 구멍이 생긴다."""
    _seed(db, "2026-08-03", [("000003", 100), ("000001", 100), ("000002", 100)])
    assert db.liquid_universe() == ["000001", "000002", "000003"]


def test_지표가_깨져_있어도_빠지지_않는다(db):
    """거래대금을 못 읽은 종목은 뒤로 밀릴 뿐 목록에서 사라지지 않는다."""
    with db._conn() as conn:
        conn.execute("INSERT INTO chart_obs VALUES (?,?,?,?,?)",
                     ("2026-08-03", "000009", "깨짐", 0.0, "not-json"))
    _seed(db, "2026-08-03", [("000001", 100)])
    assert db.liquid_universe() == ["000001", "000009"]


def test_관측이_없으면_빈_목록(db):
    assert db.liquid_universe() == []
