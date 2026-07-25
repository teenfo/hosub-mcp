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
        mapping = parse_corpcode_xml(zf.read(xml_name))
    CACHE_FILE.write_text(json.dumps(mapping, ensure_ascii=False))
    log.info("corp_code 매핑 갱신: %d건", len(mapping))
    return len(mapping)


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
