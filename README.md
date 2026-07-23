# hosub-mcp

hosub 개인 서버(Ubuntu)를 **Claude(claude.ai / 모바일 앱)에서 대화로 모니터링·제어**하기 위한
원격 MCP 서버. Claude Custom Connector(Streamable HTTP + Bearer Token)로 연결하며,
Cloudflare Tunnel 로 공인 인터넷에 노출한다. 조회 전용 웹 대시보드를 함께 제공한다.

## 특징

- **표준 MCP SDK 기반** (`mcp` v1 FastMCP, Streamable HTTP)
- **OAuth 2.1 인증** (claude.ai 커넥터용, 대시보드 비밀번호로 승인) + 정적 토큰 병행
- **서버 전체 제어**: 화이트리스트 도구 + 임의 셸 명령(`run_command`) + 파일 입출력
- **위험도 기반 승인 흐름**: Medium/High 도구는 `confirm=true` 없이는 실행되지 않음
- **백그라운드 잡**: 오래 걸리는 작업은 즉시 `job_id` 반환 + 상태 조회
- **SQLite 감사 로그**: 모든 도구 호출과 잡 종결을 기록
- **모니터링 대시보드**: 같은 프로세스에 마운트, 별도 비밀번호 로그인(조회 전용)

## 도구 (13종)

| 도구 | 위험도 | 설명 |
|---|---|---|
| `get_system_status` | Low | CPU/메모리/디스크/업타임/상위 프로세스 |
| `list_services` | Low | 등록 서비스의 systemd 상태 |
| `read_service_logs` | Low | 등록 서비스의 journald 로그 |
| `get_job_status` / `list_jobs` | Low | 백그라운드 잡 조회 |
| `read_file` / `list_directory` | Low | 파일 읽기 / 디렉터리 목록 |
| `restart_service` | Medium | 등록 서비스 재시작 |
| `run_backup` | Medium | 레지스트리 지정 백업 스크립트 실행 |
| `deploy_service` | High | 등록 서비스 배포(git pull+빌드+재시작) |
| `run_script` | High | 화이트리스트 스크립트만 실행 |
| `run_command` | High | **임의 셸 명령** (서버 전체 제어) |
| `write_file` | High | **임의 파일 쓰기** (서버 전체 제어) |

### 승인 흐름 (Medium/High)

```
사용자: "backup_db 스크립트 돌려줘"
  → Claude: run_script(script_name="daily_backup")   # confirm 없음
  → 서버: {"status":"approval_required", ...}          # 실행 안 됨
  → Claude: "daily_backup 을 실행할까요?"
사용자: "응, 실행해"
  → Claude: run_script(script_name="daily_backup", confirm=true)
  → 서버: {"status":"started", "job_id":"..."}         # 백그라운드 실행
  → Claude: get_job_status("...")                       # 결과 확인
```

## 아키텍처

```
[Claude.ai / 모바일 앱]
      ↓ Custom Connector (Bearer Token)
[Cloudflare Tunnel]  →  https://mcp.example.com
      ↓
[hosub MCP 서버 (uvicorn, 127.0.0.1:8700)]
   ├─ /mcp        도구 계층 (OAuth/정적 Bearer 인증)
   ├─ /.well-known·/authorize·/token·/register  OAuth 2.1 (대시보드 비번 승인)
   ├─ /  ·  /api  대시보드 (세션 인증, 조회 전용)
   ├─ 정책 계층 (위험도 + confirm 게이트)
   ├─ 화이트리스트 레지스트리 (config/registry.yaml)
   ├─ 잡 매니저 (백그라운드 실행)
   └─ 감사 로그 (SQLite)
```

## 로컬 개발

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest                       # 전체 테스트 (systemd 불필요, FakeRunner 사용)
```

로컬 실행:

```bash
export HOSUB_MCP_TOKEN=$(openssl rand -hex 32)
export HOSUB_DASH_PASSWORD=changeme
export HOSUB_SESSION_SECRET=$(openssl rand -hex 32)
.venv/bin/uvicorn src.asgi:app --host 127.0.0.1 --port 8700

# 스모크: 토큰 없이 → 401
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8700/mcp
# 대시보드: http://127.0.0.1:8700/  (비밀번호 로그인)
```

## 레지스트리 편집

`config/registry.yaml` 의 `services` / `scripts` 만 화이트리스트 도구가 다룰 수 있다.
**변경은 Git PR 리뷰를 거쳐서만** 반영한다(권한 변경 이력 감사 목적).
`run_command` / `write_file` 은 레지스트리와 무관하게 임의 대상을 다루므로, 방어는
`confirm` 게이트와 감사 로그에 의존한다.

## 대시보드 확장 (페이지 / 패널 추가)

대시보드는 사이드바로 전환하는 **멀티 페이지** 구조다. 현재 페이지: 대시보드(시스템/
서비스/잡/감사/홈)·데일리 브리핑·날씨·Docker.

**새 페이지 추가** (사이드바·라우팅 자동 생성):

1. `static/pages/<이름>.js` 에 `export default { id, title, icon, render(container, ctx) }`
   - 주기 갱신은 `ctx.addTimer(setInterval(...))` 로 등록(페이지 이동 시 자동 정리)
2. `static/pages/index.js` 의 `PAGES` 배열에 `import` 한 줄 추가

**페이지 안에 카드(패널) 추가**: 기본 "대시보드" 페이지는 `static/panels/*` 를
`mountPanels` 로 렌더한다. 새 패널은 `static/panels/<이름>.js`
(`{ id, title, icon, wide?, refreshMs, render(bodyEl) }`) + `static/pages/dashboard.js`
의 목록에 추가. 공용 헬퍼는 `static/app.js` 의 `el`/`fetchJSON`/`bar`/`badge`/`card`.

**서버 데이터가 필요하면** `src/dashboard.py` 에 `/api/<기능>` 엔드포인트(세션 인증)를
더하고 페이지에서 `fetchJSON("/api/<기능>")` 로 읽는다. 외부 API 는 브라우저가 아니라
**서버측에서** 호출해 자체완결(외부 요청 0)을 유지한다(날씨 페이지가 그 예).

이스터에그 훅(코나미 → 파티 모드)은 `static/app.js` 하단에 있다.

## 배포

- **설치·연동 전체 절차**: [`docs/SETUP.md`](docs/SETUP.md) (로컬 Claude Code 기준 상세 가이드)
- **최초 설치**: `sudo bash deploy/bootstrap.sh` — 전용 유저·venv·의존성·.env(시크릿 자동 발급)·systemd 유닛을 한 번에 준비
- **자동 배포 (pull 기반, 권장)**: `hosub-mcp-update.timer` 가 5분마다 추적 브랜치(`HOSUB_MCP_BRANCH`, 기본 `main`)를 폴링 → 변경 시 `git pull` + `pip install` + 서비스 재시작. **코드를 머지하면 서버가 자동으로 최신화**된다(NAT 뒤 홈서버에 적합, SSH 개방 불필요).
  - 즉시 반영: `sudo -u hosub /opt/hosub-mcp/deploy/update.sh`
  - 로그: `journalctl -u hosub-mcp-update.service`
> 참고: 과거 대안이던 GitHub Actions push 배포(`appleboy/ssh-action`)는 NAT 뒤 홈서버로 SSH 인바운드가 필요해 이 환경에 맞지 않아 제거했다. pull 방식(위)이 유일한 자동 배포 경로다. 굳이 push 배포를 원하면 22번 포트 개방(또는 cloudflared access) + 시크릿 설정이 필요하다.

### 인터넷 노출 (앱/모바일에서 쓸 때만 필요)

집 LAN 안에서만 쓰면 노출 불필요(`http://192.168.0.3:8700/`). claude.ai·모바일 앱·Cowork 커넥터로 쓰려면 공인 HTTPS 가 필요하며, 방법 두 가지:
- **Cloudflare Tunnel** — Cloudflare 관리 도메인이 있을 때. 포트 개방 불필요.
- **DuckDNS + Caddy** (도메인 구매 불필요) — 무료 DuckDNS 서브도메인 + 포트포워딩(80/443) + `deploy/Caddyfile` 자동 TLS. iptime DDNS(`*.iptime.org`)는 CAA 정책으로 인증서 발급이 막혀 있어 DuckDNS를 쓴다. → `docs/SETUP.md` 부록 B.

## 브랜치 구조 / 협업 규칙

이 저장소는 클라우드 세션(개발)·로컬 세션(서버 실행)·자동 배포가 얽혀 있어, 브랜치
용도를 접두어로 구분한다. 접두어 뒤 이름은 작업 헤딩에서 딴다.

| 브랜치 | 용도 | 머지 여부 |
|---|---|---|
| `main` | **배포 대상.** 서버의 `hosub-mcp-update.timer` 가 5분마다 pull → 자동 반영 | — |
| `feature/<이름>` | 실제 기능 구현 | PR → main 머지 |
| `fix/<이름>` | 버그·설정 수정 | PR → main 머지 |
| `dev-request/<이름>` | **개발(코드) 요청서** — 다른 Claude Code 세션에 넘길 작업 명세(문서만) | 보통 미머지(핸드오프) |
| `local-request/<이름>` | **로컬 서버 실행/기록 문서** — LAN 안 로컬 세션이 서버에 수행할 런북·작업 내역 | 보통 미머지(핸드오프) |

**요청서(`*-request/`) 규칙**
- 접두어는 영문(`dev-request` / `local-request`), 이름은 헤딩 기반.
- 요청 문서는 `docs/requests/` 아래에 둔다(예: `docs/requests/dashboard-bootstrap5-admin.md`).
- 요청서 브랜치는 코드 없이 문서만 담아 핸드오프한다. 실제 구현은 받는 세션이
  `feature/` 브랜치로 진행 후 PR.

**워크플로우 예시**
```
클라우드 세션: dev-request/foo (요청서 작성)  ──넘김──▶  로컬/다른 세션
                                                        └─ feature/foo 구현 → PR → main
main 머지 ──5분──▶ 서버 자동 배포(pull)
로컬 서버 실행 절차·내역: local-request/<이름> (docs/requests, docs/work-log 등)
```

- 최초 배포 런북: `docs/requests/local-server-deployment.md`
- 배포 반영 체크리스트/작업 내역: `docs/work-log.md`

> 브랜치 삭제는 환경에 따라 막힐 수 있다. 머지 끝난 `feature/*`·`fix/*` 와 핸드오프가
> 끝난 `*-request/*` 브랜치는 로컬 PC(`git push origin --delete <브랜치>`)나 GitHub
> Branches 페이지에서 정리한다.

## 보안 노트

- 이 서버는 대화로 **서버 전체를 제어**한다. Bearer 토큰 유출 = 서버 root 완전 장악.
- `confirm=true` 는 서버가 실제 사용자 동의를 검증할 수 없는 **advisory** 방식이다
  (단일 사용자 개인 서버 전제). 추후 승인 nonce 로 강화 가능.
- 전용 유저 실행(root 금지), 강한 토큰, Cloudflare Access 병행을 강하게 권고한다.
- 잡 상태는 인메모리라 재시작 시 소실된다 — 영구 기록은 감사 DB 가 담당한다.
- MCP SDK v2 GA 시 `FastMCP`→`MCPServer` 소규모 마이그레이션이 필요하다.
