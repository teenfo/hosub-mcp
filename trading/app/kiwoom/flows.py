"""수급 파서 — 누가 샀고 누가 팔았나. 전부 **관측**이고 판단에 쓰지 않는다.

## 실호출로 확정한 것 (2026-07-31)

18개 후보 TR 을 훑어 15개가 열려 있음을 확인했다. 그중 이 파일이 쓰는 넷:

  ka10086  일별주가        mrkcond   20행  **OHLCV + 수급이 한 콜에 온다**
  ka10059  투자자·기관별    stkinfo   100행 13개 주체 세부
  ka10014  공매도 추이      shsa      공매도량·비중·평균가
  ka10015  일별 거래상세    stkinfo   **장전/장중/장후 거래량 비중**

`ka10086` 이 중심이다. 야간 발굴이 이미 전종목 일봉(`ka10081`)을 부르는데 그건
OHLCV 만 준다. `ka10086` 은 같은 1콜에 개인·기관·외국인 순매수 + **프로그램
매매(`prm`) + 외국인 지분율(`for_wght`) + 신용비율(`crd_rt`)** 까지 얹어 준다 —
프로그램·신용을 따로 부를 이유가 없어졌다(별도 TR 로 3콜 더 쓸 뻔했다).

다만 `ka10086` 은 **20행**이고 `ka10081` 은 600행이다. 우리 피처가 MA120·60일
고저를 쓰므로 일봉을 대체할 수는 없다. 병행이다.

## 부호가 두 번 붙는다 — 여기서도

    "ind": "--11681307"    개인 순매도 1,168만주

`ka10063` 에서 잡았던 것과 같은 함정이고, 수급은 **부호가 곧 정보**라 절댓값으로
뭉갤 수 없다. `observe.signed` 를 그대로 쓴다 — 규약을 두 벌 만들지 않는다.
"""

from .observe import signed
from .quote import _num


def _rows(raw: dict, key: str) -> list[dict]:
    """응답에서 배열을 꺼낸다. 실패(rc != 0)면 빈 목록."""
    if raw.get("return_code") not in (0, "0", None):
        return []
    return [r for r in (raw.get(key) or []) if isinstance(r, dict)]


def parse_daily_price(raw: dict) -> list[dict]:
    """ka10086 → 일자별 OHLCV + 수급. 최신일이 앞에 온다.

    `for_qty`·`for_netprps` 는 같은 값(외국인 순매수 수량)이고 `frgn` 은 다른
    값이다 — 응답에 중복 필드가 많아 **한 뜻에 한 컬럼만** 쓴다.
    """
    out = []
    for r in _rows(raw, "daly_stkpc"):
        day = str(r.get("date", "")).strip()
        if len(day) != 8 or not day.isdigit():
            continue
        out.append({
            "d": f"{day[:4]}-{day[4:6]}-{day[6:]}",
            "close": abs(_num(r.get("close_pric"))),
            "open": abs(_num(r.get("open_pric"))),
            "high": abs(_num(r.get("high_pric"))),
            "low": abs(_num(r.get("low_pric"))),
            "change_pct": signed(r.get("flu_rt")),
            "volume": int(_num(r.get("trde_qty"), int)),
            "trade_value_mn": int(_num(r.get("amt_mn"), int)),
            # 여기부터 수급 — 부호가 정보다
            "ind": signed(r.get("ind")),               # 개인 순매수(주)
            "orgn": signed(r.get("orgn")),             # 기관
            "foreign": signed(r.get("for_qty")),       # 외국인
            "program": signed(r.get("prm")),           # 프로그램매매
            "for_wght": signed(r.get("for_wght")),     # 외국인 지분율(%)
            "crd_rt": signed(r.get("crd_rt")),         # 신용비율(%)
        })
    return out


# ka10059 의 세부 주체. `orgn`(기관 합계)은 ka10086 이 이미 주므로 넣지 않는다 —
# 같은 값을 두 컬럼에 두면 어느 쪽이 진실인지 나중에 알 수 없다.
DETAIL_FIELDS = {
    "fnnc_invt": "금융투자", "insrnc": "보험", "invtrt": "투신",
    "etc_fnnc": "기타금융", "bank": "은행", "penfnd_etc": "연기금",
    "samo_fund": "사모펀드", "natn": "국가", "etc_corp": "기타법인",
    "natfor": "내외국인",
}


def parse_investor_daily(raw: dict) -> list[dict]:
    """ka10059 → 일자별 13개 주체 순매수. 최신일이 앞에 온다."""
    out = []
    for r in _rows(raw, "stk_invsr_orgn"):
        day = str(r.get("dt", "")).strip()
        if len(day) != 8 or not day.isdigit():
            continue
        row = {"d": f"{day[:4]}-{day[4:6]}-{day[6:]}"}
        row.update({k: signed(r.get(k)) for k in DETAIL_FIELDS})
        out.append(row)
    return out


def parse_short(raw: dict) -> list[dict]:
    """ka10014 → 일자별 공매도. 비중은 그날 거래량 대비 %."""
    out = []
    for r in _rows(raw, "shrts_trnsn"):
        day = str(r.get("dt", "")).strip()
        if len(day) != 8 or not day.isdigit():
            continue
        out.append({
            "d": f"{day[:4]}-{day[4:6]}-{day[6:]}",
            "short_qty": abs(_num(r.get("shrts_qty"))),
            "short_wght": signed(r.get("trde_wght")),
            "short_avg_pric": abs(_num(r.get("shrts_avg_pric"))),
        })
    return out


def parse_trade_detail(raw: dict) -> list[dict]:
    """ka10015 → 일자별 **장전/장중/장후 거래량 비중**.

    실측 삼성전자 2026-07-30: 장전 0.00% · 장중 99.24% · 장후 0.75%.
    NXT 프리마켓 관측이 어느 종목에서 의미가 있는지를 이 값이 가른다 —
    장전 비중이 0 인 종목을 아무리 봐도 나올 것이 없다.
    """
    out = []
    for r in _rows(raw, "daly_trde_dtl"):
        day = str(r.get("dt", "")).strip()
        if len(day) != 8 or not day.isdigit():
            continue
        out.append({
            "d": f"{day[:4]}-{day[4:6]}-{day[6:]}",
            "bf_mkrt_wght": signed(r.get("bf_mkrt_trde_wght")),
            "opmr_wght": signed(r.get("opmr_trde_wght")),
            "af_mkrt_wght": signed(r.get("af_mkrt_trde_wght")),
        })
    return out
