# 커밋 · PR · 배포 · 검증 · 롤백

## 커밋 전

```bash
cd llm-gateway && ../.venv/bin/python -m pytest -q     # 231개
cd .. && .venv/bin/python -m pytest -q                  # 189개
```

`src/`·`static/` 을 함께 고쳤으면 **둘 다** 초록이어야 한다. JS 를 고쳤으면
`node --check` 도(ESM 이라 `.mjs` 로 복사해 검사).

새 테스트는 **일부러 깨 본다.** 통과만 보면 아무것도 안 보고 있을 수 있다.

## 브랜치 · PR

`feature/<이름>` 또는 `fix/<이름>` → PR → main. **main 직접 푸시 금지.**

스택으로 쌓았다면 스쿼시 머지가 해시를 바꿔 다음 PR 을 깨뜨린다
(`mergeable_state: dirty`). 머지 사이사이:

```bash
git fetch origin main
git rebase --onto origin/main <이전-PR-머지전-해시> <다음브랜치> --update-refs
git push --force-with-lease origin <다음브랜치>
```

리베이스 뒤 **두 스위트를 다시 돌린다.**

## 배포

`main` 에 머지되면 `hosub-mcp-update.timer` 가 5분마다 `git pull` + 대시보드·MCP
재시작을 한다. **게이트웨이는 의도적으로 자동 재빌드하지 않는다** — 잡 큐를 들고
있어 남의 배포에 끌려 재시작되면 실행 중이던 추론이 끊긴다.

### 1. 큐가 비었는지 본다

```
llm_status()   → lanes.interactive / lanes.batch 가 0/0 인가
```

잡은 SQLite 라 재시작에도 살아남지만 실행 중이던 건 재시도된다.

### 2. `config/` 만 고쳤다면 재빌드가 필요 없다

`roles.yaml`·`services.yaml`·`catalog.yaml` 은 바인드 마운트(`./config:/app/config:ro`)
라 `git pull` 만으로 반영된다(컨테이너 재시작은 필요할 수 있다).

### 3. 그 밖을 고쳤다면 재빌드

```
deploy_service(service_name="llm-gateway", confirm=true)
```

3단계를 다 한다: `git pull --ff-only` → `docker compose up -d --build` →
`gateway-mark-deployed.sh`(드리프트 마커).

> **`systemctl reload llm-gateway` 를 쓰지 않는다.** 재빌드는 하지만 `git pull` 도
> 마커 갱신도 안 해서, 자동 업데이트 타이머가 계속 드리프트 경고를 낸다.

`src/`·`static/` 을 함께 고쳤으면 `deploy_service(service_name="dash", confirm=true)` 도.

### 4. 드리프트 마커 확인

```bash
cd /opt/hosub-mcp
git rev-parse HEAD:llm-gateway   # 디스크
cat llm-gateway/.deployed-tree   # 배포됨
```

**같아야 한다.** 트리 해시라 `llm-gateway/` 아래 어떤 파일이든(문서 한 줄도)
바뀌면 달라진다.

### `.env` 를 건드려야 할 때

`llm-gateway/.env` 는 gitignore 다. append 만 하고 기존 줄을 건드리지 않는다.
값을 화면에 찍지 말고 해시로 대조한다:

```bash
sudo grep '^KEY=' /opt/hosub-mcp/llm-gateway/.env | cut -d= -f2- | sha256sum | cut -c1-8
```

## 검증 — 소비자 시점이 핵심

내부에서 200 이 나오는 것은 증명이 아니다. **공개 URL 을 소비자 토큰으로** 본다.

```bash
T=$(sudo grep '^LLMGW_TOKEN_ROXLOGY=' /opt/hosub-mcp/llm-gateway/.env | cut -d= -f2-)
U=https://hosub.duckdns.org/llm

# ⚠️ 가장 놓치기 쉬운 것 — servers[0] 에 /llm 이 있는가
curl -sH "Authorization: Bearer $T" $U/v1/openapi.json | jq -r '.servers[].url'

# 클라이언트 바이트 3중 일치
curl -sH "Authorization: Bearer $T" $U/v1/client/llmgw.py | sha256sum
sha256sum /opt/hosub-mcp/llm-gateway/client/llmgw.py
curl -sH "Authorization: Bearer $T" $U/v1/meta | jq -c '.client.files.python|{sha256,bytes,available}'

# 소비자 토큰에 관리 표면이 새는가 — [] 와 false 여야 한다
curl -sH "Authorization: Bearer $T" $U/v1/openapi.json \
  | jq '[.paths|keys[]|select(startswith("/v1/admin"))], has("x-admin-endpoints")'

# 관리 경로는 공개에서 404
curl -so /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $T" $U/v1/admin/roles

# 역할 enum 이 그 토큰 권한만 담는가
curl -sH "Authorization: Bearer $T" $U/v1/openapi.json \
  | jq -r '.components.schemas.GenerateRequest.properties.role.enum[]'
```

브라우저까지 봐야 하면 `https://hosub.duckdns.org/llm/v1/docs` 에 소비자 토큰을
넣고 오퍼레이션 목록에 `/v1/admin/*` 이 없는지 본다.

> 이 세션의 실측: 클라우드 세션에서는 `hosub.duckdns.org` 로의 outbound 가
> 프록시 정책에 막힐 수 있다. 그때는 위 명령을 **서버에서**(`run_command`) 돌린다.

## 롤백

- **엔드포인트 추가만 했다면 롤백 위험이 구조적으로 없다** — 기존 응답 불변
- `.env` 한 줄은 지우고 재기동하면 원복
- 코드는 이전 커밋으로 `git checkout` 후 `docker compose up -d --build`,
  그리고 `gateway-mark-deployed.sh` 재실행
- Caddyfile 을 안 건드렸으면 엣지 롤백은 없다

## 커밋 메시지

한국어. **무엇이 아니라 왜.** 특히 이 저장소에서 값진 것:

- 어떤 경계를 지켰는지 / 어떤 결정을 뒤집었는지(뒤집었으면 설계서에 철회 문단)
- 소비자에게 미치는 영향 — "기존 `/v1/*` 응답을 바꾸지 않았다" 는 한 줄이
  리뷰어의 가장 큰 질문에 미리 답한다
- 실측 근거를 날짜와 함께
