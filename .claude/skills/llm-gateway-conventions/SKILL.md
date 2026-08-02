---
name: llm-gateway-conventions
description: >
  공유 LLM 게이트웨이(`llm-gateway/`, 도커 127.0.0.1:8603)를 건드리는 모든 작업에
  반드시 먼저 읽는 계약·경계 가드. 엔드포인트/역할/모델/스케줄러/스펙·메타데이터
  추가·수정, `roles.yaml`·`services.yaml`·`catalog.yaml` 편집, 소비자 토큰·권한,
  게이트웨이 배포·재빌드·롤백이면 무조건 이 스킬을 쓴다 — "필드 하나만 추가",
  "역할 이름만 바꿔줘" 같은 작은 요청도 예외가 아니다. **다른 레포(Vercel 의
  roxlogy)와 다른 서비스(tnm·trading)가 이 응답 모양에 붙어 돌고 있어**, 키 하나가
  바뀌면 남의 프로덕션이 조용히 깨진다. `/v1/*` · `/llm/*` · llmgw.py · Ollama
  역할 · 잡 큐 · 모델 설치 승인이 나오는 요청에도 적용.
---

# LLM 게이트웨이 계약 가드

이 서비스는 **hosub 것이 아니라 공용**이다. 지금 이 순간 Vercel 의 roxlogy,
tnm(19,000+ 호출), trading 이 붙어 있고 각자 다른 레포에서 다른 사람이 배포한다.
그래서 여기서 깨지는 것은 이 저장소의 테스트가 아니라 **남의 서비스**다.

`llm-gateway/` 는 저장소 안에서 **완전히 독립**이다 — 자체 `pytest.ini`,
자체 `requirements.txt`, 자체 도커 이미지, 자체 배포 경로. 루트 `src/` 에서
게이트웨이 코드를 임포트하는 곳은 하나도 없다(확인됨). 그 분리를 유지한다.

## 경계 — 무엇을 건드리고 무엇을 안 건드리나

| | |
|---|---|
| **고친다** | `llm-gateway/` 아래 전부 (`app/`·`config/`·`docs/`·`tests/`·`client/`·`tools/`·`static/`·`Dockerfile`·`compose.yml`) |
| **같이 고칠 수 있다** | `src/gateway.py`(대시보드용 클라이언트)·`src/dashboard.py`의 `/api/llm/*`·`static/pages/llm*.js` — 게이트웨이가 낸 것을 화면에 붙일 때만 |
| **건드리지 않는다** | `trading/`·`tnm/`·`src/` 의 나머지·`static/pages/` 의 나머지·`docs/trading/` |
| **예외** | `client/llmgw.py` 를 고치면 사본 둘(`trading/app/llmgw.py`·`tnm/app/llmgw.py`)을 **같은 커밋에서** 함께 바꾼다 |

게이트웨이 밖으로 번지는 변경이 필요해 보이면 멈추고 사용자에게 확인한다.
대개는 게이트웨이 안에서 끝낼 방법이 있다.

## 불변 조건 — 어떤 작업에서도 깨면 안 되는 것

1. **응답 모양은 계약이다.** 기존 `/v1/*` 응답에서 키를 빼거나 의미를 바꾸지
   않는다. **추가는 안전하다** — 클라이언트의 `Job.from_dict` 가 미지 키를 버리고
   (`test_client_ignores_unknown_fields`), 그래서 필드 추가로는
   `CONTRACT_VERSION` 을 올리지 않는다. 빼는 것만 위험하다.
2. **역할 이름이 계약이고 모델은 정책이다.** `classify_news`(tnm)·
   `analyze_workout`·`coach_feedback`(roxlogy)은 레포 간 계약이라 삭제·개명하지
   않는다. 모델은 운영자가 런타임에 바꾸므로 코드가 모델 이름을 가정하지 않는다.
3. **엔드포인트 목록을 손으로 적지 않는다.** `meta.route_inventory` 가
   `app.routes` 에서 유도한다. 라우트를 추가하면 `meta.ENDPOINT_SUMMARIES` 에 한
   줄을 달아야 테스트가 통과한다 — 손 목록은 반드시 어긋난다는 게 실측이다
   (`README.md`·`integration.md` 표가 둘 다 `/v1/meta` 계열을 빠뜨린 채 방치됐다).
4. **공개 경로는 `/v1/` 아래에만 만든다.** Caddy 의 `@api path /v1/*` 가 커버하고
   그 밖은 종단 404 다 — 맨 `/docs` 같은 건 게이트웨이에 **닿지도 않는다**.
   `/v1/admin/*` 은 Caddy 가 공개에서 404 로 자르고 앱도 `admin: true` 만
   통과시킨다. **두 겹 다 유지한다**(프록시만 믿지 않는다).
5. **오류의 `error` 는 enum 이 아니다.** 검증·권한 오류에는 기계 코드가 오지만
   모델 미설치·백엔드 장애·실패한 잡에는 **사람이 읽는 문장**이 온다. 스펙에
   enum 을 다시 걸면 엄격한 검증기와 `openapi-generator` 가 진짜 응답을 거부한다.
   분기는 HTTP 상태와 `retryable` 로 한다.
6. **비밀을 응답 하나에 몰아 담지 않는다.** 토큰 목록은 마스킹·지문만, 전체 값은
   한 번에 하나(`/v1/admin/services/{name}/token`), 열람은 게이트웨이
   `admin_audit` 과 대시보드 감사 **양쪽**에. 값은 어느 로그에도 남기지 않는다.
7. **`config/` 만 바인드 마운트다.** `roles.yaml`·`services.yaml`·`catalog.yaml`
   은 `git pull` 로 즉시 반영되지만 `app/`·`docs/`·`client/`·`tools/`·`static/`
   은 이미지에 구워진다 — 고쳤으면 **재빌드해야 반영된다**.
8. **잡은 재시작에도 살아남는다.** 스키마 변경은 `Store._migrate` 의 append-only
   `ALTER TABLE ADD COLUMN` 뿐이다. 컬럼을 지우거나 의미를 바꾸면 큐에 있던
   배치 분석이 사라진다.
9. **정본 `client/llmgw.py` 와 사본 둘은 바이트 동일**이다. 비교 장치가 레포에
   없으므로(`.github/` 없음, `Makefile` 없음, pre-commit 없음) `/v1/meta` 의
   동적 sha256 이 유일한 드리프트 탐지기다. 그 해시를 하드코딩하지 않는다.

## 작업 유형 라우터

| 하려는 일 | 레시피 |
|---|---|
| 어느 파일을 고쳐야 하는지 모르겠다 | `references/layout.md` |
| 엔드포인트·응답 필드 추가, 스펙/메타데이터 수정 | `references/new-endpoint.md` |
| 소비자 계약 확인 — 응답 모양·오류·클라이언트 파일·토큰 | `references/contracts.md` |
| 커밋·PR·배포·검증·롤백 | `references/ship.md` |

레시피는 짧다 — 해당 파일 하나만 읽으면 된다. 두 유형에 걸치면 둘 다 읽는다.

## 테스트 규약

게이트웨이는 **자체 스위트**다. 루트에서 돌리면 잡히지 않는다.

```bash
cd llm-gateway && ../.venv/bin/python -m pytest -q     # 231개
cd .. && .venv/bin/python -m pytest -q                  # 189개 (대시보드·MCP)
node --check <(cat static/pages/llm-models.js)          # JS 를 고쳤으면
```

`src/gateway.py`·`src/dashboard.py`·`static/pages/llm*.js` 를 함께 고쳤다면
**둘 다 초록이어야 커밋한다.**

### 건드리면 안 되는 카나리아

의도적으로 값을 박아 둔 테스트들이다. 깨졌다면 **그 값을 고치기 전에 왜 깨졌는지**
본다 — 대개 표면이 조용히 바뀌었다는 신호다.

| 위치 | 지키는 것 |
|---|---|
| `tests/test_api.py:175` `== 6` | 픽스처 역할 수 = 토큰 필터링이 살아 있는가 |
| `tests/test_client_contract.py` | 목 서버 `shape()` 키 집합 == `_job_response` 키 집합 |
| `tests/test_client_contract.py` | 목 역할명 == 실제 `config/roles.yaml` |
| 루트 `tests/test_http.py:94` `== 19` | MCP 도구 표면이 조용히 늘지 않는가 |
| `tests/test_meta.py` | 라우트↔스펙 양방향, `main.py` grep 오류코드 대조 |

- 테스트 이름·주석·docstring 은 **한국어**. 주석은 "무엇"이 아니라 **왜**.
- 버그 수정에는 회귀 테스트를 함께 — "이게 다시 나면 이 테스트가 잡는다".
- 새 테스트를 쓸 때 **일부러 깨 본다.** 통과만 보면 아무것도 안 보고 있을 수 있다
  (실측: 유출 테스트가 부분 문자열 매칭이라 무관한 CSS 값에 걸린 적이 있다).
- 가짜 백엔드는 `tests/conftest.py` 의 `FakeOllama` — 지연·실패·모델 목록·
  디스크 크기·tok/s 를 다 조종할 수 있다. 새 시나리오가 필요하면 거기 필드를 는다.

## 하지 않을 것 (사고·결정으로 확정된 금지 목록)

- **기존 응답에서 키 제거·개명** · **역할 삭제·개명** (남의 프로덕션이 깨진다)
- **`services.yaml` 에 웹 UI 오버라이드 층** — 공개 도달 가능한 토큰에 웹 UI 로
  역할 권한을 주는 것은 권한 상승이다(설계서 7-1). 토큰 **발급·회전·폐기**도
  같은 이유로 자동화하지 않는다. 관측·열람만 열려 있다(7-4)
- **CORS 열기** — 브라우저가 게이트웨이를 직접 부르면 토큰이 노출된다
- **`/v1/` 밖에 공개 경로 만들기** (Caddy 가 안 보낸다) · **Caddyfile 에
  `/v1/admin` 을 여는 것**
- **`systemctl reload llm-gateway` 로 배포** — 재빌드는 하지만 `git pull` 도
  드리프트 마커(`.deployed-tree`) 갱신도 안 한다. `deploy_service` 를 쓴다
- **큐가 차 있을 때 재빌드** — 컨테이너가 재생성된다. `llm_status` 로 양 레인이
  0/0 인지 먼저 본다(잡은 살아남지만 실행 중이던 건 재시도된다)
- **FastAPI 전환** — Starlette 다. 자동 OpenAPI 가 없어서 `app/meta.py` 가 있다
- **외부 CDN** — 자산은 벤더링해 이미지에 담는다(레포 정책)
- **`/v1/meta` 의 sha256·bytes 하드코딩** — 서빙할 바이트에서 계산한다
- **목 서버(`tools/mock_gateway.py`)에서 `app.*` 임포트** — 계약 테스트가
  `importlib` 로 standalone 로드한다. stdlib + starlette/uvicorn 만으로 떠야 한다
