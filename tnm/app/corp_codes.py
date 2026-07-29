"""DART corpCode.xml → ticker↔corp_code 매핑.

opendart.fss.or.kr/api/corpCode.xml (zip 안의 CORPCODE.xml) 을 내려받아
DATA_DIR/corp_codes.json 으로 캐시한다. 주 1회(설정) 갱신.
"""
from __future__ import annotations

import io
import json
import logging
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import httpx

from . import settings

log = logging.getLogger("tnm.corp_codes")

CACHE_FILE: Path = settings.DATA_DIR / "corp_codes.json"
# 상장사 이름 캐시. 같은 XML 에서 함께 나오므로 추가 다운로드가 없다.
# 해외 증권사 인용 기사가 '한국 종목 얘기인가' 를 판별하는 데 쓴다.
NAMES_FILE: Path = settings.DATA_DIR / "corp_names.json"
_URL = "https://opendart.fss.or.kr/api/corpCode.xml"


def parse_corpcode_xml(xml_bytes: bytes) -> dict[str, str]:
    """CORPCODE.xml → {stock_code(6자리): corp_code}. 비상장(stock_code 공백)은 제외."""
    out: dict[str, str] = {}
    root = ET.fromstring(xml_bytes)
    for el in root.iter("list"):
        stock = (el.findtext("stock_code") or "").strip()
        corp = (el.findtext("corp_code") or "").strip()
        if stock and corp:
            out[stock] = corp
    return out


def parse_corpcode_names(xml_bytes: bytes) -> dict[str, str]:
    """CORPCODE.xml → {stock_code: corp_name}. 상장사만."""
    out: dict[str, str] = {}
    root = ET.fromstring(xml_bytes)
    for el in root.iter("list"):
        stock = (el.findtext("stock_code") or "").strip()
        name = (el.findtext("corp_name") or "").strip()
        if stock and name:
            out[stock] = name
    return out


def listed_names() -> set[str]:
    """상장사명 집합. 캐시가 없으면 빈 집합 — 호출자가 '판별 불가'로 다뤄야 한다."""
    if not NAMES_FILE.exists():
        return set()
    try:
        return set(json.loads(NAMES_FILE.read_text()).values())
    except (OSError, ValueError):
        return set()


def _cache_age_days() -> float:
    if not CACHE_FILE.exists():
        return float("inf")
    return (time.time() - CACHE_FILE.stat().st_mtime) / 86400


def load_cache() -> dict[str, str]:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except (OSError, ValueError):
            return {}
    return {}


async def refresh() -> int:
    """corpCode.xml zip 다운로드 → 캐시 갱신. 반환: 매핑 수."""
    if not settings.DART_API_KEY:
        return 0
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(_URL, params={"crtfc_key": settings.DART_API_KEY})
        r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        xml_name = next(n for n in zf.namelist() if n.lower().endswith(".xml"))
        raw = zf.read(xml_name)
        mapping = parse_corpcode_xml(raw)
        names = parse_corpcode_names(raw)
    CACHE_FILE.write_text(json.dumps(mapping, ensure_ascii=False))
    NAMES_FILE.write_text(json.dumps(names, ensure_ascii=False))
    log.info("corp_code 매핑 갱신: %d건 (상장사명 %d건)", len(mapping), len(names))
    return len(mapping)


async def ensure_names() -> set[str]:
    """상장사명 집합. 없으면 1회 받아온다.

    `mapping_for_missing` 의 나이 기준(주 1회)에만 맡기면, corp_codes.json 이
    최근에 갱신된 상태에서 이 기능이 배포될 때 **최대 7일 동안 이름 캐시가
    비어 있다.** 그 사이 해외 인용 필터가 시장 용어만으로 좁게 돌아간다.
    """
    names = listed_names()
    if names or not settings.DART_API_KEY:
        return names
    try:
        await refresh()
    except Exception as e:  # noqa: BLE001 — 없으면 좁게 판정할 뿐, 수집은 계속
        log.warning("상장사명 캐시 생성 실패: %s", e)
        return set()
    return listed_names()


async def mapping_for_missing() -> dict[str, str]:
    """캐시(오래됐으면 갱신)를 반환. DART 키 없으면 빈 dict."""
    if not settings.DART_API_KEY:
        return {}
    max_age = int(settings.WATCH.get("corp_code_refresh_days", 7))
    if _cache_age_days() > max_age:
        try:
            await refresh()
        except Exception as e:  # noqa: BLE001 — 캐시가 있으면 그걸로 계속
            log.warning("corpCode 갱신 실패(캐시 사용): %s", e)
    return load_cache()
