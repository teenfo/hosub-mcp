"""DART 신규 종목 발굴 — 감시목록 **밖**의 공시에서 관심 종목을 찾아낸다.

## 왜 필요한가

지금 뉴스 소스는 **원리적으로 새 종목을 낼 수 없다.** TNM 관심종목이 trading
감시목록에서 동기화되고, 공시는 그 관심종목에 매칭될 때만 적재되기 때문이다.
전종목 모드는 사이클마다 매칭 안 되는 공시 ~1,120건을 **세기만 하고 버린다**
(collect.py 의 `counts["unmatched"]`). `promote` 가 늘 `already` 만 내는 이유다.

## 왜 allowlist 인가

전부 통과시키면 LLM 분류 물량이 수십 배가 된다. 그리고 대부분은 정기보고서·
지분보고 같은 일상 공시라 매매와 무관하다. 보고서명으로 **먼저** 걸러야
분류기가 감당할 수 있다.

## 무엇을 통과시키는가 — 실측 근거

2026-07-27 소급 측정(4,830건 / 123종목 / 1년, 본페로니 |t| > 2.81):

    실적      익일 초과수익 +1.625%  t = 6.48   ← 통과
    공급계약  익일 초과수익 +1.638%  t = 3.81   ← 통과
    그 외 10개 카테고리는 문턱 미달

분류기 카테고리 12개 중 **둘만 살아남았다.** 그래서 allowlist 의 중심은 그
둘이고, 나머지는 '수익률이 아니라 운영상 반드시 알아야 하는 것' 만 넣는다 —
거래정지는 보유 중이면 청산 자체가 불가능해지는 사건이고, 정정공시는 앞선
공시의 내용을 뒤집는다.

**주의**: 위 측정은 *분류기 카테고리* 기준이고 여기서 거르는 것은 *보고서명* 이다.
둘은 다르다 — 보고서명은 분류 전에 보이는 유일한 단서이고, 분류를 거쳐야
카테고리가 나온다. 그래서 이 목록은 "실적·공급계약으로 분류될 법한 보고서명"
의 근사다. 통과분이 실제로 어느 카테고리로 분류되는지는 사후에 확인할 수 있다.
"""
import logging
import re

log = logging.getLogger(__name__)

# 보고서명 → 통과 사유. 사유를 남기는 이유는 나중에 "왜 이 종목이 들어왔나" 를
# 물을 수 있어야 하기 때문이다. 공백·중점(ㆍ·)이 표기마다 달라 정규화 후 비교한다.
ALLOW = {
    # --- 실측으로 살아남은 둘 ---
    # '실적' 두 글자로 매칭하면 "증권발행실적보고서"(자금조달 사후보고)가
    # 걸린다 — 실서버 예행에서 실제로 걸렸다. 보고서 형식을 특정한다.
    "실적": ("매출액또는손익구조", "영업잠정실적", "영업실적공정공시",
             "손익구조30", "결산실적공시"),
    "공급계약": ("단일판매공급계약", "공급계약체결"),
    # --- 수익률이 아니라 운영상 반드시 알아야 하는 것 ---
    #     보유 중 거래정지는 청산 자체가 불가능해지는 사건이다.
    "거래정지": ("매매거래정지", "상장폐지", "관리종목지정", "투자주의환기"),
    # --- 자본 변동: 측정 문턱은 못 넘었지만 주가에 직접 작용하는 사건 ---
    #     (자금조달 −0.829% t=−1.99, 증설투자 −0.668% t=−1.97 — 문턱 2.81 미달.
    #      방향이 음수 쪽이라 '호재 후보' 로 쓰진 않지만 관측은 한다.)
    "자본변동": ("유상증자결정", "무상증자결정", "자기주식취득결정",
                 "자기주식처분결정", "전환사채권발행결정",
                 "신주인수권부사채권발행결정"),
}

# 명시적 제외 — 일상 공시. 정정판이 통과해 버리는 것들도 여기서 막는다.
# 실서버 예행(2026-07-27)에서 실제로 통과했던 것: 증권발행실적보고서,
# 대규모기업집단현황공시.
DENY = ("분기보고서", "반기보고서", "사업보고서", "감사보고서", "결산공고",
        "대량보유상황보고", "소유상황보고", "의결권", "주주총회소집",
        "기업설명회", "IR개최", "합병등종료보고서",
        "증권발행실적", "대규모기업집단", "현황공시", "지배구조보고서",
        "사외이사", "특수관계인")

_NORM = re.compile(r"[\s()\[\]ㆍ·・,.\-_/]+")
# 앞머리 대괄호 표기: [기재정정] [첨부정정] [첨부추가] [연결] 등
_BRACKET = re.compile(r"^\[([^\]]{1,24})\]\s*")


def normalize(name: str) -> str:
    """보고서명 표기 흔들림 제거 — 공백·괄호·중점 등."""
    return _NORM.sub("", str(name or ""))


def strip_correction(report_nm: str) -> tuple[str, bool]:
    """앞머리 [기재정정] 류를 떼고 (본 보고서명, 정정여부) 를 돌려준다.

    **정정을 통째로 통과시키면 안 된다.** '정정' 만 보면 "[기재정정]분기보고서"
    나 "[기재정정]대규모기업집단현황공시" 까지 들어온다 — 실서버 예행에서
    후보 191건 중 59건(31%)이 그런 것들이었다.

    정정이 중요한 이유는 **무엇을 정정했는가** 다. 그래서 앞머리를 떼고 본
    보고서명으로 판정한다. 실적 정정은 실적으로, 분기보고서 정정은 일상 공시로
    떨어진다.
    """
    raw = str(report_nm or "").strip()
    m = _BRACKET.match(raw)
    if not m:
        return raw, False
    tag = m.group(1)
    fixed = "정정" in tag or "추가" in tag
    return raw[m.end():].strip() or raw, fixed


def classify_report(report_nm: str) -> str | None:
    """보고서명 → 통과 사유. 통과하지 못하면 None.

    DENY 를 먼저 본다 — 두 목록에 다 걸리면 일상 공시 쪽이 맞다.
    """
    base, _fixed = strip_correction(report_nm)
    n = normalize(base)
    if not n:
        return None
    if any(d in n for d in map(normalize, DENY)):
        return None
    for reason, keys in ALLOW.items():
        if any(normalize(k) in n for k in keys):
            return reason
    return None


def candidates(pairs: list[tuple], known: set[str],
               watched_tickers: set[str]) -> list[dict]:
    """매칭 안 된 공시 → 신규 종목 후보.

    pairs: `[(corp_code, RawDoc, meta)]` — collect 가 넘긴 전종목 공시
    known: 이미 감시 중인 corp_code (매칭된 것은 후보가 아니다)
    watched_tickers: 이미 관심종목인 ticker — corp_code 가 아직 안 채워진
        종목이 중복 등록되는 것을 막는다

    같은 종목이 한 사이클에 여러 공시를 내면 **첫 통과 사유 하나만** 남긴다.
    """
    out: dict[str, dict] = {}
    for corp_code, _doc, meta in pairs:
        if corp_code in known:
            continue
        ticker = str(meta.get("stock_code") or "").strip()
        # 비상장사는 stock_code 가 비어 있다 — 매매 대상이 아니다
        if not re.fullmatch(r"\d{6}", ticker) or ticker in watched_tickers:
            continue
        if ticker in out:
            continue
        reason = classify_report(meta.get("report_nm", ""))
        if not reason:
            continue
        out[ticker] = {
            "ticker": ticker,
            "name": meta.get("corp_name") or ticker,
            "corp_code": corp_code,
            "reason": reason,
            "report_nm": meta.get("report_nm", ""),
            "rcept_no": meta.get("rcept_no", ""),
        }
    return list(out.values())
