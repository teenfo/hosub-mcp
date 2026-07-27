# llm-gateway — 공유 LLM 게이트웨이

외부 서비스(roxlogy·TNM·trading·hosub MCP/대시보드)가 **맥 스튜디오의 Ollama** 를
함께 쓰기 위한 단일 게이트웨이. 설계 근거는
[`docs/requests/llm-gateway-service.md`](../docs/requests/llm-gateway-service.md).

## 왜 하나를 공유하나

- **설정 단일 소스** — 모델 교체·백엔드 주소 변경이 `config/roles.yaml` 한 줄. 소비자 코드 무변경
- **잡 영속화·재시도** — 맥이 재부팅돼도 배치 분석이 살아남는다
- **인증·사용량 귀속·역할 제한** — 어느 서비스가 무엇을 얼마나 썼는지
- **레인 분리** — 긴 배치가 짧은 대화형 요청을 막지 않는다

> **소비 서비스는 워커가 필요 없다.** LLM을 쓰려고 컨테이너를 추가할 일은 없고,
> HTTP 호출만 하면 된다. 게이트웨이의 잡 큐가 공용 워커 역할을 한다.

## 소비자 사용법

> 다른 레포에서 붙인다면 **[`docs/integration.md`](docs/integration.md)** 를 보세요.
> 복사해 쓰는 한 파일 클라이언트(`client/llmgw.py`)와, 게이트웨이 없이 개발할 수 있는
> 목 서버(`tools/mock_gateway.py`)가 있습니다.
>
> 저장소 접근이 없다면 게이트웨이가 같은 문서를 서빙한다:
> `curl -H "Authorization: Bearer $LLMGW_TOKEN" $LLMGW_URL/v1/integration`

```python
import httpx
GW, TOKEN = "http://127.0.0.1:8603", os.environ["LLMGW_TOKEN_BCL"]
H = {"Authorization": f"Bearer {TOKEN}"}

# 짧은 작업 — 결과를 기다린다
r = httpx.post(f"{GW}/v1/generate", headers=H, json={
    "role": "summarize", "prompt": text, "wait": 30}).json()
if r["status"] == "ok":
    print(r["response"])

# 긴 작업 — 즉시 job_id 받고 나중에 폴링 (자체 큐 불필요)
job = httpx.post(f"{GW}/v1/generate", headers=H, json={
    "role": "analyze_workout", "prompt": data,
    "system": "너는 하이록스 코치다 ...",     # 프롬프트는 호출자 소유
    "wait": 0, "metadata": {"session_id": 42}}).json()
later = httpx.get(f"{GW}/v1/jobs/{job['job_id']}", headers=H).json()
```

**응답 형태는 항상 같다** — `status` 가 `ok` | `pending` | `failed` 이고 `job_id` 는 언제나
포함된다. 동기/비동기를 구분해 분기할 필요가 없다.

## API

| 엔드포인트 | 설명 |
|---|---|
| `POST /v1/generate` | 생성. `wait` 초까지 기다림(0이면 즉시 pending) |
| `POST /v1/embed` | 임베딩 벡터(bge-m3). 큐를 타지 않고 동기 응답 |
| `GET /v1/jobs/{id}` | 잡 조회 (본인 서비스 것만) |
| `GET /v1/jobs?status=&limit=` | 잡 목록 |
| `DELETE /v1/jobs/{id}` | 취소 (대기 중인 것만) |
| `GET /v1/roles` | 쓸 수 있는 역할·모델 |
| `GET /v1/status` | 백엔드·레인 큐·사용량 |
| `GET /v1/integration` | 소비자용 통합 가이드(마크다운) — 레포 접근 없이 최신 계약 |
| `GET /v1/models/requests` | 모델 설치 요청 목록 |
| `POST /v1/models/requests` | 승인/거부 — `{"model":…, "action":"approve"\|"reject"}` (admin 서비스만) |
| `GET /healthz` | 헬스체크 (인증 불필요) |

**관리 전용** — `127.0.0.1:8603` 으로만 닿는다. Caddy 가 공개 경로에서 404 로 잘라내고,
앱 안에서도 `admin: true` 서비스만 통과시킨다(두 겹).

| 엔드포인트 | 설명 |
|---|---|
| `GET /v1/admin/roles` | 역할별 유효값 + 기본값 대비 차이 + roles.yaml 스니펫 |
| `POST /v1/admin/roles` | 역할 오버라이드 저장 — `{"role":…, "fields":{"model":…}}`. 없는 이름이면 신규 역할 |
| `DELETE /v1/admin/roles?role=` | 오버라이드 해제(기본값 복귀). 신규 역할이면 삭제 |
| `GET /v1/admin/audit` | 관리 작업 감사 로그 |

## 역할 모델을 재배포 없이 바꾼다

`roles.yaml` 은 **기본값**이고, 대시보드에서 건 오버라이드가 그 위에 얹힌다.
모델 교체에 PR → 머지 → 배포가 필요 없다.

지킨 것:

- **잡은 자기완결형이다.** 모델뿐 아니라 `options`·`timeout` 도 생성 시점에 못박는다.
  큐에 있던 잡이 "옛 모델 + 새 옵션" 으로 도는 일이 없다
- **`kind`·`system` 은 못 바꾼다.** `kind` 를 embed 로 바꾸면 그 역할이 큐와 메모리
  예산을 우회하는 동기 경로로 넘어가고, `system` 은 "프롬프트는 호출자 소유" 와 충돌한다
- **잘못된 오버라이드 행이 기동을 막지 않는다.** 건너뛰고 `/v1/status.overrides.invalid` 로 알린다
- **드리프트가 보인다.** `/v1/status.overrides.count` 가 0보다 크면 프로덕션이 레포와
  다르다는 뜻이다. `GET /v1/admin/roles` 의 `yaml_snippet` 을 `roles.yaml` 에 반영하면 0으로 돌아온다

> **롤백 레버.** 오버라이드는 코드가 아니라 데이터다. 이상하면
> `sqlite3 /data/llm-gateway/llmgw.db "DELETE FROM role_overrides;"` 후
> `sudo systemctl restart llm-gateway` — 순수 `roles.yaml` 동작으로 즉시 복귀한다.
>
> ⚠️ 뒤집어 말하면 **DB 를 백업본으로 되돌리면 모델 선택도 조용히 되돌아간다.**

## 새 소비자 추가 (예: BCL)

1. `config/services.yaml` 에 블록 추가
   ```yaml
   bcl:
     token_env: LLMGW_TOKEN_BCL
     allow_roles: ["summarize", "translate", "general"]
   ```
2. `.env` 에 `LLMGW_TOKEN_BCL=$(openssl rand -hex 32)` — 같은 값을 소비자에도 설정
3. 소비자 코드에서 HTTP 호출 (위 예시)

컨테이너·워커 추가 없음.

## 새 모델은 승인 한 번으로 설치된다

새 소비자가 아직 맥에 없는 모델을 쓰는 역할을 호출하면:

1. 게이트웨이가 그 잡을 **레인에서 건너뛰고**(다른 잡은 계속 돈다) 설치 요청을 만든다
2. 대시보드 **LLM → 모델 설치 요청** 카드에 "승인 대기"로 뜬다
   (Claude 에게 물어봐도 된다 — MCP 도구 `llm_model_requests` / `llm_decide_model`)
3. 승인하면 게이트웨이가 맥의 `/api/pull` 로 직접 내려받는다 — **맥에 SSH 접속 불필요**
4. 설치가 끝나면 대기하던 잡이 자동으로 이어서 실행된다

거부하거나 설치가 실패하면 그 모델을 기다리던 잡은 무한 대기 대신 명확한 오류로
끝난다. 거부한 모델은 다시 물어보지 않는다(다시 승인하면 그때 설치).

> 승인해도 설치되는 것은 `config/roles.yaml` 에 있는 역할이 참조하는 모델뿐이다.
> 역할 추가는 여전히 Git PR 을 거친다 — 임의 모델을 요청할 수 있는 경로가 아니다.

끄려면 `.env` 에 `AUTO_INSTALL_MODELS=0`.

## 역할 = 모델 정책, 프롬프트 = 호출자 소유

`config/roles.yaml` 의 역할은 **모델·레인·타임아웃·옵션**만 정한다. 프롬프트는 요청의
`system` 이 우선하고, 없을 때만 역할 기본값을 쓴다. 덕분에 roxlogy 같은 소비자가
자기 레포에서 프롬프트를 자유롭게 개선할 수 있다(게이트웨이에 PR 불필요).

## 레인과 메모리 예산

```
interactive : 동시 1 — 작은 모델·짧은 작업 (대화형)
batch       : 동시 1 — 큰 모델·긴 작업 (야간 분석 등)
```
두 레인의 실행 중 모델 크기 합이 `MEM_BUDGET_GB`(기본 40)를 넘으면 시작을 미룬다.
같은 레인에서는 **현재 로드된 모델과 같은 모델의 잡을 우선** 처리해 모델 전환을 줄이되,
5분 이상 기다린 잡은 무조건 우선한다(기아 방지).

## 실행

```bash
cp .env.example .env      # OLLAMA_URL(맥 Tailscale IP) + 토큰들 채우기
docker compose up -d --build
curl -s localhost:8603/healthz
```

운영 서버에서는 systemd 로 감싼다 — **다른 서비스 배포에 끌려 재시작되지 않도록**
수명주기를 분리하기 위해서다(잡 큐를 들고 있다).

```bash
sudo cp ../deploy/llm-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now llm-gateway

sudo systemctl restart llm-gateway   # 단독 재기동
sudo systemctl reload  llm-gateway   # 코드 반영 재빌드
```

`hosub-mcp` 자동 업데이트 타이머는 게이트웨이를 건드리지 않는다. 대신
`llm-gateway/` 코드가 바뀌면 로그로 알려준다 → `docs/SETUP.md` 8-1절.

맥 스튜디오 준비물은 **[`docs/mac-setup.md`](docs/mac-setup.md)** 에 정리해 두었다.
요점만:

```bash
launchctl setenv OLLAMA_HOST 0.0.0.0     # 외부 접속 허용 후 Ollama 재시작
sudo pmset -a sleep 0 disksleep 0        # 맥이 자면 백엔드가 사라진다
```
`launchctl setenv` 는 재부팅하면 사라지므로 LaunchAgent 로 고정해야 하고,
Tailscale 노드의 키 만료(기본 180일)도 꺼야 한다. 둘 다 문서에 스크립트가 있다.

모델은 미리 받을 필요 없다 — 첫 호출 때 게이트웨이가 설치 요청을 올리고,
대시보드에서 승인하면 자동으로 내려받는다.

## Slack 알림 (선택)

게이트웨이에는 **사람이 모르면 조용히 멈추는 지점**이 둘 있다. 그 순간에만 알린다.

| 알림 | 왜 필요한가 |
|---|---|
| 모델 설치 승인 대기 | 승인 전까지 그 모델을 쓰는 잡이 멈춰 있다. 대시보드를 안 열면 모른다 |
| 맥 백엔드 오프라인/복구 | 맥이 잠들거나 정전으로 꺼지면 야간 배치가 조용히 실패한다 |
| 모델 설치 완료/실패 | 21GB 다운로드가 끝났는지, 실패했는지 |

```bash
# .env
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

비워두면 꺼진다(오류 아님). 설계상 지킨 것:

- **알림 실패가 파이프라인을 죽이지 않는다.** 예외를 삼키고 로그만 남긴다
- **상태 전이에서만 보낸다.** 맥이 꺼져 있는 30초마다 알리면 아무도 안 본다
- **기동 시 "복구됨"을 보내지 않는다.** 재시작마다 시끄러워진다
- **비밀·프롬프트·응답 본문은 담지 않는다**

## 개발·테스트

```bash
../.venv/bin/python -m pytest        # 실제 맥 없이 전부 통과 (가짜 백엔드 주입)
```

주요 회귀 테스트: head-of-line blocking 방지, 모델 전환 최소화, 재시도/백오프,
재시작 후 잡 이어받기, 미설치 모델이 레인을 막지 않는지, 승인 후 잡 자동 재개.
