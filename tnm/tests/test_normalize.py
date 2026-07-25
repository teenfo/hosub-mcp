"""M2 완료판정 — 정규화·해시 안정성 (FR-04)."""
from app.pipeline.normalize import content_hash, normalize, strip_html


def test_strip_html():
    assert strip_html("<b>삼성전자</b> &amp; SK") == " 삼성전자  & SK"


def test_normalize_removes_reporter_and_copyright():
    src = ("삼성전자가 신규 수주를 공시했다.  홍길동 기자 hong@news.co.kr "
           "ⓒ 한국뉴스, 무단 전재 및 재배포 금지")
    out = normalize(src)
    assert "기자" not in out and "@" not in out
    assert "재배포" not in out
    assert "삼성전자가 신규 수주를 공시했다." in out


def test_normalize_collapses_whitespace():
    assert normalize("a\n\n  b\t c") == "a b c"


def test_content_hash_stable_and_noise_invariant():
    """해시는 노이즈(기자명·공백)와 무관하게 동일 — 완전중복 판정의 핵심."""
    h1 = content_hash("제목", "본문 내용.  홍길동 기자")
    h2 = content_hash("제목", "본문   내용. 김철수 기자")
    h3 = content_hash("제목", "다른 본문")
    assert h1 == h2               # 노이즈만 다른 재탕 → 동일 해시
    assert h1 != h3
    assert len(h1) == 64          # sha256 hex


def test_content_hash_title_matters():
    assert content_hash("제목A", "본문") != content_hash("제목B", "본문")
