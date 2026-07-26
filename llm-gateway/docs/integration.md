# 소비 프로젝트 통합 가이드

roxlogy·BCL·TNM·trading 등 **다른 레포**에서 hosub 의 공유 LLM 게이트웨이를 쓰는 법.

> **워커도, 컨테이너도 필요 없다.** HTTP 호출만 하면 되고, 게이트웨이의 잡 큐가
> 공용 워커 역할을 한다. 게이트웨이가 아직 없어도 개발을 시작할 수 있다(3절).

---

## 1. 5분 만에 시작

```bash
# 1) 클라이언트 한 파일을 자기 레포에 복사
cp llm-gateway/client/llmgw.py <내_프로젝트>/lib/

# 2) 게이트웨이 없이 개발 시작 — 목 서버를 띄운다
python llm-gateway/tools/mock_gateway.py

# 3) 호출
export LLMGW_URL=http://127.0.0.1:8603 LLMGW_TOKEN=dev
```

```python
from llmgw import LLMGateway

gw = LLMGateway()                     # 환경변수에서 URL·토큰을 읽는다
print(gw.run("summarize", 긴_문서))    # 끝까지 기다려 텍스트만
```

실서버로 옮길 때 바뀌는 것은 **`LLMGW_URL` 과 `LLMGW_TOKEN` 두 값뿐**이다.
응답 계약이 같다는 것은 회귀 테스트(`tests/test_client_contract.py`)가 보장한다.

---

## 2. 클라이언트 (`client/llmgw.py`)

의존성은 `httpx` 하나. 동기(`LLMGateway`)와 비동기(`AsyncLLMGateway`) 둘 다 있고
API 는 동일하다. FastAPI 같은 async 앱은 뒤엣것을 쓴다.

```python
gw = LLMGateway()                            # 또는 LLMGateway(token=..., base_url=...)

# (a) 짧은 작업 — 결과를 기다린다
text = gw.run("summarize", 문서)

# (b) 긴 작업 — 즉시 job_id 를 받고 나중에 수령 (자체 큐 불필요)
job = gw.generate("analyze_workout", 데이터,
                  system="너는 하이록스 코치다 ...",   # 프롬프트는 호출자 소유
                  wait=0,
                  metadata={"session_id": 42})       # 결과와 함께 되돌아온다
...
done = gw.wait_for(job.job_id)               # 완료까지 폴링
print(done.response)

# (c) 조회·관리
gw.roles()            # 이 토큰으로 쓸 수 있는 역할·모델·레인·타임아웃
gw.status()           # 백엔드 상태·레인 큐·사용량
gw.jobs(status="queued")
gw.cancel(job.job_id) # 대기 중인 잡만 취소된다
```

### 예외를 왜 나눴나

재시도해도 소용없는 것(설정·코드 문제)과 잠깐 뒤 다시 하면 되는 것을 구분하려고.

| 예외 | 언제 | 대응 |
|---|---|---|
| `AuthError` | 401 토큰 없음/틀림, 403 그 역할 권한 없음 | **재시도 무의미.** `.env` 나 `services.yaml` 확인 |
| `RoleError` | 404 모르는 역할, 413 프롬프트 초과 | **재시도 무의미.** 코드 수정 |
| `JobFailed` | 잡이 failed/cancelled 로 종료 | `.job.error` 확인. 모델 미설치·거부도 여기 |
| `JobTimeout` | 정한 시간 내 미완료 | 잡은 계속 돌 수 있다. `.job.job_id` 로 나중에 다시 조회 |
| `GatewayError` | 연결 실패, 429, 5xx | **재시도 가치 있음.** 백오프 후 다시 |

```python
try:
    text = gw.run("coach_feedback", 데이터)
except JobFailed as e:
    log.warning("LLM 분석 실패: %s", e.job.error)   # 서비스는 계속 돌아야 한다
except (AuthError, RoleError):
    raise                                          # 배포 설정 문제 — 크게 터뜨린다
```

---

## 3. 게이트웨이 없이 개발하기 (`tools/mock_gateway.py`)

맥도, 도커도, 토큰도 없이 통합 코드를 완성할 수 있다.

```bash
python tools/mock_gateway.py                      # 기본: 8603, 지연 1.5초, 아무 토큰
python tools/mock_gateway.py --delay 20           # pending → 폴링 경로 시험
python tools/mock_gateway.py --fail-rate 0.5      # 에러 처리 시험
python tools/mock_gateway.py --deny code          # 403 경로 시험
python tools/mock_gateway.py --token dev          # 이 토큰만 허용
```

역할 목록은 실제 `config/roles.yaml` 에서 읽으므로 **역할 이름이 어긋나지 않는다**.
응답 형태도 실서버와 동일하다(회귀 테스트로 고정).

> ⚠️ 인증이 사실상 없고 응답은 가짜다. **서버에 띄우지 말 것.**

---

## 4. 실서버 연결

hosub 관리자에게(=본인) 요청할 것:

1. `llm-gateway/config/services.yaml` 에 블록 추가 — **PR 리뷰 대상**
   ```yaml
   bcl:
     token_env: LLMGW_TOKEN_BCL
     allow_roles: ["summarize", "translate", "general"]
     rate_limit_per_min: 60
   ```
2. `llm-gateway/.env` 에 `LLMGW_TOKEN_BCL=$(openssl rand -hex 32)`
3. 같은 값을 소비 프로젝트의 `LLMGW_TOKEN` 에 설정

접속 주소는 소비자가 어디서 도느냐에 따라 다르다.

| 소비자 위치 | `LLMGW_URL` |
|---|---|
| hosub 의 다른 컨테이너 | `http://llm-gateway:8603` (`llm-net` 네트워크에 붙인다) |
| hosub 의 호스트 프로세스 | `http://127.0.0.1:8603` |
| 다른 머신 | 현재 설계 범위 밖 — 8603 은 루프백에만 게시된다 |

---

## 5. 필요한 역할·모델이 없을 때

**역할 추가**는 `config/roles.yaml` 에 PR 을 올린다(모델·레인·타임아웃만 정한다).
**프롬프트는 게이트웨이에 올리지 않는다** — 요청의 `system` 으로 보내면 되고, 그래야
자기 레포에서 자유롭게 개선할 수 있다.

**모델이 아직 맥에 없으면** 아무것도 안 해도 된다. 첫 호출 때 게이트웨이가 설치
요청을 만들고, hosub 대시보드에서 승인하면 자동으로 설치된 뒤 대기하던 잡이 이어서
실행된다. 그동안 그 잡은 `pending` 이고, **다른 잡은 막히지 않는다.**

승인 전까지 잡이 오래 `pending` 이면 이유를 확인할 수 있다:

```python
gw.model_requests()   # [{"model": "qwen3:32b", "status": "pending", ...}]
```

거부되거나 설치가 실패하면 그 잡은 무한 대기 대신 `JobFailed` 로 끝난다.

---

## 6. 응답 계약 (직접 HTTP 를 부를 때)

클라이언트를 안 쓰고 직접 호출해도 된다. **모든 요청은 잡**이고, 어느 엔드포인트든
응답 모양이 같다.

```jsonc
{
  "job_id": "a1b2c3d4e5f6",     // 항상 있다 — pending 이면 이걸로 폴링
  "status": "ok",               // ok | pending | failed | cancelled
  "response": "...",            // ok 일 때만
  "error": null,                // failed 일 때만
  "role": "analyze_workout", "model": "qwen2.5:14b", "lane": "batch",
  "attempts": 1,
  "metadata": {"session_id": 42},
  "queue_position": null,       // pending 일 때 앞에 몇 개 대기 중인지 (0 = 다음 차례)
  "created_at": "...", "started_at": "...", "finished_at": "..."
}
```

동기/비동기를 분기할 필요가 없다 — `status` 만 보면 된다.

| 엔드포인트 | 설명 |
|---|---|
| `POST /v1/generate` | 생성. `wait` 초까지 기다림(0이면 즉시 pending, 최대 300) |
| `GET /v1/jobs/{id}` | 잡 조회 (본인 서비스 것만) |
| `GET /v1/jobs?status=&limit=` | 잡 목록 |
| `DELETE /v1/jobs/{id}` | 취소 (대기 중인 것만) |
| `GET /v1/roles` | 쓸 수 있는 역할·모델 |
| `GET /v1/status` | 백엔드·레인 큐·사용량 |
| `GET /v1/models/requests` | 모델 설치 요청 (승인은 hosub 만) |
| `GET /healthz` | 헬스체크 (인증 불필요) |

요청 본문:

```jsonc
{
  "role": "analyze_workout",    // 필수
  "prompt": "...",              // 필수
  "system": "...",              // 선택 — 있으면 역할 기본 프롬프트를 덮는다
  "wait": 30,                   // 선택 — 0~300초
  "priority": 0,                // 선택 — 클수록 먼저
  "metadata": {}                // 선택 — 그대로 되돌아온다
}
```

---

## 7. 실무에서 걸리는 것들

- **폴링 간격은 2초 이상.** 기본 레이트리밋이 분당 60회라 1초 폴링은 429 를 부른다.
  `wait_for()` 의 기본값(2초)이 이 때문이다.
- **`wait` 를 크게 잡으면 HTTP 타임아웃도 같이 키워야 한다.** 서버가 그만큼 붙잡고
  있기 때문. 클라이언트는 자동으로 `wait + 15` 초를 쓴다.
- **`wait` 상한은 300초.** 그보다 긴 작업은 `wait=0` + 폴링이 정석이다.
- **배치는 `lane: batch` 역할을 쓴다.** 긴 작업을 interactive 레인에 넣으면 짧은
  대화형 요청이 뒤에서 기다린다(레인마다 동시 1개).
- **잡은 게이트웨이 재시작에도 살아남는다.** 맥이 재부팅돼도 배치 분석을 잃지 않는다.
  다만 완료된 잡은 30일 후 정리되므로, 결과가 중요하면 소비자 쪽에 저장해 둔다.
- **`metadata` 를 적극 쓴다.** 어느 세션·어느 레코드의 분석인지 담아 두면 나중에
  결과를 되찾아 붙이기 쉽다.

---

## 8. 참고

- 설계 근거: [`docs/requests/llm-gateway-service.md`](../../docs/requests/llm-gateway-service.md)
- 운영·배포: [`llm-gateway/README.md`](../README.md)
- 계약 회귀 테스트: `llm-gateway/tests/test_client_contract.py`
