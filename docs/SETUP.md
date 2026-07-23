# hosub MCP 서버 설치·연동 가이드 (상세)

이 문서는 **로컬 Claude Code(집 LAN 안 PC)에서 서버(192.168.0.3 / kch83.iptime.org)에
설치**하는 것을 전제로 한, 처음부터 끝까지의 배포 가이드다. 명령은 대부분
**서버에서 실행**한다(로컬 Claude Code가 SSH로 서버에 붙어 대신 실행하거나,
서버 콘솔에서 직접 실행).

> 이 클라우드 세션(claude.ai/code 웹)은 집 LAN 사설망에 닿지 못한다. 실제 설치는
> 반드시 로컬 Claude Code 또는 서버 콘솔에서 수행한다.

---

## 0. 사전 준비 (체크리스트)

- [ ] 서버: Ubuntu, `python3` 3.11+ (`python3 --version`), `git`, `openssl`
- [ ] 서버에 SSH 접속 가능 (`openssh-server` 설치·기동 상태)
- [ ] GitHub 저장소 `teenfo/hosub-mcp` 에 대한 읽기 권한 (private → 아래 3번 deploy key)
- [ ] Cloudflare 계정 + 도메인 + `cloudflared` (기존 Tunnel 재사용)
- [ ] 서버가 인터넷으로 나갈 수 있음 (`apt`, `git fetch`, `pip` 가능)

빠진 패키지 설치:

```bash
sudo apt update
sudo apt install -y python3-venv git openssl curl
```

---

## 1. 코드 배치 (clone)

```bash
sudo mkdir -p /opt/hosub-mcp
sudo chown "$USER":"$USER" /opt/hosub-mcp
git clone https://github.com/teenfo/hosub-mcp.git /opt/hosub-mcp
cd /opt/hosub-mcp
git checkout main        # PR 머지 후 main 사용. 아직 머지 전이면 개발 브랜치명 사용
```

> **private 저장소라 clone 시 인증이 필요**하다. 아래 **3번(deploy key)**을 먼저 만들고
> SSH URL(`git@github.com:teenfo/hosub-mcp.git`)로 clone 하면 자동 업데이트까지 매끄럽다.

---

## 2. 부트스트랩 (자동 설치 스크립트)

전용 유저·venv·의존성·.env·systemd 유닛을 한 번에 준비한다:

```bash
cd /opt/hosub-mcp
sudo bash deploy/bootstrap.sh
```

이 스크립트가 하는 일:
- 전용 유저 `hosub` 생성 (없으면)
- `/opt/hosub-mcp` 소유권을 `hosub` 로 변경
- `.venv` 생성 + `requirements.txt` 설치
- `.env` 생성 + **토큰·세션 시크릿 자동 발급** (`HOSUB_DASH_PASSWORD` 는 비워둠)
- `hosub-mcp.service`, `hosub-mcp-update.service/.timer` 를 `/etc/systemd/system` 에 설치

> 수동으로 하고 싶으면 아래 "부록 A"를 참고.

---

## 3. private 저장소 자동 업데이트용 Deploy Key

자동 업데이트(5분마다 `git pull`)가 private 저장소에서 동작하려면, `hosub` 유저가
읽기 권한으로 GitHub 에 접근할 수 있어야 한다. **읽기 전용 deploy key**를 쓴다.

```bash
# hosub 유저용 SSH 키 생성 (암호 없이)
sudo -u hosub ssh-keygen -t ed25519 -f /opt/hosub-mcp/.ssh/id_ed25519 -N "" -C "hosub-mcp-deploy"
sudo -u hosub cat /opt/hosub-mcp/.ssh/id_ed25519.pub
```

출력된 공개키를 GitHub 에 등록:
- `teenfo/hosub-mcp` → **Settings → Deploy keys → Add deploy key**
- Title: `hosub-mcp deploy (read-only)`, **Allow write access 체크 해제**

remote 를 SSH URL 로 전환 + known_hosts 등록:

```bash
sudo -u hosub git -C /opt/hosub-mcp remote set-url origin git@github.com:teenfo/hosub-mcp.git
sudo -u hosub bash -c 'ssh-keyscan github.com >> /opt/hosub-mcp/.ssh/known_hosts 2>/dev/null'
# 연결 확인 (첫 줄에 "Hi teenfo/hosub-mcp! You've successfully authenticated" 나오면 성공)
sudo -u hosub ssh -i /opt/hosub-mcp/.ssh/id_ed25519 -T git@github.com || true
sudo -u hosub git -C /opt/hosub-mcp fetch origin
```

> HTTPS + Personal Access Token 방식도 가능하지만, deploy key 가 권한 범위가 좁고
> 만료 관리가 편하다.

---

## 4. sudo 권한 (⚠️ 서버 전체 제어)

이 서버는 대화로 **서버 전체를 제어**하도록 설계됐다(`run_command`, `write_file`,
임의 패키지 설치 등). 이를 위해 전용 유저에 전체 sudo 를 부여한다:

```bash
echo 'hosub ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/hosub-mcp
sudo chmod 440 /etc/sudoers.d/hosub-mcp
sudo visudo -c        # 문법 검증
```

> **리스크 인지 필수.** Bearer 토큰 유출 = 서버 root 완전 장악. 방어선은
> (a) 강한 토큰 (b) 도구의 `confirm` 게이트 (c) 감사 로그뿐이다. 8번 보안
> 체크리스트를 반드시 지킬 것.

**화이트리스트만 원한다면 (더 안전, 서버 전체 제어 포기):**

```bash
# /etc/sudoers.d/hosub-mcp (제한형) — run_command 로 하는 root 작업은 실패함
hosub ALL=(root) NOPASSWD: /usr/bin/systemctl restart hosub-mcp, /usr/bin/systemctl restart <서비스>.service
```

**journald 로그 읽기 권한:**

```bash
sudo usermod -aG systemd-journal hosub
```

---

## 5. 환경변수(.env) 마무리

부트스트랩이 토큰/시크릿은 채웠다. **대시보드 비밀번호만** 채우면 된다:

```bash
sudo -u hosub vi /opt/hosub-mcp/.env      # HOSUB_DASH_PASSWORD 입력
```

| 변수 | 설명 |
|---|---|
| `HOSUB_MCP_TOKEN` | Claude 커넥터 Bearer 토큰 (자동 발급, 32자+) |
| `HOSUB_DASH_PASSWORD` | 대시보드 웹 로그인 비밀번호 **(직접 입력)** |
| `HOSUB_SESSION_SECRET` | 세션 쿠키 서명 키 (자동 발급) |
| `HOSUB_MCP_BRANCH` | 자동 업데이트가 추적할 브랜치 (기본 `main`) |
| `HOSUB_ALLOWED_HOSTS` | (선택) 허용 Host. 예: `mcp.example.com` |
| `HOSUB_MCP_STRICT` | `true` 면 스크립트 경로 존재/실행권한 검증 |

> `.env` 는 절대 커밋하지 않는다(`.gitignore` 포함). 권한은 `600`, 소유자 `hosub`.

---

## 6. 서비스 + 자동 업데이트 타이머 기동

```bash
# 본 서비스
sudo systemctl enable --now hosub-mcp
# 자동 업데이트 (5분마다 git pull → 변경 시 재시작)
sudo systemctl enable --now hosub-mcp-update.timer

# 상태 확인
systemctl status hosub-mcp --no-pager
systemctl list-timers hosub-mcp-update.timer --no-pager
```

**로컬 스모크 (401 이면 정상 = 인증이 걸려 있음):**

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8700/mcp
# 대시보드도 로컬에서 확인: http://127.0.0.1:8700/  (LAN PC 브라우저는 http://192.168.0.3:8700/)
```

---

## 7. 인터넷 노출 + 커넥터 등록

> **노출이 꼭 필요한 경우는 하나뿐:** claude.ai / 모바일 앱 / Cowork 에서 커넥터로
> 쓰려면 서버가 공인 HTTPS 로 도달 가능해야 한다. **집 LAN 안에서만**(대시보드,
> 로컬 Claude Code) 쓸 거면 이 절은 건너뛰고 `http://192.168.0.3:8700/` 로 바로 쓴다.
>
> 노출 방법 두 가지 — 상황에 맞게 택1:
> - **7-A. Cloudflare Tunnel** — Cloudflare 관리 도메인이 있을 때. 포트 개방 불필요.
> - **부록 B. DuckDNS + Caddy** — **도메인 구매 없이** 무료 DuckDNS 서브도메인으로
>   노출. 포트포워딩 + Caddy 자동 TLS. (iptime DDNS 는 CAA 로 인증서 발급 불가 →
>   DuckDNS 사용) → 이 문서 맨 아래 부록 B 참고.

### 7-A. Cloudflare Tunnel

#### 7.1 터널 ingress

기존 tunnel 설정(`~/.cloudflared/config.yml`)에 추가:

```yaml
ingress:
  - hostname: mcp.example.com
    service: http://localhost:8700
  # ... 기존 규칙 ...
  - service: http_status:404
```

```bash
cloudflared tunnel route dns <tunnel-name> mcp.example.com
sudo systemctl restart cloudflared
# 외부에서 확인 (401 이면 정상)
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://mcp.example.com/mcp
```

`.env` 의 `HOSUB_ALLOWED_HOSTS=mcp.example.com` 을 채우고 `sudo systemctl restart hosub-mcp`
하면 DNS 리바인딩 보호까지 켜진다.

#### 7.2 Claude Custom Connector (OAuth 2.1)

claude.ai 커넥터 UI에는 Bearer 헤더 입력란이 없어, **표준 OAuth 2.1 흐름**으로
연결한다. 서버가 필요한 엔드포인트(`.well-known/*`, `/register`, `/authorize`,
`/token`)를 제공하며, 승인 게이트는 **대시보드 비밀번호**다.

**먼저 `.env` 에 공개 URL 을 설정**해야 OAuth 메타데이터가 올바른 주소로 발급된다:

```bash
sudo -u hosub sed -i 's#^HOSUB_PUBLIC_URL=.*#HOSUB_PUBLIC_URL=https://mcp.example.com#' /opt/hosub-mcp/.env
sudo systemctl restart hosub-mcp
# 메타데이터 확인 (issuer 가 공개 URL 이어야 함)
curl -s https://mcp.example.com/.well-known/oauth-authorization-server | python3 -m json.tool
```

연결:
1. Claude.ai → Settings → Connectors → **Add custom connector**
2. **URL**: `https://mcp.example.com/mcp` (헤더 입력 불필요)
3. **연결(Connect)** 클릭 → 브라우저 창에서 **대시보드 비밀번호** 입력 → 승인
4. 대화창 **+ → Connectors** 에서 활성화
5. Low 도구부터 테스트: "hosub 시스템 상태 알려줘" → `get_system_status`

> **동작 순서**: 커넥터가 `POST /mcp` → 401(`WWW-Authenticate`) 을 받고 `.well-known`
> 메타데이터를 조회 → `/register` 로 동적 등록 → `/authorize` 승인 페이지(비밀번호) →
> `/token` 으로 PKCE 코드 교환 → 발급된 액세스 토큰으로 접속한다.
>
> 정적 `HOSUB_MCP_TOKEN` 은 curl 테스트·비상용으로 병행 유지된다:
> `curl -H "Authorization: Bearer <토큰>" ...`

#### 7.3 대시보드 접속

`https://mcp.example.com/` → `HOSUB_DASH_PASSWORD` 로그인. **Cloudflare Access(이메일
OTP/SSO) 병행을 권장**한다.

---

## 8. 자동 배포 흐름 (추후 변경 자동 반영)

이제 **코드를 고칠 때 서버에 손대지 않아도 된다:**

```
로컬/PR 에서 코드 수정
   → main 브랜치에 머지(push)
   → 서버의 hosub-mcp-update.timer 가 5분 내 감지
   → git pull (ff-only) + pip install + systemctl restart hosub-mcp
   → 새 버전 자동 반영
```

- **추적 브랜치 변경**: `.env` 의 `HOSUB_MCP_BRANCH` 수정 후 타이머 다음 주기에 반영.
- **즉시 반영(수동 트리거)**:
  ```bash
  sudo -u hosub HOSUB_MCP_BRANCH=main /opt/hosub-mcp/deploy/update.sh
  ```
- **업데이트 로그 확인**:
  ```bash
  journalctl -u hosub-mcp-update.service -n 50 --no-pager
  ```
- **롤백**: 문제가 생기면 원하는 커밋으로 되돌리고 타이머를 잠시 끈다.
  ```bash
  sudo systemctl stop hosub-mcp-update.timer
  sudo -u hosub git -C /opt/hosub-mcp reset --hard <이전-커밋>
  sudo systemctl restart hosub-mcp
  ```

> **주의:** 자동 업데이트는 추적 브랜치에 올라온 코드를 **자동으로 실행**한다.
> 즉 그 브랜치에 push 할 수 있는 사람은 서버에서 코드를 돌릴 수 있다. 브랜치
> 보호 규칙(main 직접 push 금지, PR 리뷰 필수)을 걸어 두는 것을 권장한다.

### (대안) GitHub Actions push 배포

NAT 뒤 홈서버라 GitHub Actions 가 SSH 로 들어오려면 22번 포트 개방(또는 cloudflared
access)이 필요하다. 위 pull 방식이 더 안전하므로 기본 권장이지만, 굳이 push 방식을
쓰려면 `.github/workflows/deploy.yml` + 시크릿 `HOSUB_HOST`/`HOSUB_USER`/`HOSUB_SSH_KEY`
를 설정한다.

---

## 9. 서비스를 다시 올릴 때 (레지스트리 갱신)

서버를 초기화해 레지스트리는 비어 있다. Ollama·BCL Portal 등을 다시 설치한 뒤,
`config/registry.yaml` 에 항목을 추가하고 **PR 로 머지**하면 자동 배포로 반영된다:

```yaml
services:
  ollama:
    unit: ollama.service
    description: "Ollama LLM 런타임"
scripts:
  daily_backup:
    path: /opt/scripts/backup_db.sh
    description: "DB 백업"
    timeout_seconds: 1800
backup_script: daily_backup
```

> `run_command`/`write_file` 로 임의 설치·설정은 레지스트리 없이도 가능하다.
> 레지스트리는 `restart_service`/`deploy_service`/`run_script` 같은 "안전한 전용
> 도구"용 화이트리스트일 뿐이다.

---

## 10. 트러블슈팅

| 증상 | 원인/해결 |
|---|---|
| `systemctl status hosub-mcp` → `HOSUB_MCP_TOKEN 미설정` 에러 | `.env` 의 토큰이 비었거나 32자 미만. 재발급 후 restart |
| 로컬 curl 이 401 아님(예: 000/503) | 서비스 미기동. `journalctl -u hosub-mcp -n 50` 확인 |
| 대시보드 로그인 실패 | `HOSUB_DASH_PASSWORD` 미설정. `.env` 채우고 restart |
| 자동 업데이트 안 됨 | `journalctl -u hosub-mcp-update.service` 확인. deploy key 인증 실패(3번) 또는 sudo 권한(4번) 문제 |
| `git pull` 인증 실패 | deploy key 미등록 또는 remote 가 HTTPS. 3번 재확인 |
| 외부 https 접속 안 됨 (터널) | cloudflared ingress/DNS. `cloudflared tunnel info <name>` |
| 외부 https 접속 안 됨 (Caddy) | 포트포워딩(80/443)·DDNS·인증서. 부록 B 확인 |
| Caddy 인증서 발급 실패 | 80/443 포워딩 안 됨, 또는 LE 레이트리밋. `journalctl -u caddy` 확인 |
| `restart_service` 실패 | sudoers 에 systemctl 권한 없음(4번) |

---

## 11. 보안 체크리스트

- [ ] `HOSUB_MCP_TOKEN` 32자 이상 무작위 (`openssl rand -hex 32`), 유출 시 즉시 재발급
- [ ] `.env` 권한 `600`, 소유자 `hosub`
- [ ] 프로세스는 `hosub` 유저로 실행 (root 금지 — bootstrap 이 강제)
- [ ] Cloudflare Access 로 MCP/대시보드 엔드포인트 추가 보호
- [ ] deploy key 는 **읽기 전용** (write access 해제)
- [ ] main 브랜치 보호 규칙(PR 리뷰 필수) — 자동 배포 대상이므로
- [ ] 감사 DB(`data/audit.db`) 주기 확인 — 임의 명령 실행 이력 추적
- [ ] 서버 로그인 비밀번호를 강한 값으로 변경 + SSH 키 인증 권장

---

## 부록 A. 부트스트랩 없이 수동 설치

```bash
sudo useradd -r -m -d /opt/hosub-mcp -s /bin/bash hosub
sudo chown -R hosub:hosub /opt/hosub-mcp
sudo -u hosub bash -c 'cd /opt/hosub-mcp && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt'
sudo -u hosub cp /opt/hosub-mcp/.env.example /opt/hosub-mcp/.env
sudo -u hosub vi /opt/hosub-mcp/.env     # 토큰(openssl rand -hex 32), 대시보드 비번, 세션 시크릿
sudo install -m 644 /opt/hosub-mcp/deploy/hosub-mcp.service /etc/systemd/system/
sudo install -m 644 /opt/hosub-mcp/deploy/hosub-mcp-update.service /etc/systemd/system/
sudo install -m 644 /opt/hosub-mcp/deploy/hosub-mcp-update.timer /etc/systemd/system/
sudo systemctl daemon-reload
```
그다음 4·6·7번을 진행.

---

## 부록 B. DuckDNS + Caddy 노출 (도메인 구매 불필요)

Cloudflare 관리 도메인 없이, **무료 DuckDNS 서브도메인**으로 앱 커넥터용 HTTPS 를
만든다. Caddy 가 Let's Encrypt/ZeroSSL 인증서를 자동 발급·갱신한다.

> ### ⚠️ iptime DDNS(`*.iptime.org`) 는 인증서 발급이 불가능하다
> iptime 이 도메인 전체에 **CAA 레코드(`CAA 0 issue ";"`)** 를 걸어 **모든 CA 의 인증서
> 발급을 차단**한다(사용자가 못 바꿈). 그래서 `kch83.iptime.org` 로는 Let's Encrypt/
> ZeroSSL 어느 쪽도 발급되지 않고, 자체서명 인증서는 Claude 커넥터가 거부한다.
> DDNS(이름→IP) 자체는 정상이지만 **HTTPS 를 못 씌운다.** → 해결: 같은 집 IP를
> 가리키는 **DuckDNS 서브도메인**을 추가로 등록한다(iptime DDNS 는 그대로 둬도 됨).
>
> ```
> kch83.iptime.org  ─┐
>                     ├─→ 집 공인 IP → 공유기 → 서버(192.168.0.3)
> hosub.duckdns.org ─┘        (DuckDNS 는 CAA 제약 없음 → 인증서 발급 가능 ✅)
> ```

### B.1 DuckDNS 서브도메인 등록

1. <https://www.duckdns.org> 접속 → Google/GitHub 등으로 로그인
2. 서브도메인 하나 생성 (예: `hosub` → `hosub.duckdns.org`)
3. 페이지 상단의 **token** 값 확인 (비밀값)

`.env` 에 등록 (토큰은 서버에서 직접 입력, 커밋 금지):

```bash
sudo -u hosub sed -i 's/^DUCKDNS_DOMAIN=.*/DUCKDNS_DOMAIN=hosub/' /opt/hosub-mcp/.env
sudo -u hosub vi /opt/hosub-mcp/.env      # DUCKDNS_TOKEN=<본인 토큰> 입력
```

IP 자동 갱신 타이머 설치·기동:

```bash
sudo install -m 644 /opt/hosub-mcp/deploy/duckdns-update.service /etc/systemd/system/
sudo install -m 644 /opt/hosub-mcp/deploy/duckdns-update.timer /etc/systemd/system/
sudo chmod +x /opt/hosub-mcp/deploy/duckdns-update.sh
sudo systemctl daemon-reload
sudo systemctl enable --now duckdns-update.timer
# 즉시 1회 갱신 + 확인 (OK 나오면 성공)
sudo -u hosub bash -c 'set -a; . /opt/hosub-mcp/.env; set +a; /opt/hosub-mcp/deploy/duckdns-update.sh'
getent hosts hosub.duckdns.org      # 집 공인 IP로 해석되는지
```

### B.2 iptime 공유기 포트포워딩

iptime 관리자(`192.168.0.1`) → 고급설정 → NAT/라우터 → 포트포워드 설정:

| 외부 포트 | 내부 IP | 내부 포트 | 용도 |
|---|---|---|---|
| 80 | 192.168.0.3 | 80 | 인증서 발급(HTTP-01 챌린지) |
| 443 | 192.168.0.3 | 443 | HTTPS 서비스 |

### B.3 Caddy 설치 + 설정

```bash
# Caddy 설치 (공식 저장소)
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy

# 설정 배치 (Caddyfile 안의 hosub.duckdns.org 를 본인 서브도메인으로 교체)
sudo mkdir -p /var/log/caddy
sudo cp /opt/hosub-mcp/deploy/Caddyfile /etc/caddy/Caddyfile
sudo sed -i 's/hosub\.duckdns\.org/<본인서브도메인>.duckdns.org/' /etc/caddy/Caddyfile   # 필요 시
sudo systemctl restart caddy

# 인증서 발급 로그 확인 (certificate obtained 나오면 성공)
journalctl -u caddy -n 40 --no-pager
```

### B.4 확인 + 공개 URL·Host 설정

```bash
# 외부 도달 + 인증 (401 이면 정상: TLS OK + 인증 요구)
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://hosub.duckdns.org/mcp

# .env 에 공개 URL(OAuth 메타데이터용) + Host 검증 설정
sudo -u hosub sed -i 's#^HOSUB_PUBLIC_URL=.*#HOSUB_PUBLIC_URL=https://hosub.duckdns.org#' /opt/hosub-mcp/.env
sudo -u hosub sed -i 's/^HOSUB_ALLOWED_HOSTS=.*/HOSUB_ALLOWED_HOSTS=hosub.duckdns.org/' /opt/hosub-mcp/.env
sudo systemctl restart hosub-mcp

# OAuth 메타데이터 확인 (issuer 가 https://hosub.duckdns.org 이어야 함)
curl -s https://hosub.duckdns.org/.well-known/oauth-authorization-server | python3 -m json.tool
```

### B.5 커넥터 등록 / 대시보드

- 커넥터 URL: `https://hosub.duckdns.org/mcp` (헤더 입력 불필요, OAuth 로 연결)
  → **연결** 클릭 → 브라우저에서 **대시보드 비밀번호** 입력 → 승인. 절차는 7.2 와 동일.
- 대시보드: `https://hosub.duckdns.org/`

### B.6 유의 (직접 노출이라 중요)

- 포트포워딩은 서버를 **인터넷에 직접 노출**한다. 이 서버는 `run_command` 로 root 까지
  되므로, **Bearer 토큰을 강하게 유지**하고 유출에 각별히 주의한다.
- `DUCKDNS_TOKEN` 은 비밀값이다. `.env`(권한 600) 에만 두고 절대 커밋하지 않는다.
- Caddy 는 인증서 발급 시 Let's Encrypt → 실패하면 ZeroSSL 로 자동 폴백한다.
- 필요하면 Caddyfile 의 `@blocked` 예시로 특정 IP만 허용하도록 조일 수 있다.
- 방화벽(ufw)을 쓰면 80/443 을 열어야 한다: `sudo ufw allow 80,443/tcp`.
