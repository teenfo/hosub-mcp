# hosub-mcp

hosub 개인 서버(Ubuntu)를 **Claude(claude.ai / 모바일 앱)에서 대화로 모니터링·제어**하기 위한
원격 MCP 서버. Claude Custom Connector(Streamable HTTP + Bearer Token)로 연결하며,
Cloudflare Tunnel 로 공인 인터넷에 노출한다. 조회 전용 웹 대시보드를 함께 제공한다.

## 특징

- **표준 MCP SDK 기반** (`mcp` v1 FastMCP, Streamable HTTP)
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
   ├─ /mcp        도구 계층 (Bearer 인증)
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

## 대시보드 확장 (패널 추가)

대시보드는 패널 단위로 구성된다. 새 패널 추가:

1. `static/panels/<이름>.js` 에 `export default { id, title, refreshMs, render(bodyEl) }`
2. `static/panels/index.js` 의 `PANELS` 배열에 `import` 한 줄 추가

`static/panels/home.js` 가 "홈" 컨텐츠 자리(placeholder)이며, 이스터에그 훅은
`static/app.js` 하단(코나미 코드 → 파티 모드)에 있다. 서버 변경 없이 프론트엔드만으로
날씨·미디어·메모 등 홈 위젯을 붙일 수 있다.

## 배포

- **설치·연동 전체 절차**: [`docs/SETUP.md`](docs/SETUP.md) (로컬 Claude Code 기준 상세 가이드)
- **최초 설치**: `sudo bash deploy/bootstrap.sh` — 전용 유저·venv·의존성·.env(시크릿 자동 발급)·systemd 유닛을 한 번에 준비
- **자동 배포 (pull 기반, 권장)**: `hosub-mcp-update.timer` 가 5분마다 추적 브랜치(`HOSUB_MCP_BRANCH`, 기본 `main`)를 폴링 → 변경 시 `git pull` + `pip install` + 서비스 재시작. **코드를 머지하면 서버가 자동으로 최신화**된다(NAT 뒤 홈서버에 적합, SSH 개방 불필요).
  - 즉시 반영: `sudo -u hosub /opt/hosub-mcp/deploy/update.sh`
  - 로그: `journalctl -u hosub-mcp-update.service`
- **대안 (push 기반)**: `.github/workflows/deploy.yml` (GitHub Actions `appleboy/ssh-action`, 시크릿 `HOSUB_HOST`/`HOSUB_USER`/`HOSUB_SSH_KEY`). SSH 인바운드가 필요해 NAT 환경엔 pull 방식을 권장.

### 인터넷 노출 (앱/모바일에서 쓸 때만 필요)

집 LAN 안에서만 쓰면 노출 불필요(`http://192.168.0.3:8700/`). claude.ai·모바일 앱·Cowork 커넥터로 쓰려면 공인 HTTPS 가 필요하며, 방법 두 가지:
- **Cloudflare Tunnel** — Cloudflare 관리 도메인이 있을 때. 포트 개방 불필요.
- **iptime DDNS + Caddy** (도메인 구매 불필요) — 무료 DDNS(`kch83.iptime.org`) + 포트포워딩(80/443) + `deploy/Caddyfile` 자동 TLS. → `docs/SETUP.md` 부록 B.

## 보안 노트

- 이 서버는 대화로 **서버 전체를 제어**한다. Bearer 토큰 유출 = 서버 root 완전 장악.
- `confirm=true` 는 서버가 실제 사용자 동의를 검증할 수 없는 **advisory** 방식이다
  (단일 사용자 개인 서버 전제). 추후 승인 nonce 로 강화 가능.
- 전용 유저 실행(root 금지), 강한 토큰, Cloudflare Access 병행을 강하게 권고한다.
- 잡 상태는 인메모리라 재시작 시 소실된다 — 영구 기록은 감사 DB 가 담당한다.
- MCP SDK v2 GA 시 `FastMCP`→`MCPServer` 소규모 마이그레이션이 필요하다.
