from app import settings
from app.data import symbols


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(symbols, "DB_PATH", tmp_path / "sym.db")


MASTER = [
    {"code": "005930", "name": "삼성전자"},
    {"code": "005935", "name": "삼성전자우"},
    {"code": "000660", "name": "SK하이닉스"},
    {"code": "035420", "name": "NAVER"},
    {"code": "BAD", "name": "잘못된코드"},  # 6자리 숫자 아님 → 제외
]


def test_upsert_filters_bad_codes(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    assert symbols.upsert(MASTER) == 4
    assert symbols.count() == 4
    assert symbols.name_of("005930") == "삼성전자"
    assert symbols.name_of("999999") is None


def test_resolve_by_code(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    symbols.upsert(MASTER)
    r = symbols.resolve("000660")
    assert r == [{"code": "000660", "name": "SK하이닉스"}]
    # 마스터에 없는 코드도 코드 자체로 반환 (직접 추가 허용)
    assert symbols.resolve("123456") == [{"code": "123456", "name": "123456"}]


def test_resolve_exact_name_beats_partial(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    symbols.upsert(MASTER)
    # '삼성전자' 완전일치 → 우선주(부분일치)보다 먼저
    r = symbols.resolve("삼성전자")
    assert len(r) == 1 and r[0]["code"] == "005930"


def test_resolve_partial_returns_multiple(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    symbols.upsert(MASTER)
    r = symbols.resolve("삼성")
    codes = {c["code"] for c in r}
    assert codes == {"005930", "005935"}


def test_resolve_ignores_spaces(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    symbols.upsert([{"code": "035420", "name": "NAVER"}, {"code": "323410", "name": "카카오 뱅크"}])
    assert symbols.resolve("카카오뱅크")[0]["code"] == "323410"


def test_resolve_not_found(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    symbols.upsert(MASTER)
    assert symbols.resolve("없는종목명") == []
