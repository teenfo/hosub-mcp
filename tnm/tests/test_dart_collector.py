"""M2 완료판정 — DART 수집기: rcept_no 증분·커서·재실행 중복 0·장중/장외 분기."""
from datetime import datetime
from zoneinfo import ZoneInfo

from app import settings
from app.collect import is_market_hours
from app.collectors.dart import DartCollector, parse_list_payload

KST = ZoneInfo("Asia/Seoul")

PAYLOAD = {
    "status": "000", "total_page": 1,
    "list": [
        {"rcept_no": "20260720000001", "report_nm": "단일판매ㆍ공급계약체결",
         "corp_name": "효성중공업", "flr_nm": "효성중공업", "rm": "유",
         "rcept_dt": "20260720"},
        {"rcept_no": "20260722000005", "report_nm": "주요사항보고서(유상증자결정)",
         "corp_name": "효성중공업", "flr_nm": "효성중공업", "rm": "",
         "rcept_dt": "20260722"},
    ],
}


def test_parse_incremental_by_rcept_no():
    docs, cursor = parse_list_payload(PAYLOAD, None)
    assert [d.source_uid for d in docs] == ["20260720000001", "20260722000005"]
    assert cursor == "20260722000005"
    assert docs[0].url.endswith("rcpNo=20260720000001")
    # 과거 접수분은 그날 자정 그대로 (오늘분 처리는 아래 별도 테스트)
    assert docs[0].published_at == datetime(2026, 7, 20, tzinfo=KST)
    assert "공급계약" in docs[0].title and "비고: 유" in docs[0].body


def test_parse_rerun_with_cursor_is_empty():
    """같은 응답을 커서와 함께 재파싱 → 신규 0건 (M2: 재실행 중복 0)."""
    docs, cursor = parse_list_payload(PAYLOAD, "20260722000005")
    assert docs == [] and cursor == "20260722000005"


def test_parse_partial_cursor():
    docs, cursor = parse_list_payload(PAYLOAD, "20260721000000")
    assert [d.source_uid for d in docs] == ["20260722000005"]
    assert cursor == "20260722000005"


def test_parse_no_data_status():
    docs, cursor = parse_list_payload({"status": "013", "message": "없음"}, "abc")
    assert docs == [] and cursor == "abc"


def test_parse_error_status_raises():
    try:
        parse_list_payload({"status": "020", "message": "한도 초과"}, None)
        raised = False
    except RuntimeError:
        raised = True
    assert raised


def test_enabled_requires_key_and_corp_code(monkeypatch):
    col = DartCollector()
    monkeypatch.setattr(settings, "DART_API_KEY", "")
    assert not col.enabled({"dart_corp_code": "00126380"})
    monkeypatch.setattr(settings, "DART_API_KEY", "k")
    assert not col.enabled({"dart_corp_code": None})
    assert col.enabled({"dart_corp_code": "00126380"})


def test_market_hours_branch():
    assert is_market_hours(datetime(2026, 7, 24, 10, 30, tzinfo=KST))      # 금 장중
    assert not is_market_hours(datetime(2026, 7, 24, 17, 0, tzinfo=KST))   # 금 장외
    assert not is_market_hours(datetime(2026, 7, 25, 10, 30, tzinfo=KST))  # 토


# --- 접수 시각 추정 (list.json 은 날짜만 준다) ---

def test_same_day_filing_uses_collection_time():
    """자정으로 두면 15:00 거래정지 공시가 15시간 전 것으로 보인다.

    신선도로 줄을 세우는 곳(분류 큐 우선순위·promote 의 window_hours)에서
    공시가 구조적으로 뒤로 밀린다. 전종목 모드가 장중 10분 주기라 수집 시각은
    실제 접수 시각의 상한이고 오차가 한 주기 안이다.
    """
    from app.collectors.dart import _published_at

    now = datetime(2026, 7, 29, 15, 4, tzinfo=KST)
    assert _published_at("20260729", now) == now


def test_earlier_filing_keeps_its_own_date():
    """소급 백필이 전부 '방금'이 되면 신선도 판정이 통째로 무너진다."""
    from app.collectors.dart import _published_at

    now = datetime(2026, 7, 29, 15, 4, tzinfo=KST)
    assert _published_at("20260727", now) == datetime(2026, 7, 27, tzinfo=KST)


def test_unparsable_date_falls_back_to_now():
    from app.collectors.dart import _published_at

    now = datetime(2026, 7, 29, 15, 4, tzinfo=KST)
    assert _published_at("", now) == now
