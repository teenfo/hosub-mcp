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
| `GET /v1/jobs/{id}` | 잡 조회 (본인 서비스 것만) |
| `GET /v1/jobs?status=&limit=` | 잡 목록 |
| `DELETE /v1/jobs/{id}` | 취소 (대기 중인 것만) |
| `GET /v1/roles` | 쓸 수 있는 역할·모델 |
| `GET /v1/status` | 백엔드·레인 큐·사용량 |
| `GET /healthz` | 헬스체크 (인증 불필요) |

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
cp .env.example .env      # OLLAMA_URL(맥 LAN IP) + 토큰들 채우기
docker compose up -d --build
curl -s localhost:8603/healthz
```

맥 스튜디오 준비물:
```bash
launchctl setenv OLLAMA_HOST 0.0.0.0     # 외부 접속 허용 후 Ollama 재시작
ollama pull qwen2.5:7b qwen2.5:14b qwen2.5-coder:14b qwen2.5:32b
```

## 개발·테스트

```bash
../.venv/bin/python -m pytest        # 실제 맥 없이 전부 통과 (가짜 백엔드 주입)
```

주요 회귀 테스트: head-of-line blocking 방지, 모델 전환 최소화, 재시도/백오프,
재시작 후 잡 이어받기.
