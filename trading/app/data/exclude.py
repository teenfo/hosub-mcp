"""매매·발굴 대상에서 뺄 종목 판별 — **단일 정책**.

종전에는 같은 판단을 두 곳이 서로 다르게 했다.
  · `signals/scanner._is_excluded` — 대소문자 정규화 없음, `KOACT`·`액티브` 누락
  · `discovery.is_excluded` — `.upper()` 정규화, 접두·접미 구분

그 결과 실제 손실이 났다(2026-07-27): **462900 KoAct 바이오헬스케어액티브**를
scanner 경로가 통과시켜 실매수했고(orb, 09:22 13,815 → 12:51 13,600, −1.84%),
같은 종목을 discovery 경로는 차단했다.

반대 방향 오탐도 있었다. scanner 는 `리츠`를 **부분일치**로 봐서
`메리츠금융지주`·`메리츠증권` 같은 정상 종목을 통째로 배제했다. discovery 는
이 함정을 알고 접미사로만 본다("'리츠'는 부분일치로 두면 '메리츠금융지주'가
오탐되므로 접미사로만 본다" — discovery.py 원 주석).

여기서 두 정책을 합치고, 앞으로는 어느 경로든 이 함수만 쓴다.

한계(알고 쓴다): 이름 문자열 판정이라 원리적으로 오탐이 남는다. 근본 해결은
종목 마스터에 **증권종류·시장구분 필드**를 얹어 이름을 보지 않는 것이다.
그때까지의 최선이 이 목록이다.
"""
import re

from .. import settings

# 운용사 브랜드 접두 — 이름이 이걸로 시작하면 ETF/ETN 이다
DEFAULT_PREFIXES = [
    "KODEX", "TIGER", "KOSEF", "ARIRANG", "HANARO", "TIMEFOLIO", "KOACT",
    "TREX", "PLUS", "RISE", "ACE", "SOL", "KBSTAR", "히어로즈", "마이다스",
]
# 부분일치로 봐도 안전한 낱말 (정상 종목명에 우연히 들어갈 일이 없는 것들)
DEFAULT_KEYWORDS = [
    "스팩", "ETN", "ETF", "레버리지", "인버스", "선물", "채권", "국채",
    "금리", "액티브", "커버드콜", "배당",
]
# 부분일치가 위험해 **끝자리로만** 보는 낱말
#   리츠 → 메리츠금융지주·메리츠증권 오탐
#   TR   → Total Return 표기라 이름 끝에 붙는다
DEFAULT_SUFFIXES = ["리츠", "TR"]

# 우선주 — 등락률 상위에는 저유동 우선주가 잘 걸린다.
# '3우B'·'우(전환)' 등 접미 변형까지 커버.
_PREF_RE = re.compile(r"[0-9]?우[0-9]?B?(\([^)]*\))?$")


def is_excluded(name: str, cfg: dict | None = None) -> bool:
    """ETF·ETN·리츠·스팩·채권형·우선주 등 대상에서 뺄 종목이면 True.

    cfg 를 주지 않으면 `config.yaml discovery` 섹션을 쓴다 — 정책이 하나이므로
    설정 자리도 하나다(`discovery.exclude_keywords/suffixes/prefixes`).
    """
    if cfg is None:
        cfg = settings.CONFIG.get("discovery", {})
    raw = name or ""
    n = raw.upper().replace(" ", "")
    if any(str(kw).upper() in n for kw in cfg.get("exclude_keywords", DEFAULT_KEYWORDS)):
        return True
    if any(n.endswith(str(sf).upper()) for sf in cfg.get("exclude_suffixes", DEFAULT_SUFFIXES)):
        return True
    if any(n.startswith(str(pf).upper()) for pf in cfg.get("exclude_prefixes", DEFAULT_PREFIXES)):
        return True
    # 우선주 판정은 원문으로 본다(공백 제거가 '우' 접미 판정을 바꾸지 않도록)
    return bool(_PREF_RE.search(raw.strip())) or "우선주" in raw
