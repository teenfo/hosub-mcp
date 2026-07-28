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
