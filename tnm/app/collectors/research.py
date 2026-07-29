"""증권사 리서치 리포트 수집기.

세 경로가 있고, 셋 다 같은 `Report` 로 정규화된다.

| 경로 | 소스 | 범위 | 목표가·의견 |
|---|---|---|---|
| `naver` | 네이버 금융 리서치 | 종목/산업/시황/경제/투자정보 5분류 | 종목분석은 상세 1콜 추가 |
| `consensus` | 한경컨센서스 | 기업/산업/시장/채권 | **목록에 이미 실려 있다** |
| `news` | 구글 뉴스 RSS | 해외 증권사 인용 기사 | 없음(본문에만) |

네이버는 EUC-KR 이고 한경은 UTF-8 이다. 둘 다 붙이는 이유는 다루는 증권사
집합이 다르기 때문이다(실측 2026-07-29: 한경에만 LS증권, 네이버에만
DS투자증권·다올투자증권). 같은 리포트가 양쪽에 나오면 (source, source_uid)
가 달라 두 행이 남는데, 그건 중복이 아니라 **두 소스가 같은 것을 봤다는 기록**
이다. 파이프라인 쪽 중복은 raw_items 의 content_hash 가 이미 막는다.

HTML 파싱에 bs4 를 쓰지 않는다 — 이 저장소는 파싱을 위해 의존성을 늘리지
않는 관례가 있고(dart 는 JSON, rss 는 ElementTree), 표 구조가 단순해 정규식
으로 충분하다. 대신 **구조가 바뀌면 0건이 되므로** 러너가 0건을 경고로 남긴다.
"""
from __future__ import annotations

import html as html_mod
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("tnm.research")
KST = ZoneInfo("Asia/Seoul")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
       " (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_TIMEOUT = httpx.Timeout(20.0)

NAVER_BASE = "https://finance.naver.com/research"
# 네이버 목록 페이지 → 우리 category 값.
NAVER_CATEGORIES = {
    "company": "company",       # 종목분석
    "industry": "industry",     # 산업분석
    "market_info": "market",    # 시황정보
    "economy": "economy",       # 경제분석
    "invest": "invest",         # 투자정보
}
CONSENSUS_URL = "https://consensus.hankyung.com/analysis/list"
# 한경컨센서스 report_type → 우리 category 값. EC/ST 는 실측 0건이라 뺐다.
CONSENSUS_TYPES = {"CO": "company", "IN": "industry", "MA": "market", "DE": "economy"}

GOOGLE_RSS = "https://news.google.com/rss/search"


@dataclass
class Report:
    source: str            # naver | consensus | news
    source_uid: str
    broker: str            # 원문 표기 그대로 — 매칭은 호출자가 한다
    category: str
    title: str
    url: str
    published_at: datetime
    ticker: str | None = None
    stock_name: str | None = None
    summary: str | None = None
    pdf_url: str | None = None
    analyst: str | None = None
    target_price: int | None = None
    opinion: str | None = None
    evidence: dict = field(default_factory=dict)


# ---------------- 공통 파싱 도우미 ----------------

_TAG = re.compile(r"<[^>]+>")
# 주석을 태그와 같이 취급하면 안 된다. 주석 안에 '>' 가 있으면 <[^>]+> 가 거기서
# 끊겨 남은 '-->' 가 본문 텍스트로 새어 나온다 — 실측으로 한경 기업 리포트의
# 마지막 칸이 '-->' 로 읽혀 증권사가 한 칸씩 밀렸다.
_COMMENT = re.compile(r"<!--.*?-->", re.S)


def _text(s: str) -> str:
    """주석·태그 제거 + 엔티티 복원 + 공백 정리."""
    s = _COMMENT.sub(" ", s or "")
    return re.sub(r"\s+", " ", html_mod.unescape(_TAG.sub(" ", s))).strip()


def _to_int(s: str) -> int | None:
    digits = re.sub(r"[^0-9]", "", s or "")
    return int(digits) if digits else None


def parse_ymd(s: str, *, two_digit_year: bool = False) -> datetime | None:
    """'26.07.29' / '2026-07-29' → KST 자정. 형식이 어긋나면 None."""
    s = (s or "").strip()
    m = re.match(r"^(\d{2,4})[.\-/](\d{1,2})[.\-/](\d{1,2})$", s)
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    if two_digit_year or y < 100:
        y += 2000
    try:
        return datetime(y, mo, d, tzinfo=KST)
    except ValueError:
        return None


# ---------------- 네이버 금융 리서치 ----------------

_NAVER_ROW = re.compile(r"<tr>(.*?)</tr>", re.S)
_NAVER_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_NAVER_NID = re.compile(r"(\w+_read\.naver\?nid=(\d+)[^\"']*)")
_NAVER_CODE = re.compile(r"code=(\d{6})")
_NAVER_PDF = re.compile(r"href=\"(https?://[^\"]+\.pdf)\"")


def parse_naver_list(html: str, category: str) -> list[Report]:
    """네이버 리서치 목록 HTML → Report 목록.

    표 구조가 분류마다 다르다. 종목분석/산업분석은 6칸(첫 칸이 종목명·분류),
    시황/경제/투자정보는 5칸이다. 칸 개수로 가르지 않고 **뒤에서부터** 읽는다
    — 작성일·조회수·첨부·증권사는 어느 분류에서나 마지막 4칸이기 때문이다.
    """
    out: list[Report] = []
    for block in _NAVER_ROW.findall(html):
        tds = _NAVER_TD.findall(block)
        if len(tds) < 5:
            continue
        link = _NAVER_NID.search(block)
        if not link:
            continue
        href, nid = link.group(1), link.group(2)
        # 뒤에서: [-1] 조회수 · [-2] 작성일 · [-3] 첨부 · [-4] 증권사
        published = parse_ymd(_text(tds[-2]), two_digit_year=True)
        broker = _text(tds[-4])
        if not (published and broker):
            continue
        title_td = tds[-5] if len(tds) >= 5 else ""
        title = _text(title_td)
        if not title:
            continue
        pdf = _NAVER_PDF.search(tds[-3])
        code = _NAVER_CODE.search(tds[0]) if len(tds) >= 6 else None
        out.append(Report(
            source="naver", source_uid=f"{category}:{nid}", broker=broker,
            category=category, title=title,
            url=f"{NAVER_BASE}/{href.split('?')[0]}?nid={nid}",
            published_at=published,
            ticker=code.group(1) if code else None,
            stock_name=_text(tds[0]) if (len(tds) >= 6 and code) else None,
            pdf_url=pdf.group(1) if pdf else None,
        ))
    return out


_NAVER_TARGET = re.compile(r"목표가\s*<em[^>]*>(.*?)</em>", re.S)
_NAVER_OPINION = re.compile(r"투자의견\s*<em[^>]*>(.*?)</em>", re.S)


def parse_naver_detail(html: str) -> dict:
    """종목분석 상세 → {target_price, opinion}. '없음'은 None 으로 읽는다."""
    out: dict = {"target_price": None, "opinion": None}
    m = _NAVER_TARGET.search(html)
    if m:
        out["target_price"] = _to_int(_text(m.group(1)))
    m = _NAVER_OPINION.search(html)
    if m:
        val = _text(m.group(1))
        out["opinion"] = None if val in ("", "없음", "-") else val
    return out


# ---------------- 한경컨센서스 ----------------

_HK_ROW = re.compile(r"<tr>(.*?)</tr>", re.S)
_HK_TD = re.compile(r"<td([^>]*)>(.*?)</td>", re.S)
_HK_IDX = re.compile(r"downpdf\?report_idx=(\d+)")
_HK_SUMMARY = re.compile(r"<li>(.*?)</li>", re.S)
# 제목의 '종목명(123456) 나머지' 에서 종목·코드를 뽑는다.
_HK_STOCK = re.compile(r"^(.*?)\((\d{6})\)\s*(.*)$")
_OPINIONS = {
    "buy", "strongbuy", "tradingbuy", "hold", "sell", "neutral", "notrated", "nr",
    "outperform", "marketperform", "underperform", "overweight", "underweight",
    "매수", "적극매수", "중립", "보유", "매도", "비중확대", "비중축소", "투자의견없음",
}


def _is_opinion(s: str) -> bool:
    """작성자 칸에 밀려 들어온 투자의견인지 — 사람 이름과 구분한다."""
    return re.sub(r"[\s.\-]+", "", s or "").lower() in _OPINIONS


def parse_consensus_list(html: str, category: str) -> list[Report]:
    """한경컨센서스 목록 HTML → Report 목록.

    **칸 배치가 리포트 종류마다 다르다**(실측 2026-07-29):

        기업 CO  작성일 · 제목 · 적정가격 · 투자의견 · 작성자 · 증권사 · 첨부
        산업 IN  작성일 · 제목 · 분류 · 작성자 · 증권사 · (빈칸 2)
        시장 MA  작성일 · 제목 · 작성자 · 증권사 · (빈칸 2)
        채권 DE  작성일 · **분류** · 제목 · 작성자 · 증권사 · (빈칸)

    그래서 고정 인덱스로 읽으면 안 된다. 처음 그렇게 짰다가 산업·시장·채권이
    통째로 0건이 됐다 — 증권사 칸이 비어 보여 전부 걸러졌다.

    대신 위치가 흔들리지 않는 두 앵커를 쓴다.
      · **제목** = downpdf 링크가 들어 있는 칸 (채권은 두 번째가 아니다)
      · **증권사** = 제목 뒤의 마지막 비어 있지 않은 칸 (뒤쪽 첨부 칸은 빈다)
        작성자는 그 앞 칸.
    목표가·투자의견은 기업 리포트에만 있으므로 `text_r txt_number` 클래스로
    찾는다(작성일 칸도 txt_number 라 text_r 까지 봐야 한다).
    """
    out: list[Report] = []
    for block in _HK_ROW.findall(html):
        idx = _HK_IDX.search(block)
        if not idx:
            continue
        cells = _HK_TD.findall(block)          # [(attrs, inner), ...]
        if len(cells) < 4:
            continue
        published = parse_ymd(_text(cells[0][1]))
        if not published:
            continue
        title_i = next((i for i, (_, inner) in enumerate(cells)
                        if "downpdf?report_idx" in inner), None)
        if title_i is None:
            continue
        # 제목 칸에는 마우스오버 요약(div.layerPop)이 같이 들어 있다. 먼저
        # 떼어내지 않으면 같은 문장이 제목에 두세 번 붙는다.
        raw_title = cells[title_i][1]
        summ = _HK_SUMMARY.search(raw_title)
        title = _text(re.sub(r"<div\b.*?</div>", " ", raw_title, flags=re.S))
        if not title:
            continue
        tail = [i for i in range(title_i + 1, len(cells)) if _text(cells[i][1])]
        if not tail:
            continue
        broker = _text(cells[tail[-1]][1])
        analyst = _text(cells[tail[-2]][1]) if len(tail) >= 2 else None
        # 증권사·작성자를 뺀 가운데 칸이 적정가격·투자의견이다. 기업 리포트에만
        # 있고, 산업은 '분류' 한 칸(값이 '-')이라 숫자 변환에서 None 이 된다.
        middle = tail[:-2]
        target = _to_int(_text(cells[middle[0]][1])) if middle else None
        opinion = _text(cells[middle[1]][1]) if len(middle) >= 2 else None
        # 작성자 칸이 비면 투자의견이 그 자리로 밀린다 — 사람 이름이 아니면 옮긴다
        if analyst and _is_opinion(analyst):
            opinion, analyst = opinion or analyst, None
        ticker = stock_name = None
        m = _HK_STOCK.match(title)
        if m:
            stock_name, ticker, rest = m.group(1).strip(), m.group(2), m.group(3).strip()
            title = rest or title
        url = f"https://consensus.hankyung.com/analysis/downpdf?report_idx={idx.group(1)}"
        out.append(Report(
            source="consensus", source_uid=idx.group(1), broker=broker,
            category=category, title=title, url=url, pdf_url=url,
            published_at=published, ticker=ticker, stock_name=stock_name,
            summary=_text(summ.group(1)) if summ else None,
            target_price=target, opinion=opinion, analyst=analyst or None,
        ))
    return out


# ---------------- 해외 증권사 — 국내 언론 인용 ----------------

_VOCAB = "(목표주가 OR 투자의견 OR 리포트 OR 보고서)"
# 국내 시장 자체를 가리키는 말. 이 말이 제목에 있으면 종목명이 없어도 국내 얘기다.
KR_MARKET_TOKENS = ("코스피", "코스닥", "KOSPI", "KOSDAQ", "국내증시", "국내 증시",
                    "한국증시", "한국 증시", "한국 주식", "한국 주식시장", "서울 증시",
                    "한국물", "원화", "韓증시", "韓 증시", "韓주식")
# 국내 언론이 즐겨 쓰는 축약. DART 정식명으로는 절대 안 잡히는데, 정작
# "노무라, 삼전 목표가 67만원" 같은 가장 값진 제목이 이 형태로 온다.
KR_NICKNAMES = {
    "삼전": "삼성전자", "하닉": "SK하이닉스", "현차": "현대차",
    "전자·하이닉스": "삼성전자", "만전자": "삼성전자", "만닉스": "SK하이닉스",
}
# 두 글자 상장사명('대한'·'삼양' 등)은 일상어와 충돌해 오탐을 만든다.
_MIN_NAME_LEN = 3


def foreign_query(broker: dict, *, market: bool = False) -> str:
    """해외 증권사 인용 기사 검색어.

    **두 갈래로 나눈다.** 실측(2026-07-29)에서 하나로는 안 됐다.

      market=True   … + (코스피 OR 코스닥 …)  — 국내 시장 전망. 전부 채택한다
      market=False  … 넓게 — 개별 종목 콜. 제목에 국내 상장사가 있을 때만 채택

    시장 조건만 걸면 "맥쿼리, SK하이닉스 목표주가 290만원" 처럼 코스피를 언급
    하지 않는 개별 종목 콜을 통째로 잃는다. 반대로 넓게만 걸면 Investing.com
    이 번역한 미국 종목 기사가 대부분을 차지한다 — 둘 다 실측으로 확인했다.
    """
    names = [broker["name"], *(broker.get("aliases") or [])][:3]
    who = " OR ".join(f'"{n}"' for n in names)
    q = f"({who}) {_VOCAB}"
    return f"{q} ({' OR '.join(KR_MARKET_TOKENS[:5])})" if market else q


def is_korea_related(title: str, names: set[str]) -> bool:
    """제목이 국내 시장·국내 상장사 얘기인가.

    `names` 가 비어 있으면(DART 캐시 없음) 시장 용어만으로 판정한다 — 모르는
    것을 통과시키는 대신 좁게 잡는다. 해외사 항목은 어차피 2차 정보라
    누락보다 오염이 비싸다.
    """
    t = title or ""
    if any(tok in t for tok in KR_MARKET_TOKENS):
        return True
    if any(nick in t for nick in KR_NICKNAMES):
        return True
    return any(n in t for n in names if len(n) >= _MIN_NAME_LEN)


def parse_foreign_feed(xml_text: str, broker: dict, since: datetime | None,
                       names: set[str] | None = None,
                       require_korea: bool = False) -> list[Report]:
    """구글 뉴스 RSS → Report(category='company', source='news').

    본문을 따라가지 않는다. 해외사 항목은 '원문'이 아니라 인용이므로, 링크와
    제목까지가 우리가 정직하게 주장할 수 있는 전부다.
    """
    import hashlib

    from .rss import parse_feed  # 기존 파서 재사용 (형식이 같다)

    out: list[Report] = []
    for it in parse_feed(xml_text):
        pub = it["published"]
        if since and pub <= since:
            continue
        if require_korea and not is_korea_related(it["title"], names or set()):
            continue
        uid = hashlib.sha256(it["link"].encode()).hexdigest()[:32]
        out.append(Report(
            source="news", source_uid=uid, broker=broker["name"],
            category="company", title=it["title"], url=it["link"],
            published_at=pub, summary=it.get("description") or None,
            evidence={"press": it.get("source") or "", "quoted": True},
        ))
    return out


# ---------------- 네트워크 ----------------

async def _get(client: httpx.AsyncClient, url: str, *, params: dict | None = None,
               encoding: str | None = None) -> str:
    r = await client.get(url, params=params, headers={"User-Agent": _UA})
    r.raise_for_status()
    if encoding:
        return r.content.decode(encoding, errors="replace")
    return r.text


async def fetch_naver(pages: int = 1, categories: list[str] | None = None
                      ) -> list[Report]:
    """네이버 리서치 5분류 × pages 페이지. 페이지당 30건."""
    wanted = categories or list(NAVER_CATEGORIES)
    out: list[Report] = []
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        for page_name in wanted:
            cat = NAVER_CATEGORIES.get(page_name)
            if not cat:
                continue
            for page in range(1, max(1, pages) + 1):
                try:
                    html = await _get(client, f"{NAVER_BASE}/{page_name}_list.naver",
                                      params={"page": page}, encoding="euc-kr")
                except Exception as e:  # noqa: BLE001 — 분류 단위 격리
                    log.warning("네이버 리서치 %s p%d 실패: %s", page_name, page, e)
                    break
                rows = parse_naver_list(html, cat)
                if not rows:
                    log.warning("네이버 리서치 %s p%d 파싱 0건 — 표 구조 변경 의심",
                                page_name, page)
                    break
                out.extend(rows)
    return out


async def fetch_consensus(days: int = 3, pagenum: int = 40) -> list[Report]:
    """한경컨센서스 — 최근 days 일. 목록에 목표가·투자의견이 함께 온다."""
    today = datetime.now(KST).date()
    sdate = (today - timedelta(days=max(0, days - 1))).isoformat()
    out: list[Report] = []
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        for rtype, cat in CONSENSUS_TYPES.items():
            try:
                html = await _get(client, CONSENSUS_URL, params={
                    "sdate": sdate, "edate": today.isoformat(),
                    "report_type": rtype, "pagenum": pagenum})
            except Exception as e:  # noqa: BLE001 — 분류 단위 격리
                log.warning("한경컨센서스 %s 실패: %s", rtype, e)
                continue
            rows = parse_consensus_list(html, cat)
            if not rows:
                log.warning("한경컨센서스 %s 파싱 0건 — 표 구조 변경 의심", rtype)
            out.extend(rows)
    return out


async def fill_naver_targets(reports: list[Report], limit: int = 20) -> int:
    """종목분석 리포트의 목표가·투자의견을 상세에서 채운다(리포트당 1콜).

    limit 를 두는 이유: 첫 실행이면 수십 건이 한꺼번에 들어오는데, 그때 전부
    상세를 따라가면 한 사이클이 수십 콜이 된다. 못 채운 건은 목표가가 null 로
    남을 뿐 나머지 정보는 온전하다.
    """
    todo = [r for r in reports
            if r.source == "naver" and r.category == "company"
            and r.target_price is None][:max(0, limit)]
    if not todo:
        return 0
    filled = 0
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        for r in todo:
            try:
                html = await _get(client, r.url, encoding="euc-kr")
            except Exception as e:  # noqa: BLE001 — 건 단위 격리
                log.debug("네이버 리서치 상세 실패 %s: %s", r.source_uid, e)
                continue
            got = parse_naver_detail(html)
            if got["target_price"] or got["opinion"]:
                r.target_price = got["target_price"]
                r.opinion = got["opinion"]
                filled += 1
    return filled


async def fetch_foreign(broker: dict, since: datetime | None,
                        initial_days: int = 3,
                        names: set[str] | None = None) -> list[Report]:
    """해외 증권사 1곳의 인용 기사 — 시장 질의 + 종목 질의 (증권사당 2콜).

    종목 질의는 국내 상장사·시장 용어가 제목에 있을 때만 채택한다. 그렇지
    않으면 Investing.com 이 번역한 미국 종목 기사가 결과를 덮는다(실측).
    """
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(days=max(1, initial_days))
    out: list[Report] = []
    seen: set[str] = set()
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        for market in (True, False):
            try:
                xml = await _get(client, GOOGLE_RSS, params={
                    "q": foreign_query(broker, market=market),
                    "hl": "ko", "gl": "KR", "ceid": "KR:ko"})
            except Exception as e:  # noqa: BLE001 — 질의 단위 격리
                log.warning("해외 인용 조회 실패 %s(market=%s): %s",
                            broker.get("name"), market, e)
                continue
            for r in parse_foreign_feed(xml, broker, since, names,
                                        require_korea=not market):
                if r.source_uid not in seen:
                    seen.add(r.source_uid)
                    out.append(r)
    return out
