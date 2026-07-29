"""증권사 목록 — 시드 데이터와 표기 정규화.

수집 대상 토글은 DB(tnm_brokers)가 소유한다. 이 모듈은 **최초 1회 시드**와
**소스 표기 → 등록된 증권사 매칭**만 담당한다. 사용자가 끈 증권사를 시드가
다시 켜는 일이 없도록, 시드는 `on conflict do nothing` 으로만 들어간다.

## 국내사와 해외사의 수집 경로가 다르다 — 그리고 대칭이 아니다

국내사는 리포트 원문 목록이 공개돼 있다(네이버 금융 리서치 · 한경컨센서스).
제목·목표주가·투자의견·PDF 링크까지 그대로 받는다.

**해외사는 리포트 원문을 공개 스크래핑할 수 없다.** 골드만삭스·모건스탠리
등은 기관 고객에게만 배포하고 웹에 목록조차 없다. 그래서 해외사는 국내 언론이
인용한 기사(구글 뉴스 RSS)를 모은다 — "골드만삭스, 삼성전자 목표가 상향" 같은
2차 정보다. 원문이 아니라는 것을 화면에도 `note` 로 적어 둔다. 이걸 국내사와
같은 것처럼 보여주면 없는 커버리지를 있다고 착각하게 된다.
"""
from __future__ import annotations

import re

# 국내 증권사 — 이름은 리서치 목록에 적히는 표기를 기본으로 한다.
# aliases 는 소스마다 다른 표기·과거 사명. 매칭이 실패하면 '미등록'으로 남고,
# 그 사실이 화면에 보이므로 여기 목록이 완벽할 필요는 없다.
SEED_DOMESTIC: list[tuple[str, tuple[str, ...]]] = [
    ("NH투자증권", ("NH투자", "우리투자증권")),
    ("한국투자증권", ("한국투자",)),
    ("삼성증권", ()),
    ("미래에셋증권", ("미래에셋대우", "미래에셋")),
    ("KB증권", ("KB투자증권", "현대증권")),
    ("신한투자증권", ("신한금융투자", "신한투자")),
    ("하나증권", ("하나금융투자", "하나대투증권")),
    ("키움증권", ("키움",)),
    ("메리츠증권", ("메리츠종금증권",)),
    ("대신증권", ()),
    ("교보증권", ()),
    ("유안타증권", ("유안타 리서치", "동양증권")),
    ("현대차증권", ("현대차투자증권",)),
    ("한화투자증권", ("한화증권",)),
    ("IBK투자증권", ("IBK",)),
    ("SK증권", ()),
    ("유진투자증권", ()),
    ("다올투자증권", ("KTB투자증권",)),
    ("iM증권", ("하이투자증권", "iM투자증권")),
    ("LS증권", ("이베스트투자증권", "이베스트")),
    ("DS투자증권", ()),
    ("신영증권", ()),
    ("BNK투자증권", ()),
    ("상상인증권", ("케이프투자증권",)),
    ("흥국증권", ()),
    ("부국증권", ()),
    ("한양증권", ()),
    ("유화증권", ()),
    ("리딩투자증권", ()),
    ("코리아에셋투자증권", ()),
]

_FOREIGN_NOTE = "원문 비공개 — 국내 언론 인용 기사만 수집한다"

# 해외 증권사 — 이름은 국내 기사에 실리는 한글 표기를 정본으로 삼는다.
# aliases 에 영문·약칭을 넣어 뉴스 검색과 매칭이 함께 걸리게 한다.
SEED_FOREIGN: list[tuple[str, tuple[str, ...]]] = [
    ("골드만삭스", ("Goldman Sachs", "골드만")),
    ("모건스탠리", ("Morgan Stanley",)),
    ("JP모건", ("JPMorgan", "JP Morgan", "제이피모건")),
    ("씨티그룹", ("Citi", "Citigroup", "씨티")),
    ("뱅크오브아메리카", ("BofA", "Bank of America", "메릴린치")),
    ("UBS", ("유비에스",)),
    ("HSBC", ()),
    ("노무라", ("Nomura",)),
    ("다이와증권", ("Daiwa",)),
    ("미즈호", ("Mizuho",)),
    ("맥쿼리", ("Macquarie",)),
    ("CLSA", ()),
    ("제프리스", ("Jefferies",)),
    ("번스타인", ("Bernstein",)),
    ("바클레이즈", ("Barclays",)),
    ("도이체방크", ("Deutsche Bank", "도이치방크")),
    ("BNP파리바", ("BNP Paribas",)),
    ("다이와캐피탈", ("Daiwa Capital",)),
]


def seed_rows() -> list[dict]:
    """시드 전체 — db.seed_brokers 가 소비한다."""
    rows = [{"name": n, "kind": "domestic", "aliases": list(a), "note": None}
            for n, a in SEED_DOMESTIC]
    rows += [{"name": n, "kind": "foreign", "aliases": list(a), "note": _FOREIGN_NOTE}
             for n, a in SEED_FOREIGN]
    return rows


_STRIP = re.compile(r"[\s()\[\]·,.\-_/]+")
# '리서치센터'·'리서치' 는 회사명이 아니라 부서명이라 떼고 비교한다.
# '증권'/'투자증권' 은 떼지 않는다 — '하나증권'과 '하나금융지주'는 다른 회사다.
_SUFFIX = re.compile(r"(리서치센터|리서치본부|리서치)$")


def norm(name: str) -> str:
    """표기 흔들림 제거 — 공백·괄호·구두점 제거 + 대문자 통일 + 부서명 제거."""
    s = _STRIP.sub("", str(name or "")).upper()
    return _SUFFIX.sub("", s)


def build_index(brokers: list[dict]) -> dict[str, dict]:
    """정규화 이름 → 증권사 행. 이름과 별칭을 같은 사전에 넣는다.

    충돌 시 먼저 등록된 쪽을 유지한다 — 별칭이 다른 회사의 정식명을 덮으면
    그 회사의 리포트가 통째로 다른 증권사로 귀속된다.
    """
    idx: dict[str, dict] = {}
    for b in brokers:
        for key in (b.get("name"), *(b.get("aliases") or [])):
            k = norm(key)
            if k and k not in idx:
                idx[k] = b
    return idx


def match(raw_name: str, index: dict[str, dict]) -> dict | None:
    """소스 표기 → 등록된 증권사. 없으면 None(미등록 — 수집은 계속한다)."""
    return index.get(norm(raw_name))
