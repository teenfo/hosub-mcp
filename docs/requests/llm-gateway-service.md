# 개발 요청서 — 공유 LLM 게이트웨이 서비스 (Docker)

> 외부 서비스(roxlogy·TNM·trading·hosub 자신)가 **맥 스튜디오의 Ollama** 를 함께 쓰기 위한
> 단일 게이트웨이를 만든다. 서비스마다 워커를 두지 않고 하나를 공유한다.
> 이 문서는 구현 전 설계서다 — 승인 후 `feature/` 브랜치에서 구현한다.

## 1. 배경과 목적

hosub 는 맥 스튜디오(M4 Max 48GB)의 Ollama 를 LLM 백엔드로 쓴다. 소비자가 여러 개로
늘어날 예정이다(roxlogy 훈련 분석, TNM 뉴스 요약·라벨링, trading 분석, hosub 운영 도구).

**서비스마다 워커를 두면 안 되는 결정적 이유 — 모델 스래싱**

맥 스튜디오는 메모리가 유한하다(48GB). 소비자들이 조율 없이 각자 호출하면:

```
roxlogy → qwen2.5:32b (약 20GB)  ┐
TNM     → qwen2.5:7b             ├ 동시 요청
trading → qwen2.5-coder:14b      ┘
   → 메모리 압박 → 모델 언로드/재로드 반복 → 응답 시간 수 배 폭증
```

이 조율은 **호출자 쪽에서는 불가능**하다. 중앙 게이트웨이가 큐로 직렬화해야만 해결된다.
부수적으로 모델 교체 한 곳, 사용량 추적, 백엔드 주소 변경 한 줄, 재시도 정책 일원화의
이점도 얻는다.

## 2. 확정된 결정

| 항목 | 결정 |
|---|---|
| 배포 형태 | **Docker 컨테이너** |
| API | **동기 + 비동기 잡 모두** |
| 인증 | 서비스별 토큰 (호출자 식별 = 사용량 귀속) |
| 포트 | **8603** (기존: trading 8600, tnm 8602, mcp 8700, dash 8701) |
| 백엔드 | 맥 스튜디오 Ollama, **LAN IP** (데스크톱이라 고정) |

## 3. 아키텍처

```
 roxlogy worker ─┐
 TNM            ─┤   POST /v1/generate   (짧은 요청, 동기)
 trading        ─┼─▶ POST /v1/jobs       (긴 분석, 비동기)   ┌──────────────┐
 hosub MCP/대시 ─┘   Authorization: Bearer <service token>   │ llm-gateway  │
                                                             │  :8603 (docker)│
                                                             │  · 역할 레지스트리
                                                             │  · 큐(동시성 제어)
                                                             │  · 잡 저장(SQLite)
                                                             │  · 사용량 기록
                                                             └──────┬───────┘
                                                                    │ LAN
                                                          ┌─────────▼──────────┐
                                                          │ 맥 스튜디오 Ollama  │
                                                          │ 192.168.0.x:11434  │
                                                          └────────────────────┘
```

**핵심**: 소비자는 큐를 갖지 않는다. 게이트웨이가 유일한 Ollama 접근 지점이다.

## 4. API 명세

모든 요청에 `Authorization: Bearer <service_token>` 필요. 응답은 JSON.

### 4.1 동기 생성 — `POST /v1/generate`
짧은 작업(수 초)용. 큐를 통과하지만 응답까지 대기한다.

```jsonc
// 요청
{ "role": "summarize", "prompt": "...", "system": "(선택, 역할 기본값 대체)" }
// 응답 200
{ "status": "ok", "role": "summarize", "model": "qwen2.5:7b",
  "response": "...", "eval_count": 812, "duration_ms": 3400, "queued_ms": 120 }
```
- 큐 대기가 `sync_max_wait`(기본 60s)를 넘으면 `202` + `{"status":"queued","job_id":...}`
  로 전환해 비동기로 넘긴다(호출자가 폴링). HTTP 타임아웃으로 잡을 잃지 않게 하기 위함.

### 4.2 비동기 잡 — `POST /v1/jobs`
긴 분석(수십 초~분)용. 즉시 `job_id` 반환.

```jsonc
// 요청
{ "role": "analyze_workout", "prompt": "...", "metadata": {"session_id": 123},
  "callback_url": "(선택) 완료 시 POST 로 결과 전송" }
// 응답 202
{ "job_id": "a1b2c3d4", "status": "queued", "position": 3 }
```

### 4.3 잡 조회 — `GET /v1/jobs/{job_id}`
```jsonc
{ "job_id": "a1b2c3d4", "status": "running|queued|succeeded|failed",
  "role": "analyze_workout", "model": "qwen2.5:14b",
  "response": "...", "error": null, "metadata": {...},
  "created_at": "...", "started_at": "...", "finished_at": "...", "attempts": 1 }
```
- 잡 목록: `GET /v1/jobs?status=&service=&limit=` (본인 서비스 것만 조회)
- 취소: `DELETE /v1/jobs/{job_id}` (queued 상태만)

### 4.4 메타 — `GET /v1/roles`, `GET /v1/status`, `GET /healthz`
- `/v1/roles` — 호출 서비스가 쓸 수 있는 역할·모델 목록
- `/v1/status` — 백엔드 온라인/보유 모델, 큐 길이, 현재 로드된 모델, 최근 처리량
- `/healthz` — 인증 없이 200 (컨테이너 헬스체크용)

## 5. 큐 / 동시성 설계 (이 서비스의 핵심)

**스래싱 방지가 존재 이유이므로 여기가 가장 중요하다.**

1. **전역 동시성 제한** — `MAX_CONCURRENCY`(기본 **1**). 48GB에서 32B 모델을 쓰면 1이 안전.
   작은 모델만 쓸 때를 위해 설정으로 올릴 수 있게 한다.
2. **모델 친화 스케줄링(model affinity)** — 큐에서 다음 잡을 고를 때 **현재 로드된 모델과
   같은 모델을 쓰는 잡을 우선** 선택한다. 모델 전환 횟수를 줄여 로드 비용을 크게 아낀다.
   단, 기아(starvation) 방지를 위해 대기 시간이 `MAX_STARVATION`(기본 5분)을 넘긴 잡은
   무조건 우선.
3. **`keep_alive` 전달** — Ollama 요청에 `keep_alive`(기본 `10m`)를 넣어 연속 요청 시
   모델이 언로드되지 않게 한다.
4. **우선순위** — `priority`(0=기본, 높을수록 먼저). 대화형(hosub MCP)은 높게,
   배치(roxlogy 야간 분석)는 낮게 두는 용도.
5. **재시도** — 백엔드 연결 실패·타임아웃은 지수 백오프(2s→4s→8s)로 최대 `MAX_RETRIES`(3).
   맥이 재부팅 중이어도 잡이 살아남는다.
6. **타임아웃** — 역할별 `timeout`(레지스트리). 초과 시 `failed(timeout)`.

## 6. 인증 / 멀티테넌시

`config/services.yaml` (커밋 금지, 토큰은 환경변수 참조):

```yaml
services:
  roxlogy:
    token_env: LLMGW_TOKEN_ROXLOGY
    allow_roles: ["analyze_workout", "coach_feedback", "summarize"]
    rate_limit_per_min: 30
  tnm:
    token_env: LLMGW_TOKEN_TNM
    allow_roles: ["summarize", "classify_news"]
  hosub:
    token_env: LLMGW_TOKEN_HOSUB
    allow_roles: ["*"]
```

- 토큰 → 서비스 식별 → **사용량 귀속 + 역할 접근 제어**
- 토큰 비교는 `hmac.compare_digest`
- 허용되지 않은 역할 요청은 403 (`{"status":"forbidden","allowed":[...]}`)

## 7. 역할 레지스트리

`config/roles.yaml` — 기존 `hosub-mcp/config/llm_registry.yaml` 형식을 계승하고 확장한다.

```yaml
backend:
  base_url: ${OLLAMA_URL}        # 예: http://192.168.0.x:11434
  keep_alive: 10m

roles:
  # --- hosub 운영 ---
  summarize:      { model: qwen2.5:7b,  timeout: 120, system: "...", options: {temperature: 0.3} }
  log_analyze:    { model: qwen2.5:14b, timeout: 180, system: "..." }
  translate:      { model: qwen2.5:14b, timeout: 180, system: "..." }
  code:           { model: qwen2.5-coder:14b, timeout: 240, system: "..." }
  general:        { model: qwen2.5:32b, timeout: 300, system: "..." }

  # --- roxlogy (HYROX 훈련 분석) ---
  analyze_workout: { model: qwen2.5:14b, timeout: 240, system: "너는 하이록스 코치다 ..." }
  coach_feedback:  { model: qwen2.5:32b, timeout: 300, system: "..." }

  # --- TNM ---
  classify_news:   { model: qwen2.5:7b,  timeout: 60,  system: "..." }
```

- 역할 이름은 **평면(flat)** 으로 두되, 접근 제어는 6절의 `allow_roles` 로 한다
  (네임스페이스 접두어보다 단순하고, 역할 공유·재사용이 쉽다).
- 모델 교체 = 이 파일 한 줄. **소비자 코드 무변경**이 이 설계의 핵심 가치.

## 8. 데이터 저장

SQLite 한 파일(`/data/llmgw.db`, 볼륨 마운트). **잡은 재시작에도 살아남아야 한다**
(hosub 의 인메모리 잡과 다른 점 — 배치 분석을 잃으면 안 됨).

```sql
CREATE TABLE jobs (
  id TEXT PRIMARY KEY, service TEXT NOT NULL, role TEXT NOT NULL, model TEXT,
  status TEXT NOT NULL,            -- queued|running|succeeded|failed|cancelled
  priority INTEGER DEFAULT 0,
  prompt TEXT NOT NULL, system TEXT, response TEXT, error TEXT,
  metadata_json TEXT, callback_url TEXT,
  attempts INTEGER DEFAULT 0,
  created_at TEXT, started_at TEXT, finished_at TEXT
);
CREATE INDEX idx_jobs_queue ON jobs(status, priority DESC, created_at);

CREATE TABLE usage (                -- 서비스별 사용량 집계
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, service TEXT, role TEXT,
  model TEXT, eval_count INTEGER, duration_ms INTEGER, status TEXT
);
```
기동 시 `running` 상태로 남은 잡은 `queued` 로 되돌린다(크래시 복구).

## 9. Docker 구성

```
llm-gateway/
├─ Dockerfile              # python:3.12-slim + requirements
├─ compose.yml
├─ requirements.txt        # fastapi/starlette, uvicorn, httpx, pyyaml
├─ config/
│   ├─ roles.yaml          # 커밋 대상
│   └─ services.yaml       # 커밋 대상(토큰은 env 참조만)
├─ app/
│   ├─ main.py             # 라우트
│   ├─ queue.py            # 스케줄러(동시성·친화·우선순위·재시도)
│   ├─ store.py            # SQLite
│   ├─ roles.py            # 레지스트리 로드/검증
│   ├─ auth.py             # 서비스 토큰
│   └─ ollama.py           # 백엔드 클라이언트
└─ .env.example
```

```yaml
# compose.yml (요지)
services:
  llm-gateway:
    build: .
    restart: unless-stopped
    ports: ["127.0.0.1:8603:8603"]      # 로컬만 노출, 외부는 Caddy/내부망 경유
    env_file: .env
    volumes:
      - ./config:/app/config:ro
      - llmgw-data:/data
    healthcheck:
      test: ["CMD", "python", "-c", "import httpx;httpx.get('http://127.0.0.1:8603/healthz').raise_for_status()"]
      interval: 30s
volumes: { llmgw-data: }
```

**네트워크 주의**: 기본 bridge 네트워크에서 컨테이너 → 맥 스튜디오 LAN IP(192.168.0.x)로
나가는 것은 호스트 라우팅을 타므로 그대로 동작한다. `host.docker.internal` 불필요.

`.env` (커밋 금지):
```
OLLAMA_URL=http://192.168.0.x:11434     # ← 맥 스튜디오 IP (DHCP 고정 할당 권장)
MAX_CONCURRENCY=1
LLMGW_TOKEN_ROXLOGY=...
LLMGW_TOKEN_TNM=...
LLMGW_TOKEN_HOSUB=...
```

## 10. 기존 hosub 코드 통합 (마이그레이션)

현재 hosub 는 `src/llm.py` 에서 **Ollama 를 직접** 호출한다(PR #102). 게이트웨이 도입 후에는
**Ollama 직접 호출 지점을 하나로 줄인다**:

| 대상 | 변경 |
|---|---|
| `src/llm.py` | Ollama 클라이언트 → **게이트웨이 클라이언트**로 교체 (`OLLAMA_URL` → `LLMGW_URL` + 토큰) |
| `src/tools/llm.py` | 인터페이스 유지. 내부에서 게이트웨이 호출. `llm_generate` 에 `async_job` 옵션 추가 검토 |
| 대시보드 `/api/llm/*` | 게이트웨이 `/v1/status`·`/v1/generate` 프록시로 변경 (trading·tnm 프록시와 동일 패턴) |
| `config/llm_registry.yaml` | 게이트웨이 `config/roles.yaml` 로 이관 후 **제거** (중복 방지) |
| 대시보드 LLM 페이지 | 큐 길이·서비스별 사용량 표시 추가 |

> 중요: 역할 정의가 두 곳에 남으면 반드시 어긋난다. **게이트웨이를 단일 소스**로 한다.

## 11. 배포·운영

- `config/registry.yaml` 에 서비스 등록(다른 서비스와 동일하게 대화로 재시작·배포 가능).
  Docker 이므로 `unit: docker-llm-gateway.service` 형태의 systemd 래퍼를 두거나,
  `deploy.steps` 에 `docker compose up -d --build` 를 넣는다 → **구현 시 택1**.
- 대시보드에서 상태 확인(큐·백엔드·사용량).
- 맥 스튜디오 준비물: `launchctl setenv OLLAMA_HOST 0.0.0.0` + 모델 pull.

## 12. 테스트 전략

- **단위**: 역할 레지스트리 검증, 토큰 인증(허용/거부/역할 제한), 큐 스케줄러
  (동시성 1 보장, 모델 친화 선택, 기아 방지, 우선순위, 재시도·백오프), 잡 저장/복구.
- **통합**: 가짜 Ollama 서버(로컬 stub)로 동기·비동기 전체 흐름, 백엔드 다운 시 재시도,
  타임아웃 처리, 크래시 후 `running`→`queued` 복구.
- **스래싱 회귀 테스트**: 서로 다른 모델 잡 10개를 동시 투입 → 실제 모델 전환 횟수가
  잡 수보다 현저히 적은지(친화 스케줄링 동작) 검증.
- 실제 맥 없이 전부 통과해야 한다(hosub 기존 테스트 원칙과 동일).

## 13. 완료 기준

1. 컨테이너 기동 후 `/healthz` 200, `/v1/status` 에 백엔드 온라인·모델 목록 표시
2. 토큰 없이 호출 → 401, 허용 안 된 역할 → 403
3. 동기 `/v1/generate` 정상 응답, 긴 작업은 `/v1/jobs` → 폴링으로 완료 확인
4. 동시 다수 요청 시 **Ollama 동시 실행이 MAX_CONCURRENCY 를 넘지 않음**
5. 컨테이너 재시작 후에도 큐에 남아 있던 잡이 이어서 처리됨
6. hosub MCP `llm_generate` / 대시보드 LLM 페이지가 게이트웨이 경유로 정상 동작
7. `pytest` 전체 통과

## 14. 미결 사항 (구현 착수 전 확정 필요)

- [ ] **맥 스튜디오 LAN IP** — 현재 미상. `config/llm_registry.yaml` 에 잘못된 맥북
      Tailscale IP(100.107.151.46)가 들어가 있어 함께 정정 필요
- [ ] roxlogy 가 쓸 역할 정의(`analyze_workout` 등)의 실제 프롬프트 — roxlogy 데이터
      스키마 확인 후 확정 (필요 시 roxlogy 저장소를 세션에 추가)
- [ ] Docker 서비스 등록 방식: systemd 래퍼 vs compose 직접
- [ ] 외부 서비스가 hosub **밖**(다른 호스트)에서도 호출할 계획인지 → 그렇다면 8603 노출
      범위와 TLS(Caddy 경유) 설계 추가 필요. 현재 설계는 **같은 호스트/내부망** 전제

## 15. 참고

- 기존 LLM 게이트웨이 구현: `src/llm.py`, `src/tools/llm.py`, `static/pages/llm.js` (PR #102)
- 유사 서비스 패턴: `trading`(8600), `tnm`(8602) — INTERNAL_TOKEN + 대시보드 프록시
- 소비 예정 프로젝트: [roxlogy](https://github.com/teenfo/roxlogy) (HYROX 훈련 분석,
  hosub 를 백엔드 워커로 사용)
