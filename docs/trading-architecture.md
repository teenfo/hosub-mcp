# 트레이딩 시스템 아키텍처 & 기능 인벤토리

> 반자동 한국주식 트레이딩 시스템(키움 REST, 실계좌)의 단일 정리 문서.
> 코드 위치: `trading/`(백엔드, systemd `trading.service`, :8600) + `src/dashboard.py`(프록시) + `static/pages/trading.js`·`backtest.js`(UI).
> 스키마·규칙·경로가 바뀌면 이 문서와 Notion "야간 종목 분석 리포트 작성 룰"을 함께 갱신한다.

## 전체 데이터 흐름

```
[키움 REST/WS] ──시세──> collector(1분봉 집계) ──> store(SQLite bars)
      │                                              │
      │  야간(17:30) 전종목 일봉 ──> discovery ──> features.csv + market(국면)
      │                                              │
      ▼                                              ▼
   engine.run_once(60초, 장중) ── rules.REGISTRY 평가 ──> 신호
      │   게이트: 잔고동기화 → 일일가드 → 국면(인버스) → 롱전용 → 리스크사이징
      ▼
   orders(승인대기 큐) ──[사용자 승인]──> 키움 발주(증거금 자동조정) ──> ledger(성과 로그)
      │                                                    │
      └── TTL 만료/거부                                     └── 자동청산(손절)·목표승인·장부
```

## 상시 루프 (main.py lifespan 에서 기동)

| 루프 | 주기 | 조건 | 역할 |
|---|---|---|---|
| `engine.loop` | 60초 | 평일 09:00~15:30 | 감시목록 백필 + 규칙 평가 + 승인대기 생성 |
| `engine.roster_loop` | 15분 | 평일 09:00~15:40 | 감시목록 이탈 종목 백필(수집 연속성, 유예 30일) |
| `scanner.loop` | 60초 | 평일 장중 | 거래대금상위·거래량급증·KOSPI 급등률(자동편입) |
| `discovery.loop` | 매일 | 평일 17:30 | 전종목 일봉 수집·스크리닝·국면 산출·auto 편입 |
| `reporter.loop` | 매일 | 평일 15:40 | 분봉 축적분 자동 백테스트 리포트 |
| `_ledger_loop` | 30초 | 장중 | 손절 자동청산(B모드)·목표 도달 청산 승인 제안 |
| WS feed | 실시간 | 장중 | 감시목록 체결틱 → 1분봉 집계 + 주문체결 수신 |

## 매매 규칙 (트레이딩 테크닉) — `signals/rules.py`

**레지스트리 패턴**: `@register("이름")` 데코레이터로 등록하면 `evaluate_all` 이 자동 순회.
새 기법 추가 = ① 함수 작성 ② 데코레이터 ③ `config.yaml rules:` 블록. 다른 코드 수정 불필요.
개별 규칙 예외는 격리되어 다른 규칙 평가를 막지 않는다. 목록은 `GET /api/rules`.

| 규칙 | 방향 | 셋업 | 상태 |
|---|---|---|---|
| `orb` | 롱/숏 | 시초가 범위(09:00~15) 돌파. 손절=범위 반대끝(넓음 주의) | ✅ |
| `gap` | 롱/숏 | 시가갭 1%↑ 후 첫시간 범위 이탈 | ✅ |
| `momentum` | 롱 | VWAP 위에서 직전 20봉 고가 돌파+양봉. 타이트 손절 | ✅ |
| `pullback` | 롱 | 상승추세 20MA 눌림 후 반등 양봉. 타이트 손절 | ✅ |
| `bounce_fade` | 숏 | VWAP 반등 소진 페이드(딥리서치) | ⏸ 롱전용이라 비활성 |
| `breakdown_retest` | 숏 | 지지 붕괴 후 리테스트 실패 | ⏸ 롱전용이라 비활성 |

공통 안전장치: `rules.max_stop_pct`(손절폭 4% 상한, 와이드스탑 폐기).

## 신호 → 주문 게이트 체인 (engine.run_once)

1. **재시작 복원**: 오늘 발사분(`orders` 이력)을 `_fired` 에 복원 — 재배포 중복발주 방지
2. **잔고 동기화**: 실계좌 예탁자산 확인 실패 시 신규 신호 보류(유령 사이징 방지)
3. **일일 가드**: 실현손익이 목표(+1.5%)/한도(-1.5%) 도달 시 신규 진입 중단
4. **국면 게이트**: 유효국면(아래)이 강세면 인버스 ETF 매수 보류
5. **롱 전용**: 숏 신호는 기록만(현물 계좌 공매도 불가)
6. **리스크 사이징**: `position_size(자산, 거래당리스크%, 진입, 손절)` — 잔고·리스크 반영, qty<1 이면 사유 구분 기록(잔고 부족 vs 리스크 한도)

신호는 **금액 제한 없이 전부 기록**(최근 신호 카드, 감사용) — 주문 생성만 게이트.

## 시장 국면 (유효 국면) — 하락장 수익 전략의 핵심

`유효국면 = anchor(야간리포트 편향 ?? 전일 breadth) ± 당일 시가갭 보정` (약세/중립/강세)

- **전일 breadth**: discovery 가 산출(60일선 상회 비율; `datasets/latest.json → market.regime`)
- **당일 시가갭**: 감시목록 시가갭 중앙값(±0.5% 임계)
- **야간리포트 편향**: `/data/trading/night_bias.json` `{date, regime, reason, us_close}` — Cowork 야간 작업이 미국장 분석 후 기록(Notion 룰 3.5·5단계). date=오늘일 때만 반영
- 하락장 수익 vehicle: **인버스 ETF 매수**(`config.inverse_etfs`) — 롱 규칙이 그대로 포착, 강세장에서만 게이트로 차단

## 주문·집행 — `trade/orders.py`

- 승인대기(TTL 10분) → 사용자 승인(수량/금액 편집 가능) → 시장가 발주
- **증거금 부족 자동 재발주**: 키움 "N주 매수가능" 파싱 → 수량 자동 조정 → 최대 3회 재발주. 1주도 불가면 대기열 유지
- `return_code≠0` 은 거부 처리(유령 포지션 방지). 결과는 "최근 주문 결과" 이력에 표시
- 청산: 손절=자동 시장가(B모드), 목표=승인제(`propose_exit`). 키움 REST 는 네이티브 스톱주문 없음 → 서버 감시

## 감시목록 2-tier — `data/watchlist.py`

- `collect_only=0`(매매): 규칙 평가+주문 대상 / `collect_only=1`(수집전용): 데이터만
- source: `seed`(config 시드) / `manual`(사용자·보호됨) / `auto`(야간발굴 회전) / `gainer`(장중 급등률 회전)
- 수집 로스터(`data/roster.py`): 목록 이탈 후에도 30일 백필 지속(표본 연속성)

## API 요약 (`/api/trading/*` 프록시 경유)

status · orders(+승인/거부) · signals · prices(2초 폴링용) · **rules(기법 목록)** · watchlist(+add/remove/mode) · scanner · discovery(+run) · backtest/{code} · backtest/coverage · backtest/report(latest/run) · performance · risk(가드+국면) · bars/{code} · account · settings

## 설정 지도 (`trading/config.yaml`)

| 섹션 | 핵심 키 |
|---|---|
| `watchlist` | 최초 시드(운영 기준은 DB) |
| `inverse_etfs` / `regime_gate` | 인버스 목록 / 국면 게이트(use_open_gap, use_night_bias) |
| `scanner` / `gainers` | 급등 스캐너 / KOSPI 급등률 자동편입(top_n, trade_max_price) |
| `discovery` | 야간 발굴(auto_watch, regime breadth 임계) |
| `collection` | 수집 로스터(retention 30일) |
| `backtest` | 자동 리포트(min_days 3, keep 120일) |
| `risk` | long_only, 거래당 리스크(UI 조정), 일일 목표/한도(UI 조정), TTL |
| `rules` | 규칙별 파라미터 + max_stop_pct |
| `execution` | 체결 WS, auto_exit(stop_mode=auto) |

리스크 3종(목표/한도/거래당)은 UI 저장 시 `data/risk.json` 에 영속(재배포 유지).

## 운영 메모

- 배포: PR 머지 → `deploy_service("trading")` + `/opt/hosub-mcp` ff-merge(정적 파일). 재시작 안전(오늘 신호 복원, WS 자동 재구독)
- DB: `/data/trading/trading.db`(bars/orders/audit/positions/watchlist/roster), `datasets/`(피처·매니페스트)
- 야간 분석: Cowork 데스크톱 예약 작업 → Notion 룰 문서 기준 → `html/night-report/` + `night_bias.json`
- 검증 습관: `node --check`, `pytest tests -q`, 배포 후 라이브 스모크(로그·API)
