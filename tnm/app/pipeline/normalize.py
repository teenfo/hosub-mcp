"""정규화 + 완전중복 해시 (요청서 FR-04) — 순수 함수만.

제목·본문에서 공백, 기자명, 저작권 문구, 광고 문구를 제거하고
정규화 본문의 SHA-256 으로 완전 중복을 판정한다.
"""
from __future__ import annotations

import hashlib
import html
import re

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# 보수적으로 제거: 기사 꼬리표(기자명·이메일), 저작권·재배포 금지, 광고 상용구
_NOISE_RES = [
    re.compile(r"[가-힣]{2,4}\s?(선임)?기자(\s?=)?"),
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),                     # 이메일
    re.compile(r"[ⓒ©].{0,40}?(무단\s?전재|재배포|배포)\s?(및|와)?\s?[^\s.]*\s?금지"),
    re.compile(r"저작권자\s?[ⓒ©]?.{0,40}?(금지|보호)"),
    re.compile(r"무단\s?(전재|복제)\s?(및|와)?\s?재배포\s?금지"),
    re.compile(r"\[[^\]]{0,20}(뉴스|일보|경제|미디어|투데이)[^\]]{0,10}\]"),  # [매체명] 머리표
    re.compile(r"(☞|▶)\s?[^\n]{0,60}(바로가기|구독|클릭)"),      # 광고성 꼬리
]


def strip_html(text: str) -> str:
    """태그 제거 + 엔티티 해석 (네이버 API 의 <b> 등)."""
    return html.unescape(_TAG_RE.sub(" ", text or ""))


def clean_text(text: str) -> str:
    """태그 제거 + 공백 병합 — 수집 단계의 제목·요약 정리용 (노이즈 제거는 안 함)."""
    return _WS_RE.sub(" ", strip_html(text)).strip()


def normalize(text: str) -> str:
    """노이즈 제거 + 공백 정리. 원문(body)은 별도 보존되므로 파괴적이어도 된다."""
    s = strip_html(text or "")
    for rx in _NOISE_RES:
        s = rx.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def content_hash(title: str, body: str | None) -> str:
    """정규화 (제목+본문) 의 SHA-256 — 완전중복 판정 키."""
    norm = normalize(title) + "\n" + normalize(body or "")
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()
