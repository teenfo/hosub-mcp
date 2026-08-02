# 엔드포인트·응답 필드 추가

메타데이터가 **실행 중인 앱에서 생성**되므로, 라우트를 늘리면 따라 늘려야 하는
곳이 있다. 안 하면 테스트가 잡는다 — 그게 설계다.

## 체크리스트

1. **경로를 `/v1/` 아래에 둔다.** 소비자용이면 `/v1/<이름>`, 관리용이면
   `/v1/admin/<이름>`. 그 밖은 Caddy 가 게이트웨이로 보내지 않는다.
2. **핸들러 첫 줄은 게이트다** — `_auth(request)` 또는 `_require_admin(request)`.
   공개로 둘 거면 `meta.PUBLIC_ROUTES` 에 등재하고 **왜 공개인지** 주석에 적는다
   (지금 공개는 `/healthz` 와 `/v1/docs` 둘뿐이고 후자는 설계서 7-3 에 근거가 있다).
3. **`meta.ENDPOINT_SUMMARIES` 에 한 줄.** 없으면
   `test_every_route_has_a_summary` 가 실패한다.
4. **관리 게이트가 경로 접두사로 안 드러나면 `meta.INLINE_ADMIN` 에 넣는다.**
   `POST /v1/models/requests` 가 그 예다 — `/v1/admin/*` 이 아닌데 핸들러 안에서
   `svc.admin` 을 요구한다. 접두사만 보면 소비자 API 로 오해한다.
5. **소비자용이면 `meta._consumer_paths()` 에 OpenAPI 항목을 쓴다.** 관리용이면
   쓰지 않는다 — admin 은 `x-admin-endpoints` 목록으로만 나간다(스키마 없는
   목록은 거짓말할 수 없다. 반쪽 문서화가 계약을 어긋나게 한 전례가 있다).
6. **테스트를 붙인다** — 최소: 인증 없이 401, 권한 없으면 403, 정상 응답 모양.
7. **문서 표를 갱신한다** — `llm-gateway/README.md` 와 `docs/integration.md`
   §7. 두 표 모두 "권위는 `/v1/meta` 의 endpoints" 라고 적혀 있지만, 그래도
   맞춰 둔다.

## 응답 필드를 늘릴 때

**추가는 안전하고 제거는 위험하다.**

- 클라이언트 `Job.from_dict` 가 미지 키를 버리므로 소비자가 안 깨진다
- 그래서 필드 추가로 `CONTRACT_VERSION` 을 올리지 않는다
- `_job_response()` 에 키를 늘렸다면 `tools/mock_gateway.py` 의 `shape()` 도
  같이 늘린다 — 키 집합 동일성을 카나리아가 잡는다
- **`/v1/status` 응답에 실리는 것을 확장할 때는 특히 조심한다.**
  `store.usage_summary()` 가 그 예 — 키를 늘리면 소비자 계약이 바뀐다. 새 지표가
  필요하면 `usage_by_service()` 처럼 **별도 메서드**를 만든다

## 스펙이 거짓말하지 않게

`app/meta.py` 를 고칠 때 실측으로 확인된 함정들:

- **`Error.error` 에 enum 을 걸지 않는다.** 임베딩 미설치(503)·컨텍스트
  초과(413)·백엔드 장애(502/503)와 **실패한 잡의 `error`** 에는 한국어 문장이
  온다. enum 을 걸면 `openapi-generator` 가 진짜 응답을 거부한다. 코드 목록은
  `x-error-codes` 와 `/v1/meta.error_codes` 로 **참고 표**로만 낸다.
- **임베딩 실패는 `Job` 도 `Error` 도 아니다** — `EmbedError`
  (`{status, error, retryable, hint?, model_request?}`) 다.
- **`DELETE /v1/jobs/{id}` 성공은 `Job` 이 아니다** — `{"status":"cancelled"}` 뿐
  (`job_id` 도 없다).
- **`GET /v1/jobs` 목록 항목에는 `queue_position` 이 없다** — 단건 조회에만 있다.
- **`options` 는 `/v1/meta` 에만 있다** — `/v1/roles`·`/v1/status` 는
  `role.to_dict()` 를 그대로 쓴다. 앱을 통일하지 말고 스펙에 차이를 적는다.
- 새 오류 코드를 방출하면 `meta.ERROR_CODES` 에 넣는다. 관리 전용이면
  `"admin": True` — 비-admin 토큰에는 걸러 나간다.
  `test_every_error_code_emitted_by_main_is_documented` 가 `main.py` 를 grep 해
  대조하므로 잊으면 실패한다.

## 새 데이터를 저장할 때

`Store._migrate()` 는 `PRAGMA table_info` 로 현재 컬럼을 보고 없는 것만
`ALTER TABLE ADD COLUMN` 한다. **append-only 다.**

- 컬럼 추가는 안전. 제거·개명은 하지 않는다(실서버 DB 에 잡 수천 건이 있다)
- 새 테이블은 `CREATE TABLE IF NOT EXISTS` 로 스키마 문자열에 추가
- 마이그레이션은 기동 시 자동 — 배포하면 그냥 반영된다

## 대시보드까지 붙일 때

`src/gateway.py` 의 규약: **실패해도 예외 대신 status/error dict.** 호출부가
그대로 반환하기 때문이다.

- JSON 응답이면 `_call()` 을 쓴다
- **JSON 이 아니면 `_call()` 을 쓸 수 없다**(`res.json()` 이 터진다).
  `integration_doc()` 이 유일한 템플릿 — `res.text` 를 dict 로 감싼다.
  그래서 대시보드 다운로드는 브라우저에서 Blob 으로 되돌린다(레포에
  `StreamingResponse`·`Content-Disposition` 선례가 없다)
- `src/dashboard.py` 의 새 라우트는 **`/api/*` 아래**여야 한다 — Caddy 가 그것만
  :8701 로 보낸다. 핸들러마다 `_require_auth_json`
- **조회는 감사 로그를 남기지 않는다.** 변경 계열만 `ctx.audit.log(...)`
- `static/pages/llm-models.js` 의 헬퍼(`copyBtn`·`urlRow`·`alertBox`·`spinner`)는
  모듈 레벨이 아니라 **`render()` 안 클로저**다. 새 코드도 같은 `render()` 안,
  그 선언들 뒤에 와야 한다(`const` TDZ)
- **남의 서비스가 준 문자열을 `innerHTML` 에 넣지 않는다** — 텍스트 노드로,
  클래스는 화이트리스트 패턴으로 거른다
