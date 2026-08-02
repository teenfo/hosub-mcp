# 파일 지도 — 어디를 고쳐야 하나

`llm-gateway/` 는 자체 완결이다. 아래는 "무엇을 바꾸려면 어디를 여는가" 다.

## `app/` — 서비스 본체

| 파일 | 책임 | 여는 때 |
|---|---|---|
| `main.py` (1,400줄) | HTTP 라우트 전부 + `build_app()` 조립 | 엔드포인트 추가·수정 |
| `meta.py` (700줄) | `/v1/meta`·OpenAPI 생성 | 라우트를 늘렸을 때(필수) |
| `store.py` (660줄) | SQLite — 잡·사용량·오버라이드·모델요청·A/B·감사 | 새 데이터를 저장할 때 |
| `config.py` (550줄) | `roles.yaml`·`services.yaml` 로드, 역할 오버라이드 병합 | 역할 필드·서비스 속성 |
| `scheduler.py` (500줄) | 2레인 잡 큐, 재시도, 모델 자동설치, 보존 정리 | 큐·재시도·설치 동작 |
| `ollama.py` (287줄) | 맥 Ollama HTTP (generate·embed·tags·pull·delete) | 백엔드 호출 |
| `notify.py` | Slack 알림(웹훅 없으면 no-op) | 알림 조건 |
| `catalog.py` | `catalog.yaml` 로드 + mtime 캐시 | 카탈로그 형식 |
| `auth.py` (49줄) | Bearer 인증 + 슬라이딩 레이트리밋 | 인증 경계 |

### `main.py` 안에서 아는 것이 값진 곳

- `_job_response()` — **모든 잡 응답의 단일 형태.** 여기 키를 늘리면 목 서버
  `shape()` 도 같이 늘려야 한다(카나리아가 잡는다). 키를 빼면 소비자가 깨진다.
- `_auth()` / `_require_admin()` — 인증·관리 게이트. 새 라우트는 반드시 둘 중
  하나를 지난다(공개로 둘 거면 `meta.PUBLIC_ROUTES` 에 등재하고 이유를 적는다).
- `_limits()` — 소비자가 큐를 예측하는 데 쓰는 값. **env 를 다시 읽지 않고
  살아 있는 스케줄러에서 읽는다** — 재파싱하면 주입된 값과 갈라진다.
- `_servers()` — 스펙의 `servers[]`. `LLMGW_PUBLIC_URL` → `X-Forwarded-Prefix`
  → `base_url` 순. **`/llm` 이 빠지면 생성된 클라이언트가 404 를 맞는다.**
- `_attachment()` — `?download=1` 일 때만 `Content-Disposition`.
- `CLIENT_SOURCES` / `_file_meta()` — 클라이언트 원본 서빙 + 동적 sha256.
- 라우트 표는 파일 맨 아래 `routes = [...]`. 관리 계열은 주석으로 구분돼 있다.

## `config/` — 바인드 마운트 (재빌드 불필요)

| 파일 | 내용 | 주의 |
|---|---|---|
| `roles.yaml` | 역할→모델·레인·타임아웃·시스템프롬프트, `mem_budget_gb`, `backend` | 역할 삭제·개명 금지. `${ENV}` 보간 지원 |
| `services.yaml` | 소비자→`token_env`·`allow_roles`·`rate_limit_per_min`·`admin` | **값이 아니라 env 이름만.** 변경은 PR 게이트 |
| `catalog.yaml` | 설치 가능 모델 목록 + 추정 크기 | `git pull` 로 즉시 반영 |

토큰 **값**은 `llm-gateway/.env`(gitignore)에만 있다. 비면 그 서비스는 조용히
비활성된다 — `/v1/admin/services` 가 그걸 드러낸다.

## 그 밖

- `client/llmgw.py` — 소비자가 **실제로 쓰는 인터페이스.** 사본 둘과 바이트 동일
- `tools/mock_gateway.py` — 게이트웨이 없이 개발용. `app.*` 임포트 금지
- `static/` — `docs.html`(탐색기) + 벤더링한 swagger-ui
- `docs/integration.md` — 소비자용 가이드. `/v1/integration` 으로 서빙된다
- `Dockerfile` — `app/`·`docs/`·`client/`·`tools/`·`static/` 을 COPY

## 런타임 역할 오버라이드 — 헷갈리기 쉬운 부분

`roles.yaml` 이 **기본값**이고 DB 의 오버라이드가 그 위에 얹힌다.

- `RoleConfig._base`(YAML) + `_overrides`(DB) → `_roles`(유효). **읽기는 전부
  `_roles` 를 지난다.** `apply_overrides()` 는 매번 전체를 다시 만들어 원자적으로
  갈아 끼운다(반쯤 만들어진 맵을 다른 코루틴이 보지 않게).
- 덮어쓸 수 있는 것은 `model`·`lane`·`timeout`·`options`·`max_prompt_chars` 뿐.
  **`kind` 와 `system` 은 제외** — `kind` 를 바꾸면 그 역할이 잡 큐와 메모리
  예산을 우회하는 동기 경로로 넘어가고, `system` 은 "프롬프트는 호출자 소유"
  원칙과 충돌한다.
- 잡은 생성 시점의 `model`·`options`·`timeout` 을 **스냅샷**한다. 그래서 모델을
  바꿔도 큐에 있던 잡은 옛 모델로 돈다 — 정상이다.
- 쓰기 직후 반영은 `_reload_overrides()` 한 경로로만. 다른 길을 만들지 않는다.
