# 트레이딩 시스템 아키텍처

> 반자동 한국주식 트레이딩 시스템(키움 REST/WS, 실계좌)의 구조 문서.
> 코드: `trading/`(백엔드, systemd `trading.service`, :8600) + `src/dashboard.py`(프록시)
> + UI(`static/pages/trading.js`=매매 데스크 · `scout.js`=발굴 엔진 · `backtest.js`=성과·백테스트,
> 공용 헬퍼 `tradelib.js`). 스키마·규칙·경로가 바뀌면 이 문서와 Notion
> "야간 종목 분석 리포트 작성 룰"을 함께 갱신한다.

## 전체 데이터 흐름

```
[키움 REST/WS] ──시세──> collector(1분봉 집계) ──> data/store(SQLite bars)
      │                                              │
      │  야간(17:30) 전종목 일봉 ──> scout/nightly ──> picks(3팔) + features.csv + 국면
      │                                              │
      │  장중 순위·급증 ──> scanner(파싱·필터)  ──┐   │
      │  뉴스·공시     ──> TNM(:8602) ─────────┤   │
      ▼                                        ▼   │
   [발굴 엔진 scout/engine]  6소스 → 신호 원장 → 점수 → 게이트 승격 → 감시목록 (단일 통로)
      │                                              │
      ▼                                              ▼
   signals/engine.run_once(장중) ── rules.REGISTRY 평가 ──> 신호
      │   게이트: 잔고동기화 → 일일가드 → 국면(인버스) → 롱전용 → 리스크사이징
      ▼
   trade/orders(승인대기 큐) ──[사용자 승인]──> 키움 발주(증거금 자동조정) ──> trade/ledger
      │                                                    │
      └── TTL 만료/거부                    trade/desk(2초) ─┴─ 손절 자동청산 · 목표 승인제
                                                           │
                              kiwoom/realized 대사(reconcile) ── journal(매매일지)
```

**감시목록에 쓰는 주체는 발굴 엔진과 수동 조작뿐이다.** 과거의 직접 편입 5경로
(야간발굴 상위5·거래대금·급등률·TNM promote·스캐너)는 전부 폐기됐고, 부활 방지
테스트가 있다(`test_nightly_absorption.py`). 경위는 [scout-engine.md](scout-engine.md).

## 상시 루프 (main.py lifespan 에서 기동)

| 루프 | 주기 | 조건 | 역할 |
|---|---|---|---|
| `signals/engine.loop` | `scan_interval_sec` | 평일 09:00~15:30 | 매매 tier 백필 + 규칙 평가 + 승인대기 생성 |
| `engine.roster_loop` | 15분 | 평일 09:00~15:40 | 감시목록 이탈 종목 백필(수집 연속성, 유예 30일) |
| `engine.eod_backfill_loop` | 매일 | 평일 15:35 | 그날 분봉 확정 백필 |
| `scanner.loop` | 60초 | 평일 장중 | 거래대금·급등률·급증 조회/필터 — **표시·엔진 입력만**, 직접 편입 없음 |
| `scout.loop` | 40~60초 | 상시 | **발굴 엔진** — 소스 폴링·점수·승격/강등 적용 |
| `nightly.loop` | 매일 | 평일 17:30 | 전종목 일봉 수집·3팔 표본·국면 산출(배치) |
| `scout_observe.loop` | 장중 | 평일 | 개장 창 시장 상태 관측 |
| `scout_premarket.loop` | 매일 | 평일 아침 | NXT 프리마켓 관측 |
| `scout_flows.loop` | 장중 | 평일 | 체결강도·투자자별·VI·수급 관측(감시목록) |
| `scout_bars_obs.loop` | 매일 | 평일 15:55 후 1회 | 저장 분봉 기반 매물대·VPIN 관측 (API 콜 0) |
| `reporter.loop` | 매일 | 평일 15:40 | 분봉 축적분 자동 백테스트 리포트 |
| `rule_sweep.loop` | 주간 | 토 09:00 | 기법 파라미터 스윕 |
| `journal.loop` | 매일 | 평일 마감 후 | 매매일지 생성(대사 선행) |
| `_ledger_loop` | 30초 | 장중 | EOD 정리·유령 포지션 회수 등 원장 관리 |
| `_desk_loop` | 2초 | 장중, `desk` 활성 시 | **매매 데스크** — 보유 포지션 청산 감시(WS 가격) |
| WS feed | 실시간 | 장중 | 감시목록 체결틱 → 1분봉 집계 + 주문체결 수신 |

하루 1회 배치는 `job_marks`(디스크)로 완료를 표시해 **재시작해도 중복 실행되지
않는다.** 배치 진행 중 배포는 금물 — [operations.md](operations.md).

## 모듈 지도 (`trading/app/`)

| 패키지 | 역할 | 주요 파일 |
|---|---|---|
| `scout/` | 발굴 엔진 + 야간 배치 + 관측 수집기 | `engine.py`(관제 루프) `model.py`(신호 모델) `scoring.py`(취합 점수) `promote.py`(게이트 승격) `store.py`(scout.db) `nightly.py`(야간 배치, 옛 discovery) `sources/`(어댑터) `flows.py` `bars_obs.py` `opening.py` `premarket.py` `observe.py` |
| `signals/` | 매매 신호 | `engine.py`(장중 평가 루프) `rules.py`(기법 레지스트리) `indicators.py` `priority.py` `scanner.py`(순위 파싱·필터) |
| `trade/` | 집행 | `orders.py`(승인·발주) `ledger.py`(포지션 원장) `desk.py`(고속 청산 감시) `risk.py`(사이징) `breakeven.py`(본전 이동 shadow) `override.py` |
| `data/` | 시세·감시목록 | `collector.py`(WS 분봉 집계) `store.py`(bars DB·job_marks) `watchlist.py`(2-tier) `roster.py` `symbols.py` `exclude.py`(ETF·스팩 제외 정책) `admit.py` `regime_log.py` |
| `kiwoom/` | 증권사 API | `client.py`(REST+레이트리밋) `ws.py`(실시간) `account.py` `realized.py`(실현손익 대사) `flows.py` `observe.py` `quote.py` `venue.py` |
| `research/` | 측정 도구 | `panel.py`(가격 패널) `eventstudy.py`(IC·잔차화) `ranking.py`(랭킹 비교 하네스) `newsimpact.py` `kelly.py` `cases.py` `trailing.py` `profile.py`(매물대) `vpin.py` |
| `backtest/` | 백테스트 | `runner.py` `report.py` `sweep.py` `job.py`+`offload.py`(자식 프로세스 실행) |
| `notify/` | 알림 | 텔레그램 등 |

루트: `main.py`(FastAPI + lifespan), `settings.py`(설정·런타임 오버라이드),
`journal.py`(매매일지), `features.py`(일봉 피처).

## 감시목록 2-tier — `data/watchlist.py`

- `collect_only=0`(매매): 규칙 평가+주문 대상 / `collect_only=1`(수집전용): 데이터만
- source: `seed`(config) / `manual`(사용자) / 엔진 적용분(결정 원장에 근거 기록)
- 수동·seed 는 **고정핀** — 감시목록에서 빠지지 않지만 매매 자리를 예약하지도
  않는다(점수 미부여, 게이트는 동일 적용). 근거는 [measurement.md](measurement.md).
- 수집 로스터(`data/roster.py`): 목록 이탈 후에도 30일 백필 지속(표본 연속성)

## API 요약 (`/api/trading/*` 프록시 경유)

status · orders(+승인/거부) · signals · prices · rules · watchlist(+add/remove/mode) ·
scanner · nightly(+run) · scout(+엔진 상태·결정 이력) · profile · vpin · bars-obs/run ·
backtest/{code} · backtest/report · performance · risk · bars/{code} · account(+realized) ·
positions/{id}/close(실제 매도 발주) · positions/{id}/void(고아 정리) · desk · settings

프록시 화이트리스트는 `src/dashboard.py` — 새 엔드포인트는 여기도 열어야 화면에서 보인다.

## 설정 지도 (`trading/config.yaml`)

| 섹션 | 핵심 키 |
|---|---|
| `watchlist` | 최초 시드(운영 기준은 DB) |
| `inverse_etfs` / `regime_gate` | 인버스 목록 / 국면 게이트(use_open_gap, use_night_bias) |
| `scanner` / `gainers` | 순위 조회·필터 파라미터(편입 아님 — 엔진 입력) |
| `nightly` | 야간 배치 — 3팔 표본(top_n·random_fill·screen_fill)·bearish_top_n·국면 임계 |
| `scout` | **발굴 엔진** — mode·승격/강등 임계·probation·상한·회전·서킷브레이커·소스별 설정·observe |
| `collection` | 수집 로스터(retention 30일) |
| `backtest` | 자동 리포트(min_days, keep) |
| `risk` | long_only·거래당 리스크·일일 목표/한도·TTL (UI 조정분은 `data/risk.json`) |
| `rules` | 규칙별 파라미터 + max_stop_pct (UI 토글은 `data/rules.json`) |
| `execution` | 체결 WS·auto_exit·`desk`(고속 청산 감시, 런타임은 `data/desk.json`) |
| `research` | cost_pct 등 측정 공통 |

**config 기본값과 `promote.DEFAULTS` 같은 코드 기본값은 반드시 동기한다** — 드리프트가
운영/테스트 불일치 버그를 만든 전례가 있다(진단 2026-08-01 M6).

## 데이터 저장소

| 경로 (서버 `/data/trading/`) | 내용 |
|---|---|
| `trading.db` | bars(1m/1d) · orders · positions · watchlist · roster · job_marks · 관측 원장 다수 |
| `scout.db` | 발굴 신호 원장 · decisions(승격/강등 이력) · source_health · profile_obs · vpin_obs |
| `discovery.db` | 야간 배치 picks(**파일명 유지** — 데이터 연속성, scout/nightly 가 소유) |
| `datasets/` | features.csv · latest.json(국면) |
| `engine.json` `risk.json` `rules.json` `desk.json` `night_bias.json` | 런타임 오버라이드 — [operations.md](operations.md) |

## 운영 메모 (요약 — 상세는 operations.md)

- 배포: PR 머지 → `deploy_service("trading")`(백엔드) / `deploy_service("dash")` 또는
  `restart_service("dash")`(UI·프록시). 재시작 안전(오늘 신호 복원, WS 자동 재구독,
  배치 완료 표시는 디스크).
- 프로세스 분리: :8700 MCP / :8701 대시보드 / :8600 트레이딩 / :8602 TNM.
- 검증 습관: `node --check`, `pytest tests -q`(trading), 루트 `pytest tests tnm/tests -q`,
  배포 후 라이브 스모크(로그·API).
