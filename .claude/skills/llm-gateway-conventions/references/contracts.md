# 소비자 계약 — 지금 무엇이 약속돼 있나

붙어 있는 소비자와 각자의 권한:

| 서비스 | 쓸 수 있는 역할 | 분당 | admin | 어디서 도는가 |
|---|---|---|---|---|
| `hosub` | `*` | 120 | **예** | 같은 호스트 (대시보드·MCP) |
| `roxlogy` | `analyze_workout`·`coach_feedback`·`summarize` | 30 | 아니오 | **Vercel (다른 레포)** |
| `tnm` | `summarize`·`classify_news`·`translate`·`embed` | 120 | 아니오 | 같은 호스트 :8602 |
| `trading` | `summarize`·`general` | 60 | 아니오 | 같은 호스트 :8600 |

`admin: true` 는 hosub 하나뿐이다. 늘리지 않는다.

## 핵심 계약: 모든 요청은 잡이다

`/v1/generate` 는 성공·대기·실패 **모두 200** 으로 같은 모양을 준다. 호출자는
`status` 만 보면 되고 동기/비동기를 분기할 필요가 없다.

```jsonc
{ "job_id": "...", "status": "ok|pending|failed|cancelled",
  "response": "...", "error": null, "role": "...", "model": "...",
  "lane": "interactive|batch", "attempts": 1, "metadata": {},
  "queue_position": null,          // pending 일 때, 단건 조회에만
  "created_at": "...", "started_at": "...", "finished_at": "..." }
```

예외는 `/v1/embed` 하나 — 잡 큐를 타지 않고 동기 응답한다.

## 오류 — 코드인 것과 문장인 것

**분기는 HTTP 상태와 `retryable` 로 한다.** `error` 문자열로 분기하면 깨진다.

| 상태 | 뜻 | 재시도 |
|---|---|---|
| 401 / 403 | 토큰 없음·틀림 / 역할 권한 없음 | 무의미 |
| 404 / 413 | 모르는 역할 / 입력 초과 | 무의미 |
| 429 | 레이트리밋 | 간격 늘려서 |
| 502 / 503 | 백엔드 장애·모델 미설치 | 나중에 |

`error` 에 **기계 코드**가 오는 곳: 검증·권한 오류(`unauthorized`·`unknown_role`·
`prompt_too_long` 등, 전체 목록은 `meta.ERROR_CODES`).

`error` 에 **한국어 문장**이 오는 곳:
- `/v1/embed` 의 모델 미설치(503)·`InputTooLong`(413)·`BackendError`(502/503)
- **실패한 잡의 `error`** — 모든 소비자가 폴링하면 받는다

이 비대칭이 스펙에서 `enum` 을 뺀 이유다.

## 클라이언트 파일이 진짜 인터페이스다

소비자는 HTTP 를 손으로 짜지 않고 `client/llmgw.py` **한 파일을 복사**해 쓴다.

```
llm-gateway/client/llmgw.py   ← 정본
trading/app/llmgw.py          ← 바이트 동일
tnm/app/llmgw.py              ← 바이트 동일
```

**비교 장치가 레포에 없다.** `.github/` 없음, `Makefile` 없음, pre-commit 없음,
어떤 `*.sh`·conftest 도 셋을 비교하지 않는다. 지금 같은 건 손으로 셋 다 붙여넣은
결과일 뿐이다. 그래서:

- 정본을 고치면 **같은 커밋에서 사본 둘도** 고친다
- 최신성은 `/v1/meta` 의 `client.files.python.sha256` 으로 확인한다. 그 값은
  **서빙할 바이트에서 계산**되므로 하드코딩하면 즉시 어긋난다
- 대시보드 카드가 그 해시를 앞쪽에 크게 띄우는 이유다

### OpenAPI 로 표현되지 않는 것들 (`/v1/meta` 의 `client.notes`)

스펙으로 클라이언트를 생성하면 아래가 전부 사라진다. 그래서 파이썬 소비자에게는
파일을 받으라고 안내한다.

- 자동 재시도가 **없다** — 재시도는 호출자 책임
- `wait_for` 기본 폴링 2초 (레이트리밋이 분당 60이라 1초는 429)
- `generate` 는 HTTP 타임아웃을 자동으로 `wait+15` 로 넓힌다
- `embed` 타임아웃은 120초 고정
- `Job.from_dict` 는 모르는 필드를 버린다 (전방 호환의 근거)
- `cancel` 은 모든 오류를 삼켜 `False` 를 준다 — 인증 실패도 `False`

## 공개 경계

```
공인 인터넷 ─▶ Caddy ─▶ /llm/v1/*        프록시 (단, /llm/v1/admin/* 은 404)
                        그 밖의 /llm/*   종단 404
같은 호스트 ─▶ http://127.0.0.1:8603/v1/*   전부 (관리 포함)
```

- **CORS 를 열지 않는다** — 브라우저가 게이트웨이를 직접 부르면 토큰이 노출된다.
  프로덕션 소비자는 자기 서버 사이드 라우트에서만 호출한다
- 무인증 경로는 `/healthz` 와 `/v1/docs` 둘뿐. 후자는 브라우저가 최상위
  내비게이션에 헤더를 못 싣기 때문이고, **껍데기에 서버 데이터가 없다**는 것이
  그 예외를 정당화한다(테스트가 단어 경계로 단언한다)
- 관리 표면은 admin 토큰에도 `x-admin-endpoints` **목록**으로만 나간다.
  스키마 없는 목록은 거짓말할 수 없다

## 토큰

- `services.yaml` 에는 **env 변수 이름만**, 값은 `llm-gateway/.env`(gitignore)
- 비면 그 서비스는 **경고 없이 비활성**된다 — `/v1/admin/services` 가 드러낸다
- 목록은 마스킹(`앞6…뒤6`)과 sha256 앞 12자만. 전체 값은
  `/v1/admin/services/{name}/token` 으로 **한 번에 하나**, 열람은 양쪽 감사에
- **발급·회전·폐기는 없다.** 새 소비자는 `services.yaml` PR → `.env` → 재기동
