# 개발 요청서 — 공유 LLM 게이트웨이 서비스 (Docker)

> 외부 서비스(roxlogy·TNM·trading·hosub 자신)가 **맥 스튜디오의 Ollama** 를 함께 쓰기 위한
> 단일 게이트웨이를 만든다. 서비스마다 워커를 두지 않고 하나를 공유한다.
> 이 문서는 구현 전 설계서다 — 승인 후 `feature/` 브랜치에서 구현한다.
>
> **rev.2** — 설계 리뷰 반영(레인 분리, 단일 응답 형태, 콜백 제한, 도커 네트워크,
> 프롬프트 소유권, 잡 DB 위치, 근거 정정).

## 1. 배경과 목적

hosub 는 맥 스튜디오(M4 Max 48GB)의 Ollama 를 LLM 백엔드로 쓴다. 소비자가 여러 개로
늘어날 예정이다(roxlogy 훈련 분석, TNM 뉴스 요약·라벨링, trading 분석, hosub 운영 도구).

### 왜 서비스마다 워커를 두지 않는가

우선 **정확히 해둘 것**: Ollama 자체도 `OLLAMA_MAX_LOADED_MODELS`·`OLLAMA_NUM_PARALLEL`
로 로드 모델 수와 병렬 요청을 관리하고, 메모리가 모자라면 요청을 큐잉한다. 즉 게이트웨이가
없다고 시스템이 붕괴하지는 않는다. (초안에서 "게이트웨이 없이는 스래싱을 막을 수 없다"고
쓴 것은 과장이었다.)

공유 게이트웨이를 두는 실제 근거는 다음 순서다:

1. **설정 단일 소스** — 모델 교체·백엔드 주소 변경이 한 곳. 소비자 코드 무변경. (가장 큼)
2. **잡 영속화·재시도** — 맥이 재부팅·슬립해도 배치 분석이 살아남는다. Ollama 는 못 해준다.
3. **인증·사용량 귀속·역할 제한** — 어느 서비스가 무엇을 얼마나 썼는지, 무엇을 쓸 수 있는지.
4. **우선순위/공정성** — 대화형 요청이 야간 배치에 묻히지 않게.
5. **모델 전환 최소화** — 같은 모델 잡을 묶어 처리(보조적 이득).

## 2. 확정된 결정

| 항목 | 결정 |
|---|---|
| 배포 형태 | **Docker 컨테이너** |
| API | **동기 + 비동기** — 내부적으로는 **모두 잡**으로 통일(4절) |
| 인증 | 서비스별 토큰 (호출자 식별 = 사용량 귀속 + 역할 제한) |
| 포트 | **8603** (기존: trading 8600, tnm 8602, mcp 8700, dash 8701) |
| 백엔드 | 맥 스튜디오 Ollama, **LAN IP** (데스크톱이라 고정) |
| 프롬프트 소유 | **호출자** (게이트웨이는 기본값만 제공 — 7절) |
| 스트리밍 | **1차 제외** (9절에 근거·로드맵) |

## 3. 아키텍처

```
 roxlogy worker ─┐                                        ┌──────────────────────┐
 TNM            ─┤  POST /v1/generate  (대기하며 결과)     │ llm-gateway :8603    │
 trading        ─┼─▶ POST /v1/jobs      (긴 분석, 폴링)  ─▶│  (docker)            │
 hosub MCP/대시 ─┘  Authorization: Bearer <service token>  │  · 역할=모델 정책     │
                                                           │  · 2레인 큐          │
                                                           │  · 잡 영속(SQLite)    │
                                                           │  · 사용량 기록        │
                                                           └──────────┬───────────┘
                                                                      │ LAN
                                                        ┌─────────────▼─────────────┐
                                                        │ 맥 스튜디오 Ollama         │
                                                        │ 192.168.0.x:11434         │
                                                        └───────────────────────────┘
```

**핵심**: 소비자는 큐를 갖지 않는다. 게이트웨이가 유일한 Ollama 접근 지점이다.

## 4. API 명세 — 모든 요청은 잡이다

리뷰 반영: 초안의 "60초 넘으면 202로 형태가 바뀜"은 **같은 엔드포인트가 두 응답 형태**를
반환해 모든 소비자가 분기해야 하는 함정이었다. **응답 형태를 하나로 통일**한다.

모든 요청에 `Authorization: Bearer <service_token>` 필요.

### 4.1 생성 — `POST /v1/generate`

```jsonc
// 요청
{
  "role": "analyze_workout",     // 필수 — 모델 정책 선택
  "prompt": "...",               // 필수
  "system": "...",               // 선택 — 호출자 프롬프트(없으면 역할 기본값)
  "wait": 30,                    // 선택 — 결과를 몇 초까지 기다릴지(기본 30, 0이면 즉시 반환)
  "priority": 0,                 // 선택 — 클수록 먼저
  "metadata": {"session_id": 123}// 선택 — 호출자 참조용, 그대로 보관·반환
}

// 응답 200 — 성공/대기 모두 같은 형태. job_id 는 항상 존재.
{
  "job_id": "a1b2c3d4",
  "status": "ok" | "pending" | "failed",
  "response": "..." | null,      // ok 일 때만 채워짐
  "error": null,
  "role": "analyze_workout", "model": "qwen2.5:14b",
  "queued_ms": 120, "duration_ms": 3400
}
```

- `status: "pending"` 이면 **같은 `job_id`로 폴링**하면 된다. 클라이언트는 분기 하나(`status`)만
  보면 되고, "동기/비동기"라는 개념을 몰라도 된다.
- `wait: 0` 으로 부르면 곧바로 `pending` → 사실상 비동기 제출.

### 4.2 잡 조회 — `GET /v1/jobs/{job_id}`
4.1 과 동일한 응답 형태에 타임스탬프·시도 횟수를 더한다.
```jsonc
{ "job_id":"a1b2c3d4", "status":"running", "role":"...", "model":"...",
  "response":null, "error":null, "metadata":{...}, "attempts":1,
  "created_at":"...", "started_at":"...", "finished_at":null, "queue_position": 2 }
```
- 목록: `GET /v1/jobs?status=&limit=` — **본인 서비스 잡만** 보인다.
- 취소: `DELETE /v1/jobs/{job_id}` (queued 상태만).

> `/v1/jobs` **POST 는 두지 않는다**. `/v1/generate` 에 `wait: 0` 을 쓰면 되므로
> 엔드포인트를 늘릴 이유가 없다(개념 축소).

### 4.3 메타
- `GET /v1/roles` — 호출 서비스가 쓸 수 있는 역할·모델 목록
- `GET /v1/status` — 백엔드 온라인·보유 모델, **레인별 큐 길이**, 현재 로드된 모델, 최근 처리량
- `GET /healthz` — 인증 없이 200 (컨테이너 헬스체크용)

## 5. 큐 / 스케줄링 — 2레인 구조

리뷰 반영: 초안의 `MAX_CONCURRENCY=1` 은 **head-of-line blocking** 을 낳는다. 우선순위는
큐 순서만 바꿀 뿐 **실행 중인 잡을 비우지 못하므로**, 3분짜리 32B 배치가 돌면 2초짜리
대화형 요청도 3분을 기다린다. 레인을 나눠 해결한다.

```
interactive 레인 : 동시 1 — 작은 모델(≤14B)·짧은 timeout. 대화형/UI 요청
batch 레인       : 동시 1 — 큰 모델·긴 작업. 야간 분석 등
                   → 합계 최대 2개 동시 (48GB 에서 14B+14B, 7B+32B 등 공존 가능)
```

- 레인은 **역할 정의에 `lane: interactive|batch`** 로 지정(기본 `batch`).
- **메모리 상한 가드**: 두 레인의 모델 추정 크기 합이 `MEM_BUDGET_GB`(기본 40)를 넘으면
  작은 쪽 레인을 대기시킨다. 32B 가 도는 동안엔 interactive 를 7B 로만 허용하는 식.
  (모델별 추정 크기는 설정 테이블로 두고, 미상이면 보수적으로 큰 값 사용)
- **모델 친화 스케줄링** — 같은 레인에서 다음 잡을 고를 때 현재 로드된 모델과 같은 모델을
  우선. 단 대기 `MAX_STARVATION`(기본 5분) 초과 잡은 무조건 우선(기아 방지).
- **`keep_alive`** — Ollama 요청에 기본 `10m` 전달, 연속 요청 시 재로드 방지.
- **재시도** — 연결 실패·타임아웃은 지수 백오프(2s→4s→8s), 최대 `MAX_RETRIES`(3).
  맥 재부팅 중에도 잡이 살아남는다.
- **타임아웃** — 역할별 `timeout`. 초과 시 `failed(timeout)`.

## 6. 인증 / 멀티테넌시

`config/services.yaml` (커밋 대상, **토큰 값은 환경변수 참조만**):

```yaml
services:
  roxlogy:
    token_env: LLMGW_TOKEN_ROXLOGY
    allow_roles: ["analyze_workout", "coach_feedback", "summarize"]
    rate_limit_per_min: 30
    callback_allow: []            # 7.1 참고 — 기본 비허용
  tnm:
    token_env: LLMGW_TOKEN_TNM
    allow_roles: ["summarize", "classify_news"]
  hosub:
    token_env: LLMGW_TOKEN_HOSUB
    allow_roles: ["*"]
```

- 토큰 → 서비스 식별 → 사용량 귀속 + 역할 접근 제어. 비교는 `hmac.compare_digest`.
- 미허용 역할 요청은 403 `{"status":"forbidden","allowed":[...]}`.

### 6.1 콜백은 1차에서 제외 (보안)

초안의 `callback_url` 은 **SSRF 위험**이 있다. 토큰이 유출되면 게이트웨이를 통해 같은
호스트의 내부 서비스(trading:8600, mcp:8700 등)로 임의 POST 를 유발할 수 있다.

**결정: 1차 구현에서 콜백을 빼고 폴링만 지원**한다. 필요해지면 서비스별
`callback_allow` allowlist(위 스키마에 자리만 예약)로 **명시 허용된 URL 접두어**에만
전송하도록 추가한다.

## 7. 역할 = "모델 정책", 프롬프트는 호출자 소유

리뷰 반영: 프롬프트를 게이트웨이에 두면 roxlogy 개발자가 프롬프트를 고칠 때마다
**hosub-mcp 레포에 PR + 배포**해야 한다. 프롬프트는 자주 바뀌므로 마찰이 크다.

**역할이 정하는 것(운영 관심사, 게이트웨이 소유)**
- `model`, `timeout`, `options`, `lane`, (선택) `max_prompt_chars`

**프롬프트(도메인 관심사, 호출자 소유)**
- 요청의 `system` 이 있으면 그것을 쓴다 → roxlogy 는 자기 레포에서 자유롭게 반복 개선
- 없으면 역할의 `system` 기본값 사용 (hosub 운영 도구처럼 게이트웨이가 관리해도 되는 경우)

`config/roles.yaml`:
```yaml
backend:
  base_url: ${OLLAMA_URL}          # 예: http://192.168.0.x:11434
  keep_alive: 10m

roles:
  # --- hosub 운영 (프롬프트도 여기서 관리) ---
  summarize:    { model: qwen2.5:7b,  lane: interactive, timeout: 120, system: "...", options: {temperature: 0.3} }
  log_analyze:  { model: qwen2.5:14b, lane: interactive, timeout: 180, system: "..." }
  translate:    { model: qwen2.5:14b, lane: interactive, timeout: 180, system: "..." }
  code:         { model: qwen2.5-coder:14b, lane: interactive, timeout: 240, system: "..." }
  general:      { model: qwen2.5:32b, lane: batch,       timeout: 300, system: "..." }

  # --- roxlogy (모델 정책만. 프롬프트는 roxlogy 가 system 으로 전달) ---
  analyze_workout: { model: qwen2.5:14b, lane: batch, timeout: 240 }
  coach_feedback:  { model: qwen2.5:32b, lane: batch, timeout: 300 }

  # --- TNM ---
  classify_news:   { model: qwen2.5:7b,  lane: interactive, timeout: 60 }
```

모델 교체 = 이 파일 한 줄. **소비자 코드 무변경**이 이 설계의 핵심 가치다.

## 8. 데이터 저장

SQLite 한 파일. **잡은 재시작에도 살아남아야 한다**(배치 분석 유실 방지).

- **위치: `/data/llm-gateway/llmgw.db` 를 바인드 마운트** — 도커 기본 볼륨
  (`/var/lib/docker`, 루트 465GB)이 아니라 hosub 의 데이터 디스크(`/data`, 1.8TB)에 둔다.
  백업·용량 관리가 쉬워진다.
- WAL 모드. 쓰기는 게이트웨이 단일 프로세스이므로 경합 없음.

```sql
CREATE TABLE jobs (
  id TEXT PRIMARY KEY, service TEXT NOT NULL, role TEXT NOT NULL, model TEXT,
  lane TEXT NOT NULL,
  status TEXT NOT NULL,            -- queued|running|succeeded|failed|cancelled
  priority INTEGER DEFAULT 0,
  prompt TEXT NOT NULL, system TEXT, response TEXT, error TEXT,
  metadata_json TEXT,
  attempts INTEGER DEFAULT 0,
  created_at TEXT, started_at TEXT, finished_at TEXT
);
CREATE INDEX idx_jobs_queue ON jobs(lane, status, priority DESC, created_at);

CREATE TABLE usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, service TEXT, role TEXT,
  model TEXT, eval_count INTEGER, duration_ms INTEGER, status TEXT
);
```
기동 시 `running` 으로 남은 잡은 `queued` 로 되돌린다(크래시 복구). 완료 잡은
`JOB_RETENTION_DAYS`(기본 30) 후 정리.

### 8-1. 모델 설치 요청 (자동 요청 → 사람 승인 → 자동 설치)

외부 서비스가 늘어나면 **아직 맥에 없는 모델**을 쓰는 역할이 반드시 생긴다. 이때
"맥에 SSH 로 들어가 `ollama pull`" 을 사람이 하게 두면 게이트웨이의 의미가 반감된다.
반대로 무조건 자동 설치하면 수십 GB 가 예고 없이 내려받힌다. 그래서 **요청은 자동,
결정은 사람**으로 나눈다.

```sql
CREATE TABLE model_requests (
  model TEXT PRIMARY KEY,
  status TEXT NOT NULL,     -- pending|approved|pulling|ready|rejected|failed
  requested_by TEXT, roles_json TEXT, est_size_gb REAL,
  progress INTEGER DEFAULT 0, error TEXT,
  created_at TEXT NOT NULL, decided_at TEXT, finished_at TEXT
);
```

흐름:

1. 스케줄러가 주기적으로 `/api/tags` 로 보유 모델을 캐시한다(기본 30초).
2. 대기 잡 중 모델이 없는 것은 **레인에서 건너뛴다** — 미설치 모델 하나가 큐 전체를
   막으면 head-of-line 방지 설계가 무너지기 때문이다. 동시에 `model_requests` 에
   `pending` 행을 만든다(요청 서비스·역할·추정 크기 포함).
3. 사람이 대시보드 **LLM → 모델 설치 요청** 카드에서 승인/거부한다. MCP 도구
   (`llm_model_requests` / `llm_decide_model`)로도 같은 결정을 내릴 수 있다.
4. 승인되면 게이트웨이가 `POST /api/pull` 을 스트리밍으로 호출해 진행률을 기록한다.
   **맥에 SSH 접속이 필요 없다.**
5. `ready` 가 되면 다음 캐시 갱신에서 모델이 보이고, 대기하던 잡이 그대로 실행된다.

설계상 중요한 선택:

- **목록을 모를 때는 "없다"고 단정하지 않는다.** `/api/tags` 가 실패하면(맥 재부팅 등)
  캐시를 `None` 으로 두고 예전처럼 낙관적으로 실행한다. 백엔드가 잠깐 죽었다고 큐
  전체를 설치 대기로 돌리면 안 된다. 기동 시에는 레인을 열기 전에 목록을 한 번 읽어,
  기동 직후 잡이 404 로 죽는 창을 없앤다.
- **거부/실패는 무한 대기가 아니라 명확한 실패.** 그 모델을 기다리던 잡을 즉시 실패
  처리해 호출자가 이유를 안다. 거부한 모델을 자동으로 다시 물어보지 않는다(사용자의
  결정을 존중). 단 `ready` 였던 모델이 사라지면 다시 승인을 받는다.
- **승인 권한은 `services.yaml` 의 `admin: true` 인 서비스에만.** 현재 `hosub`
  (MCP·대시보드)뿐이다. 소비 서비스는 목록 조회만 가능하다.
- **임의 모델을 요청할 수 있는 경로가 아니다.** 설치 후보는 `roles.yaml` 의 역할이
  참조하는 모델뿐이고, 역할 추가는 여전히 Git PR 을 거친다.
- 끄고 싶으면 `AUTO_INSTALL_MODELS=0` — 예전 동작으로 완전히 되돌아간다.

> 조회 전용으로 설계한 대시보드에서 **유일하게 상태를 바꾸는 버튼**이다. 범위가
> 좁고(PR 리뷰를 통과한 역할의 모델 설치), 세션 로그인 뒤에 있으며, 감사 로그에
> 남는다는 전제로 둔 의도된 예외다.

## 9. 스트리밍 — 1차 제외 (명시적 결정)

`stream: false` 고정이라 긴 답변은 완료까지 화면이 비어 있다. 1차에서는 감내한다
(주 용도가 배치 분석·도구 호출이라 영향이 작음). 대화형 UI 를 키울 때 **SSE 로
`/v1/generate/stream`** 을 추가하는 것을 로드맵에 둔다. 잡 모델과 공존 가능하도록
스트리밍 경로도 잡을 생성하되 결과를 증분 전송하는 형태로 설계한다.

## 10. Docker 구성 / 네트워크

리뷰 반영: `127.0.0.1:8603` 만 게시하면 **다른 컨테이너에서 접근 불가**하다
(컨테이너의 localhost 는 자기 자신). roxlogy 워커도 도커로 돌릴 계획이므로 공용 네트워크가 필요하다.

```
llm-gateway/
├─ Dockerfile              # python:3.12-slim
├─ compose.yml
├─ requirements.txt        # starlette/fastapi, uvicorn, httpx, pyyaml
├─ config/{roles.yaml, services.yaml}
├─ app/{main.py, queue.py, store.py, roles.py, auth.py, ollama.py}
└─ .env.example
```

```yaml
# compose.yml (요지)
networks:
  llm-net: { name: llm-net }        # 소비자 컨테이너가 붙는 공용 네트워크

services:
  llm-gateway:
    build: .
    restart: unless-stopped
    networks: [llm-net]
    ports:
      - "127.0.0.1:8603:8603"       # 호스트 프로세스(hosub-mcp/dash)용
    env_file: .env
    volumes:
      - ./config:/app/config:ro
      - /data/llm-gateway:/data      # 잡 DB (8절)
    healthcheck:
      test: ["CMD", "python", "-c", "import httpx;httpx.get('http://127.0.0.1:8603/healthz').raise_for_status()"]
      interval: 30s
```

- **호스트 프로세스**(hosub-mcp, hosub-dash, trading, tnm) → `http://127.0.0.1:8603`
- **컨테이너 소비자**(roxlogy 워커 등) → `llm-net` 에 join 후 `http://llm-gateway:8603`
- 컨테이너 → 맥 스튜디오 LAN IP 아웃바운드는 호스트 라우팅을 타므로 그대로 동작
  (`host.docker.internal` 불필요)

`.env` (커밋 금지):
```
OLLAMA_URL=http://192.168.0.x:11434     # ← 맥 스튜디오 IP (DHCP 고정 할당 권장)
MEM_BUDGET_GB=40
MAX_RETRIES=3
LLMGW_TOKEN_ROXLOGY=...
LLMGW_TOKEN_TNM=...
LLMGW_TOKEN_HOSUB=...
```

## 11. 기존 hosub 코드 통합 (마이그레이션)

**완료** — 게이트웨이가 실서버에서 검증된 뒤 진행했다(계획대로 별도 PR).

| 대상 | 결과 |
|---|---|
| `src/llm.py` | **삭제.** 역할을 `src/gateway.py`(게이트웨이 클라이언트)가 대신한다 |
| `src/tools/llm.py` | 도구 인터페이스 유지 + `wait` 노출. `llm_job` 추가(pending 결과 수령) |
| 대시보드 `/api/llm/*` | 게이트웨이 프록시로 변경 + `/api/llm/jobs/{id}` 추가 |
| `config/llm_registry.yaml` | **삭제.** 역할 정의는 게이트웨이 `config/roles.yaml` 단일 소스 |
| `AppContext.llm` | **제거.** LLM 은 HTTP 로만 접근하므로 컨텍스트에 들어갈 이유가 없다 |
| 대시보드 LLM 페이지 | 레인별 큐·실행 중 모델·메모리 예산·서비스별 사용량 표시. 생성이
  `pending` 이면 폴링해서 결과를 받는다(32b 모델도 화면에서 쓸 수 있게) |

### 11.1 새 단일 장애점 — 감수 범위 명시

게이트웨이 경유로 바꾸면 **게이트웨이가 죽으면 hosub 의 `llm_generate` 도 실패**한다
(전에는 Ollama 직접 호출이라 무관했다). 의도된 트레이드오프이며 `restart: unless-stopped`
\+ 헬스체크로 자동 복구한다.

**`LLMGW_FALLBACK_DIRECT` 는 구현하지 않았다** (설계 대비 변경). 이유:

- 폴백은 방금 지운 Ollama 직접 호출 코드를 그대로 되살려 둬야 한다. "설정의 단일 소스"가
  이 게이트웨이의 존재 이유인데, 기본값 off 인 두 번째 경로를 남기면 그 이유가 약해진다
- 기본값 off 인 경로는 아무도 실행하지 않으므로, 정작 필요한 장애 순간에 동작을 보장할 수 없다
- **탈출구는 이미 있다.** MCP 에 `run_command` 가 있어 게이트웨이가 죽어도
  `curl http://100.69.201.28:11434/api/generate` 를 대화로 바로 실행할 수 있다.
  전용 플래그로 중복 구현할 값이 없다

게이트웨이 자동 복구가 실제로 못 미더운 것으로 드러나면 그때 다시 판단한다.

## 12. 배포·운영

- **systemd 래퍼 + 레지스트리 등록 둘 다** 한다(미결 3 확정 — 15절).
  `deploy/llm-gateway.service` 로 단독 재기동·로그 조회가 되고,
  `config/registry.yaml` 의 `llm-gateway` 항목으로 대화 배포(`git pull` +
  `docker compose up -d --build`)가 된다.
- **다른 서비스 배포가 게이트웨이를 재시작하지 않는다.** 잡 큐를 들고 있어
  끌려 재시작되면 실행 중이던 추론이 끊기고 모델 다운로드가 중단된다.
  경로별 영향 표는 `docs/SETUP.md` 8-1절.
- 대시보드에서 상태 확인(레인별 큐·백엔드·사용량).
- 맥 스튜디오 준비물: `llm-gateway/docs/mac-setup.md`.

## 13. 테스트 전략

- **단위**: 역할 레지스트리 검증, 토큰 인증(허용/거부/역할 제한/레이트리밋),
  스케줄러(레인별 동시성, 메모리 예산 가드, 모델 친화 선택, 기아 방지, 우선순위,
  재시도·백오프), 잡 저장/크래시 복구.
- **통합**: 가짜 Ollama stub 으로 전체 흐름(`wait` 만료 → pending → 폴링 → 완료),
  백엔드 다운 시 재시도, 타임아웃, 재시작 후 큐 이어받기.
- **회귀 테스트(중요)**:
  - *Head-of-line*: batch 레인에 장시간 잡을 넣은 상태에서 interactive 요청이
    **막히지 않고** 완료되는지.
  - *모델 전환 최소화*: 서로 다른 모델 잡 10개 투입 시 실제 모델 전환 횟수가
    잡 수보다 현저히 적은지.
  - *미설치 모델이 레인을 막지 않음*: 미설치 모델 잡을 먼저 넣어도 뒤에 들어온
    정상 잡이 먼저 완료되는지(8-1절).
  - *승인 후 자동 재개*: 승인 → pull → 대기하던 잡이 이어서 성공하는지. 거부/설치
    실패 시 무한 대기가 아니라 명확한 오류로 끝나는지.
- 실제 맥 없이 전부 통과해야 한다.

## 14. 완료 기준

1. 컨테이너 기동 후 `/healthz` 200, `/v1/status` 에 백엔드·레인별 큐 표시
2. 토큰 없이 401, 미허용 역할 403
3. `/v1/generate` 가 **항상 같은 응답 형태**(job_id 포함)로 ok/pending 을 반환하고,
   pending 은 폴링으로 완료 확인
4. batch 장시간 잡 실행 중에도 interactive 요청이 정상 처리(레인 분리 검증)
5. 동시 실행이 레인 정의·메모리 예산을 넘지 않음
6. 컨테이너 재시작 후 큐에 남은 잡이 이어서 처리됨
7. 컨테이너 소비자가 `llm-net` 으로, 호스트 프로세스가 `127.0.0.1:8603` 으로 각각 접근 성공
8. hosub MCP `llm_generate` / 대시보드 LLM 페이지가 게이트웨이 경유로 정상 동작
9. 미설치 모델 요청 시 레인이 막히지 않고 `pending` 요청이 생기며, 대시보드에서
   승인하면 자동 설치 후 대기 잡이 이어서 완료됨 (8-1절)
10. `pytest` 전체 통과

## 15. 미결 사항 (구현 착수 전 확정 필요)

- [x] ~~**맥 스튜디오 LAN IP**~~ → **확정: Tailscale IP `100.69.201.28`** 을 쓴다.
      맥 스튜디오에도 Tailscale 을 설치해 `macstudio` 노드로 붙었다. LAN IP
      (192.168.0.31)는 DHCP 라 바뀔 수 있어 tailnet 주소가 낫고, 같은 LAN 이라
      직접 연결(9ms)이므로 손해가 없다. `config/llm_registry.yaml` 의 잘못된
      맥북 IP(100.107.151.46)도 함께 정정했다.
      맥 쪽 준비 절차는 `llm-gateway/docs/mac-setup.md`.
      hosub 에서 확인한 것: tailscale ping 성공, 도커 컨테이너 → tailnet 도달 가능,
      11434 는 아직 `Connection refused`(= Ollama 가 없거나 127.0.0.1 바인딩)
- [ ] roxlogy 역할의 모델 선택(`analyze_workout` 등) — 데이터 규모·응답 품질 요구 확인
      (프롬프트는 7절에 따라 roxlogy 소유이므로 게이트웨이엔 모델 정책만 정의)
- [x] ~~Docker 서비스 등록 방식: `docker compose` 직접 vs systemd 래퍼~~ →
      **확정: systemd 래퍼**(`deploy/llm-gateway.service`). 게이트웨이는 잡 큐를
      들고 있어 **다른 서비스 배포와 수명주기가 분리돼야 한다**. 유닛으로 두면
      `systemctl restart/reload llm-gateway` 로 단독 제어되고, MCP
      `restart_service`/`read_service_logs`/`deploy_service` 대상이 되며,
      부팅 순서를 `docker.service` 뒤로 명시할 수 있다.
      `ExecStop` 은 `compose down` 이 아니라 `stop` — `llm-net` 을 지우면 거기
      붙은 다른 소비 컨테이너가 끊기기 때문이다.
      `dash`/`tnm` 배포가 같은 클론에서 `git pull` 하므로 게이트웨이 코드가
      디스크에만 내려오는 드리프트가 생기는데, `update.sh` 가 이를 감지해
      **행동하지 않고 로그로만 알린다**(분리 유지 + 침묵 방지). 상세는
      `docs/SETUP.md` 8-1·8-2절.
- [x] ~~소비 서비스가 hosub **밖 다른 호스트**에서도 호출할 계획인지~~ → **확정: 그렇다.**
      roxlogy 는 Vercel 에서 서비스된다. 15-1절 참고

### 15-1. 집 밖 소비자 (확정)

roxlogy 가 Vercel 에 있어 "같은 호스트/내부망" 전제가 깨진다. 세 가지가 문제였다.

1. 게이트웨이는 `127.0.0.1:8603` 에만 게시 — 집 NAT 안이라 밖에서 접근 불가
2. Vercel 서버리스는 **고정 출발 IP가 없다**(Enterprise Secure Compute 제외) →
   hosub 쪽에서 IP 허용목록을 만들 수 없다
3. 서버리스 함수는 플랜에 따라 최대 60~300초 — LLM 을 기다리며 태울 시간이 아니다

**결정: 이미 공개 중인 Caddy(`hosub.duckdns.org`)에 `/llm/v1/*` 경로를 추가한다.**
소비자는 `wait=0` 으로 던지고 브라우저가 폴링한다.

- **`/v1/*` 만 연다.** `/healthz` 는 공개할 이유가 없다
- **CORS 를 열지 않는다.** 브라우저가 게이트웨이를 직접 부르면 토큰이 노출된다 —
  소비자는 자기 서버 사이드 라우트에서만 호출해야 한다
- **토큰이 유일한 통제**이므로 `rate_limit_per_min` 을 보수적으로(roxlogy 30) 잡아
  토큰이 새도 소비량 상한이 걸리게 한다
- **위험 범위는 "맥 GPU 소모"까지다.** MCP 토큰(=root)과 달리 서버가 장악되지 않는다.
  이 차이가 공개를 수용 가능하게 만든다
- 인증 실패를 클라이언트 IP 와 함께 로그에 남기고(`X-Forwarded-For`), 컨테이너 로그를
  10MB×3 으로 회전시킨다 — 공개 엔드포인트는 스캐너를 부르고, 도커 기본 무제한 로그는
  루트 디스크(465GB)를 잠식한다

**검토했으나 택하지 않은 대안**: hosub 가 roxlogy 를 폴링하는 역방향 구조. 집 서버가
인바운드를 전혀 안 받고 정전·재부팅에도 큐가 나중에 소화된다는 장점이 있으나, 양쪽에
코드가 더 붙고 roxlogy 에 인증된 claim/complete 엔드포인트가 필요하다. hosub 는 이미
MCP·대시보드로 공개돼 있어 노출이 새로운 범주의 위험이 아니라고 판단했다. 인바운드를
완전히 닫아야 할 이유가 생기면 이 대안으로 전환할 수 있다 — 게이트웨이 설계는 그대로고
hosub 워커가 또 하나의 소비자가 될 뿐이다.

소비자 쪽 지침(코드 예시 포함)은 `llm-gateway/docs/integration.md` 5절.

## 16. 참고

- 기존 LLM 게이트웨이 구현: `src/llm.py`, `src/tools/llm.py`, `static/pages/llm.js` (PR #102)
- 유사 서비스 패턴: `trading`(8600), `tnm`(8602) — INTERNAL_TOKEN + 대시보드 프록시
- 소비 예정: [roxlogy](https://github.com/teenfo/roxlogy) — HYROX 훈련 분석,
  hosub 를 백엔드 워커로 사용
