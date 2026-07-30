# 소비 프로젝트 통합 가이드

roxlogy·BCL·TNM·trading 등 **다른 레포**에서 hosub 의 공유 LLM 게이트웨이를 쓰는 법.

> **워커도, 컨테이너도 필요 없다.** HTTP 호출만 하면 되고, 게이트웨이의 잡 큐가
> 공용 워커 역할을 한다. 게이트웨이가 아직 없어도 개발을 시작할 수 있다(3절).

**이 문서는 게이트웨이가 직접 서빙한다.** 저장소 접근 없이 언제나 최신본을 받을 수 있다:

```bash
curl -H "Authorization: Bearer $LLMGW_TOKEN" $LLMGW_URL/v1/integration
```

소비 프로젝트는 이 문서를 복사하지 말고 이 URL 을 참조하는 편이 낫다 — 계약이 두
곳에 있으면 반드시 어긋난다.

> ### 📣 최근 계약 변경 — 반드시 읽을 것
>
> **역할의 모델이 이제 런타임에 바뀔 수 있다.** 운영자가 PR·배포 없이 교체하므로
> 응답의 `model` 값이 호출마다 다를 수 있다. 모델 이름을 코드에 하드코딩하지 말고
> 필요하면 `GET /v1/roles` 로 확인하라. **역할 이름·응답 형태·엔드포인트는 그대로다.**
> 자세한 내용과 나머지 변경(모델 삭제 가능, `model` 필드는 관리 전용, `/v1/admin/*`
> 는 공개 경로에서 404)은 **[6-1절](#6-1-역할의-모델은-런타임에-바뀔-수-있다-️)**.

> **저장소 접근이 없어도 1절을 그대로 쓸 수 있다.** 1~3절이 쓰는 두 파일
> (`client/llmgw.py`, `tools/mock_gateway.py`)을 게이트웨이가 직접 서빙한다:
>
> ```bash
> curl -H "Authorization: Bearer $LLMGW_TOKEN" $LLMGW_URL/v1/client/llmgw.py -o llmgw.py
> curl -H "Authorization: Bearer $LLMGW_TOKEN" $LLMGW_URL/v1/client/mock_gateway.py -o mock_gateway.py
> ```
>
> 파이썬이 아니라면 [1-A](#1-a-저장소-없이-시작-http-만으로)로 가거나
> `GET /v1/openapi.json` 으로 타입 있는 클라이언트를 생성하라.

---

## 1. 5분 만에 시작 (파이썬)

```bash
# 1) 클라이언트 한 파일을 자기 레포에 가져온다
#    저장소가 있으면:
cp llm-gateway/client/llmgw.py <내_프로젝트>/lib/
#    없으면 게이트웨이에서 받는다 (토큰만 있으면 된다):
curl -H "Authorization: Bearer $LLMGW_TOKEN" \
  https://hosub.duckdns.org/llm/v1/client/llmgw.py -o <내_프로젝트>/lib/llmgw.py

# 2) 게이트웨이 없이 개발 시작 — 목 서버를 띄운다
python llm-gateway/tools/mock_gateway.py
#    (목 서버도 받을 수 있다: $LLMGW_URL/v1/client/mock_gateway.py)

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

### 내 사본이 최신인가

`llmgw.py` 에는 버전 문자열이 없다. **해시로 확인한다** — 게이트웨이가 서빙하는
바이트에서 계산한 값을 `GET /v1/meta` 가 알려 준다:

```bash
# 내 사본
sha256sum lib/llmgw.py
# 게이트웨이가 들고 있는 것
curl -sH "Authorization: Bearer $LLMGW_TOKEN" $LLMGW_URL/v1/meta \
  | python3 -c 'import json,sys; c=json.load(sys.stdin)["client"]["files"]["python"]; print(c["sha256"], c["bytes"])'
```

같으면 최신이다. 다르면 `GET /v1/client/llmgw.py` 로 다시 받는다.
`/v1/meta` 의 `client.files.python` 에는 `entrypoints`·`exceptions`·`notes` 도 함께
온다 — **OpenAPI 로는 표현되지 않는 것들**(폴링 루프, `wait+15` 타임아웃 자동
확대, 미지 필드 무시, `cancel()` 이 오류를 삼켜 `False` 를 주는 것)이 거기 적혀 있다.

---

## 1-A. HTTP 만으로 시작 (파이썬이 아닐 때)

라이브러리도 목 서버도 필요 없다. 알아야 할 것은 **토큰 하나와 엔드포인트 네 개**다.
타입 있는 클라이언트를 만들려면 스펙을 그대로 코드 생성기에 먹인다:

```bash
curl -H "Authorization: Bearer $LLMGW_TOKEN" $LLMGW_URL/v1/openapi.json -o gw.json
# 스펙은 이 토큰 기준으로 생성된다 — role enum 에 쓸 수 있는 역할만 들어 있다
```

```bash
export LLMGW_URL=https://hosub.duckdns.org/llm
export LLMGW_TOKEN=<관리자에게 받은 값>

# 이 토큰으로 쓸 수 있는 역할·모델 확인 — 여기서부터 시작하면 된다
curl -H "Authorization: Bearer $LLMGW_TOKEN" $LLMGW_URL/v1/roles
```

```bash
# 짧은 작업 — 결과를 기다린다
curl -X POST $LLMGW_URL/v1/generate -H "Authorization: Bearer $LLMGW_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"role":"summarize","prompt":"...","wait":30}'

# 긴 작업 — job_id 만 받고 나중에 폴링 (서버리스는 반드시 이 방식, 5절)
curl -X POST $LLMGW_URL/v1/generate -H "Authorization: Bearer $LLMGW_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"role":"analyze_workout","prompt":"...","system":"내 프롬프트","wait":0}'
curl -H "Authorization: Bearer $LLMGW_TOKEN" $LLMGW_URL/v1/jobs/<job_id>
```

구현에 필요한 것은 이 네 가지뿐이다:

1. **응답은 항상 같은 모양**이고 `status` 가 `ok|pending|failed` 다 → 7절
2. `pending` 이면 `job_id` 로 폴링한다(**2초 이상 간격**) → 8절
3. 오류 코드의 의미(재시도 가치가 있는지) → 아래 표
4. 모델이 아직 없으면 자동으로 설치 요청이 생긴다 → 6절

| 코드 | 의미 | 대응 |
|---|---|---|
| 401 / 403 | 토큰 없음·틀림 / 역할 권한 없음 | **재시도 무의미.** 설정 확인 |
| 404 / 413 | 모르는 역할 / 입력 초과 | **재시도 무의미.** 코드 수정 |
| 429 | 레이트리밋 | 간격을 늘려 재시도 |
| 503 | 백엔드 일시 불가·모델 설치 대기 | **나중에 재시도** |
| 5xx 기타 | 게이트웨이 오류 | 백오프 후 재시도 |

> ⚠️ **`error` 필드로 분기하지 말 것.** 검증·권한 오류에서는 기계 코드
> (`unauthorized`·`unknown_role` 등, 전체 목록은 `/v1/meta` 의 `error_codes`)가
> 오지만, **모델 미설치·백엔드 장애·실패한 잡에서는 사람이 읽는 문장이 온다.**
> 분기는 **HTTP 상태와 `retryable`** 로 하라. `/v1/embed` 의 실패 본문은
> `{status, error, retryable}` 모양이고 `status:"failed"` 를 함께 들고 온다.

2절의 파이썬 클라이언트는 위 규칙을 감싼 것일 뿐이다. 직접 구현해도 동일하다.

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
| **집 밖**(Vercel·클라우드 등) | `https://hosub.duckdns.org/llm` — 5절 참고 |

---

## 5. 집 밖에서 붙이기 (Vercel 등)

roxlogy 처럼 서버리스/클라우드에서 도는 소비자용. Caddy 가 `/llm/v1/*` 만 게이트웨이로
넘긴다(`deploy/Caddyfile`). `/healthz` 는 공개하지 않는다.

```bash
LLMGW_URL=https://hosub.duckdns.org/llm
LLMGW_TOKEN=<LLMGW_TOKEN_ROXLOGY 와 같은 값>
```

### 서버리스는 절대 기다리지 않는다

Vercel 함수는 플랜에 따라 최대 60~300초다. LLM 을 기다리며 그 시간을 태우는 건
비용·안정성 양쪽에서 나쁘다. **`wait=0` 으로 던지고 job_id 만 저장한 뒤 즉시 응답**하고,
브라우저가 폴링하게 한다.

```
[브라우저] 운동 기록 저장
   → [route] gw.generate(..., wait=0) → job_id 를 DB 에 저장하고 즉시 응답
   → 화면은 "분석 중"
[브라우저] 몇 초마다 /api/analysis/{id}
   → [route] gw.job(job_id) → 완료되면 결과 저장 + 반환
```

크론이 아니라 브라우저 폴링을 권한다 — Vercel Cron 은 플랜에 따라 주기 제한이 있고,
브라우저 폴링은 UI 피드백이 자연스럽다.

```ts
// app/api/analysis/route.ts — 분석 시작 (서버 사이드에서만!)
const res = await fetch(`${process.env.LLMGW_URL}/v1/generate`, {
  method: "POST",
  headers: { Authorization: `Bearer ${process.env.LLMGW_TOKEN}`,
             "Content-Type": "application/json" },
  body: JSON.stringify({
    role: "analyze_workout",
    prompt: JSON.stringify(workout),
    system: COACH_PROMPT,          // 프롬프트는 roxlogy 소유
    wait: 0,                       // 기다리지 않는다
    metadata: { session_id: session.id },
  }),
});
const { job_id } = await res.json();
await db.session.update({ where: { id: session.id }, data: { llmJobId: job_id } });
return Response.json({ status: "analyzing" });
```

```ts
// app/api/analysis/[id]/route.ts — 폴링 대상
const r = await fetch(`${process.env.LLMGW_URL}/v1/jobs/${jobId}`,
  { headers: { Authorization: `Bearer ${process.env.LLMGW_TOKEN}` } });
const job = await r.json();          // status: ok | pending | failed
if (job.status === "ok") await db.session.update({ ... , data: { analysis: job.response } });
return Response.json(job);
```

### 반드시 지킬 것

- **토큰은 서버 사이드에만.** `NEXT_PUBLIC_` 접두어를 붙이면 브라우저에 노출된다.
  브라우저는 게이트웨이를 직접 부르지 않고 자기 서비스의 라우트를 부른다
  (그래서 Caddy 에서 CORS 를 열지 않았다)
- **Vercel 환경변수는 Production 에만 설정.** Preview/Development 까지 체크하면
  모든 PR 프리뷰가 토큰을 갖는다. 프리뷰에서는 목 서버를 쓰거나 분석을 건너뛴다
- **폴링은 2초 이상.** roxlogy 한도는 분당 30회다(=2초). 더 자주 부르면 429
- **집 서버는 가정용 회선이다.** 정전·재부팅·공인 IP 변경(DuckDNS 갱신 5분 주기)에
  대비해 `GatewayError` 는 치명적 실패로 다루지 말고 "나중에 다시"로 처리한다.
  `llmJobId` 를 저장해 두면 서버가 돌아왔을 때 결과를 그대로 이어받을 수 있다
  (잡은 게이트웨이 재시작에도 살아남는다)

### 위험 범위

게이트웨이 토큰이 새면 피해는 **맥 GPU 소모**까지다 — MCP 토큰(=root)과 달리 서버가
장악되지 않는다. `allow_roles` 로 쓸 수 있는 역할이, `rate_limit_per_min` 으로 소비량이
제한된다. 유출이 의심되면 `.env` 의 토큰만 갈아끼우고 게이트웨이를 재시작하면 된다.

인증 실패는 클라이언트 IP 와 함께 로그에 남으므로 `docker logs llm-gateway` 로 누가
두드리는지 볼 수 있다. 스캐너가 붙으면 시끄러울 수 있는데, 컨테이너 로그는 10MB×3 으로
회전하므로 디스크는 안전하다.

---

## 5-1. 임베딩 (`POST /v1/embed`)

RAG·유사도·중복 제거용 벡터. **이것만 잡이 아니라 동기로 바로 돌아온다.**

```bash
curl -X POST $LLMGW_URL/v1/embed -H "Authorization: Bearer $LLMGW_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"role": "embed", "input": ["첫 문장", "둘째 문장"]}'
```
```jsonc
{
  "status": "ok",
  "model": "bge-m3",
  "embeddings": [[...], [...]],   // 입력 순서와 같다
  "count": 2,
  "dimensions": 1024,
  "duration_ms": 42
}
```

**왜 큐를 안 태우나.** 임베딩은 밀리초 단위로 끝나고 보통 즉시 응답 경로에서 쓴다.
2레인 큐에 넣으면 3분짜리 32b 생성 뒤에서 기다리게 되어 쓸모가 없어진다. 대신
인증·역할 제한·레이트리밋·사용량 집계는 생성과 똑같이 적용된다.

**배치로 보내라.** `input` 에 배열을 주면 한 번에 처리된다. 100건을 100번 호출하면
레이트리밋에 걸리고 느리다 — 한 번에 보내는 쪽이 훨씬 빠르다(최대 256건).

```python
vecs = gw.embed([item.title for item in items])   # 한 번에
```

실패 시 `503`(재시도 가치 있음 — 맥 재부팅 등) 또는 `502`(모델 문제 등)로 구분된다.

> 임베딩 역할은 `roles.yaml` 에 `kind: embed` 로 등록한다. 생성용 역할을
> `/v1/embed` 에 주면(또는 반대로) `400 wrong_role_kind` 로 즉시 거부된다 —
> 조용히 이상한 결과가 나오는 것보다 낫다.

---

## 6. 필요한 역할·모델이 없을 때

**역할 추가**는 `config/roles.yaml` 에 PR 을 올린다(모델·레인·타임아웃만 정한다).
**프롬프트는 게이트웨이에 올리지 않는다** — 요청의 `system` 으로 보내면 되고, 그래야
자기 레포에서 자유롭게 개선할 수 있다.

> 운영자는 hosub 대시보드에서 역할을 추가할 수도 있지만, 그렇게 만든 역할은
> `services.yaml` 의 `allow_roles` 에 없으므로 **당신의 토큰에는 보이지 않는다.**
> 외부 서비스가 쓸 역할은 여전히 PR 을 거친다.

**모델이 아직 맥에 없으면** 아무것도 안 해도 된다. 첫 호출 때 게이트웨이가 설치
요청을 만들고, hosub 대시보드에서 승인하면 자동으로 설치된 뒤 대기하던 잡이 이어서
실행된다. 그동안 그 잡은 `pending` 이고, **다른 잡은 막히지 않는다.**

승인 전까지 잡이 오래 `pending` 이면 이유를 확인할 수 있다:

```python
gw.model_requests()   # [{"model": "qwen3:32b", "status": "pending", ...}]
```

거부되거나 설치가 실패하면 그 잡은 무한 대기 대신 `JobFailed` 로 끝난다.

---

## 6-1. 역할의 모델은 런타임에 바뀔 수 있다 ⚠️

**이게 이 문서에서 가장 최근에 바뀐 계약이다.** 운영자가 hosub 대시보드에서
역할의 모델(그리고 레인·타임아웃·옵션)을 **PR·배포 없이** 바꿀 수 있다.
`config/roles.yaml` 은 이제 기본값이고, 런타임 오버라이드가 그 위에 얹힌다.

소비자 입장에서 달라지는 것:

- **모델 이름을 코드에 하드코딩하지 마라.** 역할 이름이 계약이고 모델은 정책이다.
  지금 무엇이 붙어 있는지 알아야 하면 `GET /v1/roles` 로 확인한다
  ```python
  [r for r in gw.roles() if r["name"] == "analyze_workout"][0]["model"]
  ```
- **응답의 `model` 필드가 호출마다 다를 수 있다.** 로깅·분기·비용 계산에 쓰고
  있다면 고정값을 가정하지 말 것. 잡 행에는 **생성 시점의 모델**이 박히므로,
  이미 큐에 들어간 잡은 교체 후에도 **옛 모델로 실행된다**(옵션·타임아웃도 함께
  스냅샷된다). 교체가 진행 중인 작업을 오염시키지 않는다는 뜻이다
- **모델이 삭제될 수도 있다.** 그 경우 다음 잡은 6절의 "미설치 → 설치 요청 →
  승인" 흐름을 그대로 타고 `pending` 이 된다. **새로운 실패 모드는 없다**
- **역할이 추가될 수 있다.** 단 `allow_roles` 에 없으면 당신의 토큰에는 안 보인다.
  `GET /v1/roles` 가 보여주는 것이 곧 쓸 수 있는 전부다

역할 이름·응답 형태·엔드포인트는 그대로다 — 바뀌는 것은 **역할 뒤의 모델**뿐이다.

### 쓸 수 없는 것 (관리 전용)

- **`POST /v1/generate` 의 `model` 필드는 관리 서비스 전용**이다. 소비자 토큰으로
  보내면 `403 forbidden` 이다. 역할이 모델을 정한다는 계약을 지키기 위해서다
- **`/v1/admin/*` 는 공개 경로에서 404** 다. 리버스 프록시가 잘라내므로 집 밖에서는
  존재하지 않는다 — 시도할 필요가 없다

---

## 7. 응답 계약 (직접 HTTP 를 부를 때)

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
| `POST /v1/embed` | 임베딩 벡터. **유일하게 잡이 아닌 엔드포인트** |
| `GET /v1/integration` | **이 문서** (마크다운). 계약의 최신본 |
| `GET /v1/meta` | 기계가 읽는 계약 — 역할·한도·오류코드·클라이언트 해시 |
| `GET /v1/openapi.json` | OpenAPI 3.1 스펙. `?download=1` 로 파일 저장 |
| `GET /v1/openapi.yaml` | 같은 스펙(YAML) |
| `GET /v1/client/llmgw.py` | 파이썬 클라이언트 원본 |
| `GET /v1/client/mock_gateway.py` | 개발용 목 게이트웨이 원본 |
| `GET /healthz` | 헬스체크 (인증 불필요) |

> 이 표는 손으로 관리한다 — 권위 있는 목록은 **`GET /v1/meta` 의 `endpoints`** 다.
> 그쪽은 실행 중인 라우터에서 만들어지므로 어긋날 수 없다.

요청 본문:

```jsonc
{
  "role": "analyze_workout",    // 필수
  "prompt": "...",              // 필수
  "system": "...",              // 선택 — 있으면 역할 기본 프롬프트를 덮는다
  "wait": 30,                   // 선택 — 0~300초
  "priority": 0,                // 선택 — 클수록 먼저
  "metadata": {}                // 선택 — 그대로 되돌아온다
  // "model" 은 관리 전용이다 — 소비자가 보내면 403 (6-1절)
}
```

> `"role"` 은 계약, `"model"`(응답)은 정책이다. 역할의 모델은 운영자가 런타임에
> 바꿀 수 있으므로 응답의 `model` 이 매번 같다고 가정하지 말 것 — **6-1절.**

---

## 8. 실무에서 걸리는 것들

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
- **역할 모델을 교체해도 이미 큐에 있던 잡은 옛 모델로 실행된다.** 잡은 생성 시점의
  모델·옵션·타임아웃을 자기 행에 스냅샷하기 때문이다. "지금 막 바꿨는데 왜 옛
  모델로 나오지" 는 정상이며, 다음 요청부터 새 모델이다(6-1절).
- **응답의 `model` 을 캐시 키·비용 계산에 쓴다면 값이 바뀔 수 있음을 전제로 짠다.**
  모델 이름으로 분기하는 코드가 있으면 역할 이름으로 바꾸는 편이 안전하다.

---

## 9. 참고

- 설계 근거: [`docs/requests/llm-gateway-service.md`](../../docs/requests/llm-gateway-service.md)
- 운영·배포: [`llm-gateway/README.md`](../README.md)
- 계약 회귀 테스트: `llm-gateway/tests/test_client_contract.py`
