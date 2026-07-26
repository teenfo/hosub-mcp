# 개발 요청서 — TNM 임베딩을 공유 LLM 게이트웨이 경유로 전환

> 브랜치: `dev-request/tnm-embedding-via-gateway` (문서만 · 핸드오프)
> 구현은 받는 세션이 `feature/tnm-embedding-via-gateway` 로 진행 후 PR.

## 1. 배경

공유 LLM 게이트웨이(`llm-gateway/`, `127.0.0.1:8603`)에 **임베딩 API 가 추가됐다**(PR #110).

직전 작업(#108)에서 TNM 분류는 게이트웨이로 옮겼지만, 임베딩은 "게이트웨이가 아직
미지원"이라 Mac Ollama 직접 호출로 남겨뒀다. 그 전제가 해소됐으니 마저 옮긴다.

`tnm/app/ollama.py` 에 전환 지점이 이미 주석으로 표시돼 있다:

```
# ---------------- 임베딩 (Ollama 직접 — 게이트웨이 미지원 구간) ----------------
```

옮기면 얻는 것: 사용량 집계에 TNM 임베딩이 잡히고, 모델 정의가 `roles.yaml` 한 곳으로
통일되며(모델 교체 시 TNM 코드 무변경), 배치 호출로 호출 수가 1/N 로 준다.

## 2. 게이트웨이 임베딩 API

```
POST /v1/embed
Authorization: Bearer $LLMGW_TOKEN
{"role": "embed", "input": "한 건" | ["여러", "건"]}

200 → {"status":"ok", "model":"bge-m3", "embeddings":[[...]],
       "count":2, "dimensions":1024, "duration_ms":42}
503 → 재시도 가치 있음 (맥 재부팅·연결 실패 등). {"retryable": true}
502 → 모델 문제 등 재시도 무의미
400 → wrong_role_kind (생성용 역할을 넘긴 경우)
403 → 그 역할 권한 없음
413 → batch_too_large(256 초과) 또는 input_too_long(총 200k자 초과)
```

최신 계약은 게이트웨이가 직접 서빙한다 — 저장소를 안 봐도 된다:

```bash
curl -H "Authorization: Bearer $LLMGW_TOKEN" $LLMGW_URL/v1/integration   # 5-1절
```

**중요한 성질**: `/v1/embed` 는 게이트웨이에서 **유일하게 잡 큐를 타지 않는다.**
생성 잡 뒤에서 기다리지 않고 즉시 처리된다(3분짜리 32b 생성이 돌아도 무관). 대신
인증·역할 제한·레이트리밋·사용량 집계는 생성과 동일하게 적용된다.

## 3. 이미 갖춰진 것 (서버에서 확인 완료 — 손댈 필요 없음)

- `tnm/.env` 에 `LLMGW_URL`·`LLMGW_TOKEN` 설정됨. 게이트웨이의 `LLMGW_TOKEN_TNM` 과 **값 일치 확인**
- 게이트웨이 `services.yaml` 의 tnm `allow_roles` 에 `"embed"` 포함됨
- 맥에 `bge-m3:latest`(1.16GB, 1024차원) 설치됨. 게이트웨이에서 `model_available: true`
- `tnm` 서비스 active

## 4. 해야 할 일

### 4.1 `tnm/app/llmgw.py` 에 `embed()` 추가

이 파일은 게이트웨이의 `llm-gateway/client/llmgw.py` 복사본인데 **#110 이전 버전이라
`embed()` 가 없다.** 원본에서 가져오거나 같은 시그니처로 추가한다:

```python
async def embed(self, texts: str | list[str], *,
                role: str = "embed") -> list[list[float]]:
    out = await self._call("POST", "/v1/embed",
                           json={"role": role, "input": texts}, timeout=120)
    return out["embeddings"]
```

`_raise_for_status()` 가 이미 상태코드를 `AuthError`/`RoleError`/`GatewayError` 로
분류하므로 추가 처리는 불필요하다.

### 4.2 `tnm/app/ollama.py` 의 `embed()` 를 게이트웨이 경유로 교체

현재는 `bases()` 를 순회하며 구형 `/api/embeddings`(1건씩)를 호출한다.
**배치를 받는 형태로 바꾼다** — 게이트웨이의 `/api/embed` 는 배열을 한 번에 처리한다.

```python
async def embed_batch(texts: list[str]) -> list[list[float]]:
    """게이트웨이 경유 임베딩. 실패 → OllamaUnavailable (배치 전체 보류)."""
```

기존 단건 `embed(text)` 는 호출부가 하나뿐이니 배치 시그니처로 바꾸는 편이 낫다.
유지해야 한다면 `embed_batch([text])[0]` 로 감싼다.

**에러 매핑**: `GatewayError`(연결 실패·503·429) → `OllamaUnavailable`.
기존 의미("항목을 버리지 말고 보류")가 그대로 맞다.

### 4.3 직접 호출 경로 정리

`bases()`, `_http()`, `OLLAMA_URL`/`OLLAMA_FALLBACK_URL` 참조가 임베딩 전환 후
어디에 남는지 확인하고 **제거한다.** 게이트웨이의 존재 이유가 "설정의 단일 소스"인데
쓰이지 않는 두 번째 경로를 남기면 그 이유가 약해진다.

- `reachable()` 은 게이트웨이만 확인하도록 단순화
- `config.yaml` 의 `llm.embed_model` 은 무의미해진다(모델은 `roles.yaml` 소유)
  → `embed_role: "embed"` 로 대체
- `primary_model`/`fallback_model` 도 이미 "직접 호출 폴백용 참고값"으로만 남아 있으니
  같이 정리 대상인지 판단

> 참고: hosub 본체는 같은 전환에서 `src/llm.py` 를 **통째로 삭제**했다(#104).
> 직접 호출 폴백은 의도적으로 만들지 않았다 — 기본값 off 인 경로는 정작 장애 순간에
> 동작을 보장할 수 없고, 게이트웨이가 죽어도 MCP `run_command` 로 맥에 curl 을 바로
> 쏠 수 있어 탈출구가 이미 있기 때문이다. 근거는 설계서
> [`llm-gateway-service.md`](llm-gateway-service.md) 11.1 절. 같은 판단을 권한다.

### 4.4 `EmbedWorker.run_batch()` 를 진짜 배치로

`tnm/app/pipeline/workers.py` 의 현재 구현은 `pending_embeds(limit)` 로 N건을 가져와
**for 루프로 N번 호출**한다. 게이트웨이는 한 번에 최대 256건을 받으므로
**한 번의 호출로 바꾼다.**

- 텍스트 구성 로직(`title + "\n" + norm_body`, `[:2000]` 절단)은 그대로
- 실패 시 배치 전체 보류 + `mark_embed_retry` — 기존 동작 유지
  (배치 호출이라 부분 실패가 없어져 오히려 단순해진다)
- 성공 시 `save_embedding` 을 순서대로 매핑 — **응답 `embeddings` 는 입력 순서와 같다**
- `limit=8` 이 배치 기준으로도 적정한지 판단. 호출 수가 1/8 로 줄었으니 키울 여지가
  있으나 **측정 없이 키우지 말 것** — 한 번의 실패로 보류되는 항목만 늘어난다

### 4.5 테스트

`tnm/tests/test_classify.py` 가 #108 에서 게이트웨이 모킹 패턴을 잡아뒀으니 그대로 따른다.

- 배치 호출이 **1번**만 나가는지(N번이 아니라) — 이번 변경의 핵심 이득이다
- **응답 벡터가 입력 순서대로 올바른 `item_id` 에 저장되는지**
- `GatewayError` → `OllamaUnavailable` → 배치 전체 보류 + 재시도 스케줄
- 게이트웨이 미설정/다운 시 예외로 죽지 않고 보류되는지

## 5. 주의

- **DB 마이그레이션 불필요.** 같은 `bge-m3`, 같은 1024차원이라 pgvector 컬럼 그대로다.
  기존 저장분과 신규 생성분이 같은 공간에 있다.
- **가장 위험한 실패 모드는 순서 어긋남이다.** 배치 응답과 `item_id` 매핑이 틀리면
  잘못된 임베딩이 **에러 없이** 저장된다. 유사도 검색이 미묘하게 이상해질 뿐이라
  발견이 늦다. 4.5 의 순서 테스트를 반드시 넣을 것.
- **`tnm/app/llmgw.py` 는 복사본이라 원본과 어긋난다.** 이번에 `embed()` 를 추가하면서,
  앞으로도 원본(`llm-gateway/client/llmgw.py`)과 동기화가 필요하다는 점을 인지할 것.
  같은 레포 안이므로 복사 대신 참조하는 구조로 바꾸는 것도 선택지다(별도 판단).
- **게이트웨이 코드는 건드리지 말 것.** 필요한 변경이 있으면 요청할 것.

## 6. 검증

로컬:

```bash
cd tnm && ../.venv/bin/python -m pytest -q
```

실서버(배포 후):

```bash
# 임베딩이 게이트웨이를 거치는지 — usage 에 tnm 이 잡혀야 한다
TOK=$(grep '^LLMGW_TOKEN_HOSUB=' /opt/hosub-mcp/llm-gateway/.env | cut -d= -f2)
curl -s -H "Authorization: Bearer $TOK" localhost:8603/v1/status \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["usage"])'

# 대기 중인 임베딩이 실제로 소진되는지
journalctl -u tnm -n 50 --no-pager | grep -i embed
```

> **`llm-gateway` 는 재기동하지 말 것.** 잡 큐를 들고 있어 다른 서비스와 수명주기가
> 분리돼 있다(설계 의도). TNM 배포는 `deploy_service("tnm")` 또는
> `systemctl restart tnm` 으로 충분하다. 경로별 영향은 `docs/SETUP.md` 8-1 절.

## 7. 범위 밖

- 게이트웨이 코드 변경
- 임베딩 모델 교체(필요하면 `roles.yaml` 에 별도 PR)
- 유사도 검색·중복 제거 로직 자체의 개선
