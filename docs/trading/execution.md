# 매매 집행 — 신호 → 주문 → 체결 → 원장

감시목록(매매 tier)에서 실거래까지의 경로. 코드는 `trading/app/signals/` + `trade/`.

## 매매 규칙 — `signals/rules.py`

**레지스트리 패턴**: `@register("이름")` 데코레이터로 등록하면 `evaluate_all` 이
자동 순회. 새 기법 추가 = ① 함수 ② 데코레이터 ③ `config.yaml rules:` 블록.
개별 규칙 예외는 격리된다. 목록은 `GET /api/rules`.

| 규칙 | 방향 | 셋업 | 상태 |
|---|---|---|---|
| `orb` | 롱/숏 | 시초가 범위(09:00~15) 돌파. 범위 상한 + RVOL 필터 | ✅ |
| `gap` | 롱/숏 | 시가갭 1%↑ 후 첫시간 범위 이탈 | ✅ |
| `momentum` | 롱 | VWAP 위 직전 20봉 고가 돌파+양봉 | ✅ |
| `pullback` | 롱 | 상승추세 20MA 눌림 후 반등 양봉 | ✅ |
| `divergence` | 롱 | 가격 LL + RSI HL + 확인봉. 손절=저점−ATR 버퍼 | ⏸ **비활성 편입**(측정 전) |
| `bounce_fade` / `breakdown_retest` | 숏 | — | ⏸ 롱전용이라 비활성 |

공통: `rules.max_stop_pct`(손절폭 상한). UI 토글은 `data/rules.json` 에 영속 —
**config 와 다를 수 있고, UI 쪽이 사용자의 더 최근 결정이다.**

우선순위: `priority.py` 가 동점을 해소한다(전 규칙 priority 값 보유).

## 신호 → 주문 게이트 체인 (signals/engine.run_once)

1. **재시작 복원** — 오늘 발사분을 `_fired` 에 복원(재배포 중복발주 방지)
2. **잔고 동기화** — 예탁자산 확인 실패 시 신규 신호 보류(유령 사이징 방지)
3. **일일 가드** — 실현손익 목표/한도 도달 시 신규 진입 중단
4. **국면 게이트** — 유효국면 강세면 인버스 ETF 매수 보류
5. **롱 전용**(`risk.long_only`, 런타임 `risk.json`) — 숏 신호는 기록만.
   **false 로 바꾸면 현물 계좌에서 나갈 수 없는 숏 주문이 나간다** — 2026-07-29
   유령 포지션 실사고의 원인이었다. 인버스 ETF 롱이 하락 대응 수단이다
6. **리스크 사이징** — `position_size(자산, 거래당리스크%, 진입, 손절)`

신호는 금액 제한 없이 **전부 기록**(감사용) — 주문 생성만 게이트.

## 시장 국면 (유효 국면)

`유효국면 = anchor(야간리포트 편향 ?? 전일 breadth) ± 당일 시가갭 보정`

- 전일 breadth: nightly 배치 산출(60일선 상회 비율, `datasets/latest.json`)
- 야간 편향: `data/night_bias.json` — Cowork 야간 작업이 미국장 분석 후 기록,
  date=오늘일 때만 반영. append-only 이력화되어 결정론 대조군과 나란히 적재
  (40거래일 판정 예정 — [measurement.md](measurement.md))
- 하락장 수익 vehicle: 인버스 ETF 매수(`config.inverse_etfs`)

## 주문·집행 — `trade/orders.py`

- 승인대기(TTL 10분) → 사용자 승인(수량·금액 편집 가능) → 시장가 발주
- **증거금 부족 자동 재발주**: "N주 매수가능" 파싱 → 수량 조정 → 최대 3회
- `return_code≠0` 은 거부 처리. 키움 REST 는 네이티브 스톱주문이 없어 **서버가 감시**한다
- **유령 포지션 회수**: 주문번호는 받았지만 체결되지 않은 건은
  `ledger.reap_unfilled` 가 **계좌 잔고 대조**로 void 처리한다. 판별 기준은
  `fill_confirmed` 가 아니라 계좌다(WS 이벤트만 놓친 정상 체결이 존재한다)

## 매매 데스크 — `trade/desk.py` (고속 청산 감시)

보유 포지션(≤5)만 **2초 주기**로 판정한다. 핵심: **가격은 이미 실시간이다**
(WS 틱 → BarAggregator 메모리 스냅샷, API 콜 0). 빨라진 것은 판정 주기이지
호출이 아니다 — REST 는 스냅샷이 `stale_sec`(5초) 넘게 낡은 종목만 보충한다.

안전장치: `exit_pending` 을 발주 **전에** 세움(중복 발주 차단) · WS 미연결이면
주기 자동 강등 · 라인 갱신은 값이 바뀔 때만 DB 쓰기 · `execution.desk.enabled`
런타임 오버라이드(`data/desk.json`)로 배포 없이 on/off.

손절·익절 라인 실시간 갱신: `update_lines()` 훅 + `positions.stop_live/target_live`
(원본 stop/target 은 보존 — 갱신 규칙이 나빴을 때 되돌릴 기준). 트레일링 규칙은
`research/trailing.py` 측정을 근거로 운영한다.

## 원장·대사·일지

**원본은 증권사다 (2026-08-01 대치).** `trade/fills.py` 가 장중 60초 주기로
체결내역(ka10076) 스냅샷을 `broker_fills` 에 저장하고 시스템 positions 를
사실에 정렬한다: 유령 void(체결내역에 없는 당일 주문) · 놓친 청산 기록(계좌 0
+ 매도 체결) · 수동 매매 편입(`rule='external'`, 손절·목표 없음 — 감시·기록만,
자동 청산 없음). 실현손익 주값은 증권사 계산값(ka10074 순액, `broker_daily`)
이고 **일일 가드도 이 값으로 판정**한다(동기화가 낡으면 모델값 폴백,
`source` 로 구분). 시스템 positions 는 의도(손절·목표·규칙 귀속) 레이어로
남는다 — 의도는 증권사 원장에 존재하지 않는다. 대조 리포트: `GET /api/fills`.

- `trade/ledger.py`: positions·실현손익. `void` 상태는 모든 집계·일지·shadow 에서 제외
- **수동 청산 버튼은 실제 매도를 발주한다**(`execute_exit`) — 원장만 닫던 버그의
  회귀 테스트가 있다(`test_manual_exit.py`). 고아 정리는 별도 **제외(void)** 버튼
  (계좌 대조 확인 문구 포함)
- **대사(reconcile)**: `kiwoom/realized.py` 가 증권사 체결내역으로 실측 손익을
  붙인다. 멱등. 일지 생성(`journal.py`)이 집계 **전에** 1회 실행하고, 실패 시
  '실측 미반영'을 사실 목록에 남긴다(조용히 모델값을 쓰지 않는다). 모델 비용이
  손실을 과대계상한 실측(−1,796원/일)이 이 순서의 근거
- 화면 실현손익은 **증권사 라이브 값이 주값**, 원장(engine)값은 대조 표시

## 리스크 설정

리스크 3종(일일 목표/한도, 거래당 리스크)은 UI 저장 시 `data/risk.json` 영속.
`long_only` 도 같은 파일이다. **주문·매매 관련 설정 변경은 사용자 판단 사항** —
자동화 작업에서 임의로 바꾸지 않는다.
