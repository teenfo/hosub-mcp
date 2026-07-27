"""로그에서 시크릿을 지운다.

httpx 가 INFO 레벨에서 **요청 URL 전체**를 찍는다. DART 는 API 키를 쿼리
파라미터(`crtfc_key`)로 받으므로, 그대로 두면 journald 를 읽을 수 있는 사람에게
키가 그대로 보인다(실측 2026-07-27, tnm.service 로그).

로거를 통째로 끄지 않고 **가려서** 남기는 이유는, 어떤 URL 을 언제 호출했는지가
수집 장애를 진단하는 1차 단서이기 때문이다. 키만 지우면 둘 다 얻는다.

포매팅 전 `record.args` 까지 훑는다 — httpx 는 `log.info("HTTP Request: %s %s ...")`
형태로 URL 을 인자에 담아 넘기므로, `record.msg` 만 보면 아무것도 못 지운다.
"""
import logging
import re

# 값이 시크릿인 쿼리 파라미터. 이름을 남기고 값만 가린다 — 어느 키가 쓰였는지는
# 진단에 필요하고, 값은 필요 없다.
SECRET_PARAMS = ("crtfc_key", "client_secret", "client_id", "api_key",
                 "apikey", "token", "access_token", "key")
_QUERY = re.compile(
    r"(?i)\b(" + "|".join(SECRET_PARAMS) + r")=([^&\s\"'|]+)")
MASK = "***"


def scrub(text: str) -> str:
    return _QUERY.sub(lambda m: f"{m.group(1)}={MASK}", text)


def _scrub_arg(a):
    """포매팅 인자 하나를 가린다 — **문자열이 아닌 것도 본다.**

    httpx 는 URL 을 `httpx.URL` **객체**로 넘긴다:

        logger.info('HTTP Request: %s %s "%s %d %s"', request.method, request.url, ...)

    문자열만 검사하면 이게 그대로 통과한다 — 실서버에서 정확히 그렇게 새고
    있었다(2026-07-27, 필터를 넣고도 키가 12번 찍혔다). 그렇다고 전부
    `str()` 로 바꾸면 `%d` 인자가 깨지므로, **시크릿이 실제로 들어 있을 때만**
    문자열로 치환한다.
    """
    if isinstance(a, str):
        return scrub(a) if "=" in a else a
    try:
        s = str(a)
    except Exception:  # noqa: BLE001 - __str__ 이 터져도 로깅을 막지 않는다
        return a
    if "=" not in s:
        return a
    masked = scrub(s)
    return masked if masked != s else a


class SecretFilter(logging.Filter):
    """레코드의 메시지와 인자에서 시크릿 쿼리값을 지운다."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and "=" in record.msg:
            record.msg = scrub(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: _scrub_arg(v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(_scrub_arg(a) for a in record.args)
        return True


def install() -> None:
    """루트 핸들러 전부에 필터를 건다.

    핸들러에 거는 이유: 로거에 걸면 그 로거를 통과하는 레코드만 잡히는데,
    서드파티 로거는 이름을 다 알 수 없다. 핸들러는 최종 출구라 전부 지난다.
    """
    f = SecretFilter()
    root = logging.getLogger()
    for h in root.handlers:
        if not any(isinstance(x, SecretFilter) for x in h.filters):
            h.addFilter(f)
