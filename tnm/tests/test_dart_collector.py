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
