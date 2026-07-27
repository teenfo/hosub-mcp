"""로그에서 시크릿을 지운다 — httpx 가 요청 URL 전체를 찍는다.

DART 는 API 키를 쿼리 파라미터로 받으므로, 가리지 않으면 journald 를 읽을 수
있는 사람에게 키가 그대로 보인다(실측 2026-07-27 tnm.service 로그).

로거를 끄지 않고 가리는 이유: 어떤 URL 을 언제 호출했는지가 수집 장애 진단의
1차 단서다. 키만 지우면 둘 다 얻는다.
"""
import logging

import pytest

from app import logsafe


def test_scrub_masks_dart_key():
    url = ("https://opendart.fss.or.kr/api/list.json?crtfc_key=abcdef0123456789"
           "&bgn_de=20260727&page_no=2")
    out = logsafe.scrub(url)
    assert "abcdef0123456789" not in out
    assert "crtfc_key=***" in out
    assert "bgn_de=20260727" in out       # 진단에 필요한 것은 남는다


@pytest.mark.parametrize("param", logsafe.SECRET_PARAMS)
def test_every_declared_param_is_masked(param):
    assert logsafe.scrub(f"http://x/y?{param}=SECRETVALUE&z=1") == \
           f"http://x/y?{param}=***&z=1"


def test_case_insensitive():
    assert "***" in logsafe.scrub("http://x?CRTFC_KEY=abc")


def test_non_secret_params_untouched():
    url = "http://x/y?page_no=2&sort=date&bgn_de=20260101"
    assert logsafe.scrub(url) == url


def test_multiple_secrets_in_one_line():
    out = logsafe.scrub("a?token=T1 b?client_secret=S2")
    assert "T1" not in out and "S2" not in out


# --- 필터가 실제 로그 레코드에서 동작하는가 ---

def _record(msg, args=()):
    return logging.LogRecord("httpx", logging.INFO, __file__, 1, msg, args, None)


def test_filter_scrubs_message():
    r = _record("HTTP Request: GET http://x?crtfc_key=abc \"200 OK\"")
    logsafe.SecretFilter().filter(r)
    assert "abc" not in r.getMessage()


def test_filter_scrubs_args_not_just_message():
    """httpx 는 URL 을 **인자**로 넘긴다 — msg 만 보면 아무것도 못 지운다."""
    r = _record("HTTP Request: %s %s", ("GET", "http://x?crtfc_key=SECRET"))
    logsafe.SecretFilter().filter(r)
    assert "SECRET" not in r.getMessage()
    assert "crtfc_key=***" in r.getMessage()


class _UrlLike:
    """httpx.URL 흉내 — 문자열이 아니지만 str() 하면 키가 들어 있다."""

    def __init__(self, s):
        self._s = s

    def __str__(self):
        return self._s


def test_filter_scrubs_non_string_args():
    """httpx 는 URL 을 **객체**로 넘긴다 — 문자열만 보면 그대로 샌다.

    실서버 회귀(2026-07-27): 필터를 넣고 배포했는데도 키가 12번 찍혔다.
    isinstance(a, str) 검사에서 httpx.URL 객체가 통과했기 때문이다.
    """
    r = _record('HTTP Request: %s %s "%s"',
                ("GET", _UrlLike("https://x/api?crtfc_key=LEAKED&page=2"), "200 OK"))
    logsafe.SecretFilter().filter(r)
    msg = r.getMessage()
    assert "LEAKED" not in msg
    assert "crtfc_key=***" in msg and "page=2" in msg


def test_numeric_args_survive_formatting():
    """전부 str() 로 바꾸면 %d 가 깨진다 — 시크릿이 있을 때만 치환한다."""
    r = _record("%s took %d ms", ("GET", 42))
    logsafe.SecretFilter().filter(r)
    assert r.getMessage() == "GET took 42 ms"


def test_object_without_secret_is_left_alone():
    obj = _UrlLike("https://x/api?page=2")
    r = _record("%s", (obj,))
    logsafe.SecretFilter().filter(r)
    assert r.args[0] is obj          # 바꾸지 않는다


def test_broken_str_does_not_break_logging():
    class _Boom:
        def __str__(self):
            raise RuntimeError("nope")

    r = _record("%r", (_Boom(),))
    assert logsafe.SecretFilter().filter(r) is True


def test_filter_handles_dict_args():
    # LogRecord 는 1-튜플 안의 Mapping 을 풀어 args 로 삼는다 — 그 형태로 넘긴다
    r = _record("%(url)s", ({"url": "http://x?token=SECRET"},))
    logsafe.SecretFilter().filter(r)
    assert "SECRET" not in r.getMessage()


def test_filter_passes_records_through():
    """필터는 가릴 뿐 버리지 않는다 — 로그가 사라지면 진단이 불가능해진다."""
    assert logsafe.SecretFilter().filter(_record("아무 메시지")) is True


def test_filter_survives_non_string_args():
    r = _record("count=%d", (7,))
    logsafe.SecretFilter().filter(r)
    assert r.getMessage() == "count=7"


# --- 설치 ---

def test_install_is_idempotent():
    root = logging.getLogger()
    handler = logging.NullHandler()
    root.addHandler(handler)
    try:
        logsafe.install()
        logsafe.install()
        assert sum(isinstance(f, logsafe.SecretFilter)
                   for f in handler.filters) == 1
    finally:
        root.removeHandler(handler)


def test_install_covers_third_party_loggers(caplog):
    """서드파티 로거 이름을 다 알 수 없으므로 **핸들러**에 건다."""
    handler = logging.StreamHandler()
    handler.addFilter(logsafe.SecretFilter())
    rec = _record("GET %s", ("http://x?crtfc_key=LEAK",))
    for f in handler.filters:
        f.filter(rec)
    assert "LEAK" not in rec.getMessage()
