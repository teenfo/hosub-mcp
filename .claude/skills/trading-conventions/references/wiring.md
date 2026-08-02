# 레시피 — API·화면·설정 배선 지도

한 기능이 화면까지 닿으려면 여러 파일을 지나간다. **한 곳이라도 빠지면 404/무표시로
조용히 실패한다** — 이 지도를 체크리스트로 쓴다.

## 새 API 엔드포인트

| # | 파일 | 할 일 |
|---|---|---|
| 1 | `trading/app/main.py` | `@app.get/post("/api/...")` + `Depends(require_auth)` |
| 2 | `src/dashboard.py` | 프록시 화이트리스트 정규식 — **GET 은 `_TRADING_GET_RE`, POST 는 `_TRADING_POST_RE`, 15초 넘게 걸리면 `_TRADING_SLOW_RE` 까지 세 곳** |
| 3 | `static/pages/*.js` | `fetchJSON("/api/trading/...")` 로 소비 (필요 시) |

- 무거운 작업은 엔드포인트에서 직접 돌리지 않는다 — `backtest/job.py` JOBS 에
  등록하고 `offload.run_job`(자식 프로세스) + 실행 중 가드(`_x_running` 패턴).
- 스트리밍 응답은 일반 프록시가 버퍼링한다 — 전용 Route 를 generic
  `{path:path}` 보다 먼저 등록 (prices/stream 선례).
- 조회성 엔드포인트 docstring 에 "조회성 — 주문 없음" 명시.

## 새 화면 요소

- 페이지: `static/pages/<이름>.js` + `pages/index.js` PAGES 등록.
  기존 페이지에 카드 추가가 우선 — 페이지를 늘리지 않는다.
- 주기 갱신은 `ctx.addTimer(setInterval(...))` (페이지 이동 시 자동 정리).
- 가격 표시는 셀 부분 갱신(`data-px` 패치) — 표 재렌더 금지(버튼이 흔들린다).
- 검증: `node --check static/pages/<파일>.js` + dash 재시작.

## 설정 추가

| 종류 | 위치 | 규칙 |
|---|---|---|
| 정적 설정 | `trading/config.yaml` | 값마다 근거 주석. 코드 DEFAULTS 와 동기 |
| 런타임 오버라이드 | `data/<기능>.json` | 배포 없이 켜고 끌 것: engine/risk/rules/desk 패턴 복제. 우선순위: 오버라이드 > config > 코드 기본값 |
| 관측 파라미터 | `scout.observe.<이름>` | — |

실거래에 닿는 새 동작은 `enabled: false` 로 배포하고 활성화는 별도(런타임)로.

## 백그라운드 루프 추가

`main.py` lifespan `create_task` 목록 + `docs/trading/architecture.md` 의
상시 루프 표 갱신. 루프 안: 예외 격리(한 사이클 실패가 루프를 죽이지 않게),
장중/평일 조건, 하루 1회면 job_marks.

## 문서 동기

구조·스키마·경로가 바뀌면 `docs/trading/` 해당 문서를 같은 PR 에서 갱신한다.
측정 결과·결정은 `docs/trading/measurement.md` 원장에 추가(기존 항목 수정 금지
— append 가 원칙).
