"""현재 시세 조회(ka10006) 파싱 — 등락률과 체결강도가 한 호출에 같이 온다.

## 왜 필요한가 — 게이트가 낡은 값으로 판정하고 있었다

매매 tier 승격은 `trade_tier_cap` 체결가능성 게이트로 결정한다(promote.py).
그런데 그 게이트가 보는 `Candidate.price` 는 소스마다 신선도가 다르다.

  volume/gainers/presurge  스캔 응답의 현재가      — 60초 이내
  nightly                  `picks["close"]`        — **발굴 배치일 종가**
  news                     TNM 항목의 가격         — 수집 시점

실측 2026-07-28: 펌텍코리아 승격 결정에 `체결가능 48,600원` 이라 적혀 있는데
그 시각 실제 현재가는 **47,050원(−3.19%)** 이었다. 48,600 은 7/27 종가다.
오늘 71번의 승격 판단을 전부 어제 가격으로 했다. 이번엔 갭이 3% 라 판정이
뒤집히지 않았지만, 상한 근처에서 갭이 크면 뒤집힌다.

**이건 예측이 아니라 판정 재료의 신선도 문제**다. 1.5단계가 남기기로 한
"유동성·체결가능성 게이트" 의 정확성 그 자체다.

## 체결강도는 기록만 한다

`cntr_str` 은 매수/매도 체결량 비율(100 = 균형)이고 **어느 소스도 쓰지 않는
새 정보**다. 그래서 좋은지 나쁜지 **모른다**. 발굴 3규칙 점수도 '그럴듯해서'
넣었고 1년 뒤 이벤트 스터디에서 알파가 아니라 베타 프록시로 판명됐다.

그래서 이 값은 `decisions` 원장에 **싣기만 하고 판단에 쓰지 않는다.** 4주 뒤
소스별 기여도 측정에서 "체결강도가 실제로 성적과 상관있나" 를 처음으로 물을
수 있게 하는 것이 목적이다. 유의하면 그때 게이트로 올린다.

실측 표본(2026-07-28 장중): 펌텍코리아 118.53 · NAVER 98.66 ·
마키나락스 75.04 · 삼성전자 71.54.
"""


def _num(v, cast=float):
    try:
        return cast(float(str(v).strip().lstrip("+") or 0))
    except (TypeError, ValueError):
        return cast(0)


def parse_quote(raw: dict) -> dict | None:
    """ka10006 → {price, change_pct, cntr_str, volume}. 실패하면 None.

    **None 과 0 을 구분하는 것이 중요하다.** 조회 실패를 '가격 0' 으로 돌려주면
    게이트가 그걸 '1주도 못 사는 종목' 이 아니라 '가격 미확인' 으로 읽어야 하는데
    두 상태가 섞인다. 호출부가 폴백을 결정할 수 있게 None 을 낸다.

    `close_pric` 는 등락 방향이 **부호로** 실려 온다(하락이면 "-47050").
    가격은 절댓값이고 방향은 `flu_rt` 가 따로 준다 — account.py 와 같은 규약이다.
    """
    if raw.get("return_code") not in (0, "0", None):
        return None
    price = abs(_num(raw.get("close_pric")))
    if price <= 0:
        return None
    return {
        "price": price,
        "change_pct": _num(raw.get("flu_rt")),
        "cntr_str": _num(raw.get("cntr_str")),
        "volume": _num(raw.get("trde_qty"), int),
    }


def parse_watch_info(raw: dict) -> dict[str, dict]:
    """ka10095(관심종목정보) → {code: {price, change_pct, cntr_str, volume}}.

    `parse_quote` 와 **같은 모양**을 낸다. 호출부가 단건(ka10006)과 묶음(ka10095)을
    바꿔 쓸 때 형태가 달라지면 그 자리에서 분기가 생긴다.

    묶음으로 받는 이유는 콜 수다. ka10006 은 종목당 1콜이라 승격 후보가 늘면
    선형으로 증가한다 — 실측 2026-07-30 full 전환 직후 mrkcond 가 분당 72~85콜
    까지 올라 전체 예산의 절반을 먹었다. ka10095 는 89종목을 1콜로 받는다.

    필드명이 ka10006 과 다르다: 현재가가 `close_pric` 이 아니라 `cur_prc` 이고
    거래대금(`trde_prica`)이 추가로 온다. 가격에 붙는 +/- 는 전일대비 **방향**
    이므로 절댓값으로 읽는다.
    """
    if raw.get("return_code") not in (0, "0", None):
        return {}
    out: dict[str, dict] = {}
    for row in raw.get("atn_stk_infr") or []:
        code = str(row.get("stk_cd", "")).strip()
        price = abs(_num(row.get("cur_prc")))
        if not code or price <= 0:
            continue
        out[code] = {
            "price": price,
            "change_pct": _num(row.get("flu_rt")),
            "cntr_str": _num(row.get("cntr_str")),
            "volume": _num(row.get("trde_qty"), int),
            "trde_prica": _num(row.get("trde_prica"), int),
            "spread_pct": spread_pct(row),
            **extra_fields(row),
        }
    return out


def extra_fields(row: dict) -> dict:
    """ka10095 의 **쓰지 않고 버리던 필드들** — 추가 호출 0.

    63개 필드가 오는데 9개만 읽고 있었다. 나머지는 그냥 버렸다. 이 TR 은 승격
    게이트가 이미 매 사이클 부르므로, 적재 비용은 컬럼 몇 개의 디스크뿐이다.

    무엇을 왜 남기나 — 전부 **관측이고 판단에 쓰지 않는다**:

      req_imbalance   최우선 매수/매도 잔량비. 실측 3.46:1 같은 값이 나온다.
                      진입 직전에 매도벽이 있었는가를 사후에 물을 수 있는 유일한
                      기록이다. 지금은 남는 데가 없다.
      tot_imbalance   총 잔량비 — 위와 다른 시간축의 같은 정보.
      day_pos         **당일 시·고·저 안에서 현재가의 위치**(0=저가, 1=고가).
                      이번 실측이 정확히 이 값을 가리켰다: 진입가가 직전 30분
                      범위의 평균 0.67 지점이었고, 위치가 높을수록 이후 상승
                      여력이 단조로 줄었다(하단 1.52% → 상단 1.02% → 신고가
                      0.95%). 그런데 그 값을 **결정 시점에 남긴 적이 없어**
                      분봉으로 사후 재계산해야 했다. 남기지 않으면 4주 뒤에도
                      사후 재계산만 가능하다.
      vol_vs_prev     전일 거래량 대비 — presurge 와 다른 정규화.
      exp_price/qty   예상체결. 08:30~09:00 동시호가 정보라 **주문 없이**
                      프리마켓을 관측할 수 있는 유일한 창이다.
      limit_pos       상한가까지 남은 거리(%). 0 에 가까우면 상한가 근처다.

    값을 못 만들면 키를 **빼고** 돌려준다. 0 으로 채우면 '측정했는데 0' 과
    '못 쟀다' 가 섞여, 4주 뒤 평균이 조용히 아래로 끌린다(스프레드 표본을
    따로 센 것과 같은 이유).
    """
    out: dict = {}

    def ratio(a: str, b: str, key: str) -> None:
        x, y = abs(_num(row.get(a))), abs(_num(row.get(b)))
        if x > 0 and y > 0:
            out[key] = round(x / y, 4)

    ratio("pri_buy_req", "pri_sel_req", "req_imbalance")
    ratio("tot_buy_req", "tot_sel_req", "tot_imbalance")

    price = abs(_num(row.get("cur_prc")))
    hi, lo = abs(_num(row.get("high_pric"))), abs(_num(row.get("low_pric")))
    if price > 0 and hi > lo > 0:
        out["day_pos"] = round((price - lo) / (hi - lo), 4)
        out["day_range_pct"] = round((hi - lo) / lo * 100, 4)
    op = abs(_num(row.get("open_pric")))
    if op > 0 and price > 0:
        out["from_open_pct"] = round((price - op) / op * 100, 4)

    prev = _num(row.get("pred_trde_qty_pre"))
    if prev:
        out["vol_vs_prev"] = round(prev, 4)

    exp_p = abs(_num(row.get("exp_cntr_pric")))
    if exp_p > 0:
        out["exp_price"] = exp_p
        out["exp_qty"] = _num(row.get("exp_cntr_qty"), int)

    upl = abs(_num(row.get("upl_pric")))
    if upl > 0 and price > 0:
        out["limit_pos"] = round((upl - price) / price * 100, 4)
    return out


def spread_pct(row: dict) -> float | None:
    """매수1·매도1 호가 간격(%). 낼 수 없으면 None.

    ka10095 는 5호가를 같은 행에 함께 준다(`buy_1th_bid`/`sel_1th_bid`) — 편입
    게이트가 이미 이 TR 을 부르므로 **추가 호출이 0** 이다.

    왜 재는가: 우리는 유동성을 거래대금 하한(30억)으로만 보는데, 스프레드는
    그와 다른 축이다. 거래대금이 커도 호가가 벌어진 종목은 왕복 비용이 모델값
    (0.33%)보다 커진다. 지금 확정률을 올리는 이유가 '이익이 비용에 먹힌다'
    인데, 확정선을 올리는 것은 대증요법이고 스프레드 넓은 종목을 안 잡는 것이
    원인 요법이다.

    **판단에 쓰지 않고 `decisions` 에 싣기만 한다.** 체결강도와 같은 규약이다 —
    문턱을 지금 손으로 정하면 1.5단계에서 폐기한 실수를 반복한다. 2주 뒤
    "스프레드가 넓은 종목의 성적이 실제로 나쁜가" 를 물을 수 있게 하는 것이
    이번 범위다.

    호가에 붙는 +/- 는 전일대비 방향이므로 절댓값으로 읽는다(가격 필드와 동일).
    """
    bid = abs(_num(row.get("buy_1th_bid")))
    ask = abs(_num(row.get("sel_1th_bid")))
    if bid <= 0 or ask <= 0 or ask < bid:
        return None                      # 장 전·거래정지·데이터 결손
    return round((ask - bid) / bid * 100, 4)
