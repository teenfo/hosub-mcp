# 발굴 엔진 (Scout Engine)

종목 발굴의 **단일 통로**. 6개 소스의 신호를 원장에 쌓고, 취합 점수를 매기고,
게이트로 승격/강등을 판정해 감시목록에 적용한다. 코드는 `trading/app/scout/`.

## 왜 이 구조인가 — 폐기된 것들

2026-07 이전에는 5개 경로가 감시목록에 **직접** 썼다(야간발굴 상위5 ·
거래대금 전량교체 · 급등률 전량교체 · TNM promote · 스캐너). 실측된 결함:

- 전량 교체라 순위 경계 종목이 60초마다 들락날락 → WS 재구독 폭주
- source 귀속이 첫 편입에 영구 고정 → 기여도 측정 불가
- 발굴 경로에만 체결가능성 가드 부재 → 1주도 못 사는 고가주가 매매 대상
- 유동성 정의가 경로마다 다름, 동점 정렬이 API 응답 순서 의존
- 보유 종목이 순위에서 밀리면 삭제 → 손절이 최대 15분 묵은 가격으로 판정

엔진은 shadow(기록만) → collect(수집 tier) → full(전체) 로 각 3거래일 관찰을
거쳐 올라왔고, **2026-08-01 full 로 완전 흡수 완료** — 직접 편입 경로는 코드에서
삭제됐다(`watchlist.replace_*` 삭제, TNM `promote.py` 삭제, `discovery.py` →
`scout/nightly.py` 이관). 부활 방지 테스트: `test_nightly_absorption.py`.

## 신호 모델 — `model.py`

```python
Signal(code, name, source, kind, strength(0~1), raw, evidence, observed_at, ttl_sec)
```

| 소스 | 위치 | 원시값 → strength | TTL·비고 |
|---|---|---|---|
| `volume` 거래대금 상위 | `sources/intraday.py` | 순위 r → `1-(r-1)/N` | 장중, 짧은 감쇠 |
| `gainers` 등락률 상위 | `sources/intraday.py` | 순위 → 동일 | 장중 |
| `presurge` 거래량 급증 | `sources/intraday.py` | 급증률 로그 스케일 | **max_tier=collect 하드 룰**(측정 이력 없음) |
| `nightly` 야간 발굴 | `sources/slow.py` | **전 팔 0.667 고정**(`NIGHTLY_STRENGTH`) | `observed_at`=배치 시각 17:30 고정 — 감쇠·TTL 이 실제로 작동(아침 ≈0.43, ~22h 만료) |
| `news` 뉴스·공시 | `sources/slow.py` | TNM 점수/100 | `impact_direction` 악재·불명 차단, min_score 70 |
| `manual` 수동 | — | 1.0 | **점수 미산입**(고정핀) — 아래 참조 |

`observed_at` 재도장 금지: 소스가 같은 신호를 재수집해도 관측 시각을 갱신하면
감쇠·TTL 이 무력해진다(진단 2026-08-01 M3에서 수정).

## 점수 — `scoring.py`

```
total(code) = Σ_groups  max_{s ∈ group} ( strength × decay(age, half_life) )
그룹: intraday(volume/gainers/presurge) · daily(nightly) · news · human
```

- **그룹 내 max, 그룹 간 합** — 장중 3소스는 같은 팩터(당일 모멘텀)의 세 뷰라서
  더하면 베타 노출을 3배 증폭한다. 서로 다른 정보원이 겹칠 때만 점수가 오른다.
- **가중치는 전 소스 1.0 고정.** 잔차 IC 가 실현 R 로 환산되지 않음이 실측돼
  (2026-07-27) 학습도 수동 조정도 하지 않는다.
- **`human` 그룹은 점수 미산입**(UNSCORED). 수동 종목이 세기 1.0·무감쇠로 영구
  만점이 되어 매매 자리를 점유했던 실측(매매 28 중 22가 수동/seed) 때문. 수동은
  고정핀 — 목록에서 안 빠지되 자리는 예약 못 한다.
- **점수의 용도는 강등 순서와 화면 표시뿐이다.** 매매 승격은 게이트가 결정한다.

## 승격/강등 — `promote.py`

매매 tier 승격 게이트 (**전부 통과해야 함** — 점수 임계 없음):

1. **유동성** (거래대금 하한)
2. **체결가능성** — `trade_tier_cap`(가격 상한, 예수금 대비), 가격을 모르면 승격하지
   않는다. nightly/news/manual 은 가격이 낡으므로 승격 결정 시 실측가를 붙인다
3. **probation** — 수집 tier 에서 **완료된 세션** `probation_sessions`(1) 이상 체류.
   세션 완료 = 15:30 마감 후에만 계상 → 주말·야간 유입은 다음 거래일 하루를
   관찰하고 그 다음 날부터 승격 가능(진단 2026-08-01 M1/M2에서 달력일→세션 교정)
4. **상한** — `max_trade`(40, 레이트리밋에서 유도된 값. 같은 사이클 승격분 포함 계산)

강등·회전:

- 히스테리시스: `promote_collect: 0.35` / `demote_below: 0.25`, `min_dwell_min: 15`
- **장 마감 후 강등 금지**(`in_session`) — 장중 소스가 보고할 수 없는 시간대의
  '신호 없음'을 "후보 아님"으로 읽었던 실사고(72종목 중 22 강등 결정)의 재발 방지
- 보유 포지션·manual/seed 는 강등하지 않는다. 감시목록 밖 보유는 상한을 잠식하지 않는다
- **기회비용 회전**: `silent_dwell_min`(390분=1거래일) 동안 신호 0건이면 대기 후보에게
  자리를 넘긴다. 성과가 아니라 **신호 유무**로 판정(과거 손익은 다음 거래를 예측하지
  않음이 실측: 상관 −0.050). 대기열은 probation 통과분만, 쿨다운 390분, 사이클당
  `max_rotate: 3`
- 서킷브레이커: 축소 `MAX_SHRINK`(10) · 상한 초과 강등 `max_demote`(10) · 회전 재료
  스냅샷 실패 시 그 사이클 회전 중단(`rotation_ready`)
- 동점 정렬은 일 단위 시드 md5 tie-break — 결정적이되 특정 종목코드에 편향되지 않는다

## 모드와 상태 — `engine.py`

- `MODES = shadow | collect | full`, 현행 **full**. 런타임 오버라이드
  `data/engine.json` > config > shadow. **shadow 는 이제 '기존 경로로 복귀'가 아니라
  '감시목록 동결'을 뜻한다** — 다른 쓰기 경로가 없기 때문.
- freeze(킬 스위치): 쓰기만 멈추고 현 상태 동결.
- 결정은 전 건 `decisions` 원장에 남는다(모드·적용 여부 포함). `apply()` 는 실제
  적용된 종목 집합을 돌려주고 원장은 **종목별로** 적용 여부를 기록한다 — 부분 실패가
  전 건 적용으로 기록되던 결함(H3/H4)의 수정.
- 재시작 직후 폭주 방어: 각 소스가 최소 1회 성공 폴을 마치기 전에는 적용하지 않는다.

## 야간 배치 — `nightly.py` (옛 discovery)

평일 17:30, 전종목 일봉 수집 → 피처 계산 → **3팔 표본** 저장 + 국면(breadth) 산출.

| 팔 | 선정 | 용도 |
|---|---|---|
| score | 3규칙 점수 상위 `top_n: 5` | 현행 점수의 실효 측정용 표본 |
| random | 유동성 통과 무작위 `random_fill: 40` | 대조군 |
| screen | 외부 문서의 조건식 | **`screen_fill: 0` 비활성** — 변동성 정합 대조군에서 우위 소멸(2026-08-01 측정). 부활 조건: 변동성 정합 대조군을 이길 것 |

세 팔 모두 엔진에는 **같은 세기(0.667)**로 들어간다 — 팔 간 차이는 선정 방식뿐이라
사후 비교가 성립한다(`picks.pick_kind` + `decisions` 로 4주 뒤 판정).

데이터 연속성 불변: DB 파일명 `discovery.db`, 표본 시드 문자열 `{day}:discovery` /
`{day}:screen` 은 **바꾸지 않는다** — 과거 표본과의 비교 가능성이 여기 걸려 있다.

진행 상태 영속화: 배치 중단 시 그날 일봉이 이미 있는 종목은 건너뛰고 재개한다
(배포가 배치를 끊어 1,200콜을 버린 실사고의 재발 방지).

## 관측 수집기 — 신호·점수·주문에 넣지 않는다

"측정 없이 정하지 않는다" 원칙의 입력 축적. 전부 **관측 전용**이고, 4주 뒤 물을
질문을 코드 docstring 에 미리 못박아 뒀다.

| 수집기 | 무엇 | 질문 |
|---|---|---|
| `flows.py` | 체결강도·투자자별·VI·수급 (감시목록 × 3콜) | 항목별 사전 질문 docstring 참조 |
| `bars_obs.py` | 매물대(10일 분봉)·VPIN(당일, BVC) — **API 콜 0** | 진입가의 밸류에어리어 내/외 ↔ 실현 R / 높은 VPIN **다음 날**이 나빴나 |
| `opening.py` | 개장 창 시장 상태 | — |
| `premarket.py` | NXT 프리마켓 | — |
| `observe.py` | 장중 시장 관측 | — |
| presurge `vol_3min` | 문서 정의(누적 거래량 델타) 급증 관측 | 문서식 vs 현행식 어느 쪽이 나은가 |
| `breakeven.py` (trade/) | 본전 이동 shadow — MFE·도달률 | 손절선을 진입가로 올렸다면 결과가 나았나 |

새 관측을 추가할 때의 규약: ① 신호·점수·승격·주문 어디에도 넣지 않는다 ② 판정
질문과 기준을 **구현 시점에** docstring 으로 못박는다 ③ API 예산을 늘리지 않는
경로를 우선한다(bars_obs 가 모범).

## TNM 과의 관계

TNM(:8602)은 뉴스·공시를 수집·LLM 분류·점수화하는 **별도 서비스**다. 발굴 엔진의
`news` 소스가 `GET /api/items` 로 소비한다(방향: TNM → trading 단방향).
TNM 의 직접 편입(promote)은 폐기됐다. DART 전종목 미매칭 공시는 보고서명 allowlist →
ticker 역매핑 → `origin='dart'` 등록 경로로 **새 종목**을 낼 수 있다(폐루프 해소).
서비스 토큰이 서로 다름에 주의(`settings.TNM_TOKEN`).

## 화면 — `static/pages/scout.js` "발굴 엔진"

두 판(nav-pills): **[운영 — 후보·감시목록]**(status·report·소스·감시목록) /
**[엔진 판단]**(엔진 상태·후보 큐·판단 vs 실제 diff·소스 원시·결정 이력, 미적용
건수 배지). 탭 상태는 sessionStorage. 라우터 별칭 `discover` → `scout`
(`static/app.js` PAGES aliases).
