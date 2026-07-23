> **[아카이브] 이 실행 요청서는 완료되었습니다.**
> 완료일: 2026-07-23 · 대상 서버: `kch83.iptime.org` (192.168.0.3)
> 결과: hosub-mcp 설치·기동, DuckDNS+Caddy HTTPS, OAuth 2.1 커넥터, Bootstrap 대시보드 검증 완료.
> 재사용 가능한 배포 절차는 `docs/SETUP.md` 를 참조하세요. 이 문서는 기록 보관용입니다.

---

# 실행 요청서 — hosub 서버 배포·연동 (로컬 Claude Code 수행)

> 이 문서는 **집 LAN 안의 로컬 Claude Code**(서버에 SSH 로 접근 가능한 환경)에서
> 수행할 배포 실행 요청서다. 클라우드/웹 세션은 사설망에 닿지 못하므로, 이 작업은
> 반드시 로컬 세션 또는 서버 콘솔에서 진행한다.

## 0. 대상 정보

| 항목 | 값 |
|---|---|
| 서버 | `kch83.iptime.org` / LAN IP `192.168.0.3`, Ubuntu |
| SSH 계정 | `choho@192.168.0.3` (또는 서버 콘솔) |
| 저장소 | `https://github.com/teenfo/hosub-mcp` (**public** — clone/pull 인증 불필요) |
| 설치 경로 | `/opt/hosub-mcp` |
| 실행 유저 | `hosub` (bootstrap 이 생성, 프로세스는 root 금지) |
| 공개 도메인 | DuckDNS 서브도메인 (iptime `*.iptime.org` 은 CAA 로 인증서 발급 불가) |
| 배포 방식 | pull 기반 자동 업데이트(`hosub-mcp-update.timer`, 5분 주기) |
| 상세 문서 | `docs/SETUP.md` (부록 B: DuckDNS+Caddy) 참조 |

> 저장소가 public 이라 이전 계획의 **deploy key 는 불필요**하다. `git clone/pull` 이
> 인증 없이 된다.

## 1. 목표

1. 서버에 hosub-mcp 를 설치·기동하고 자동 업데이트를 켠다.
2. DuckDNS + Caddy 로 공인 HTTPS 를 만든다(도메인 구매 없이).
3. Claude 커넥터를 OAuth 로 연결해 폰/앱에서 서버를 대화로 제어 가능하게 한다.
4. 조회 전용 대시보드(Bootstrap admin)를 접속 확인한다.

## 2. 선행 확인 (서버에서)

```bash
python3 --version           # 3.11+ 필요
git --version; openssl version
sudo -v                     # sudo 가능
ping -c1 github.com         # 인터넷 (git/pip)
```
빠진 패키지: `sudo apt update && sudo apt install -y python3-venv git openssl curl`

## 3. 설치 (서버에서, 순서대로)

### 3.1 clone + 부트스트랩
```bash
sudo mkdir -p /opt/hosub-mcp && sudo chown "$USER":"$USER" /opt/hosub-mcp
git clone https://github.com/teenfo/hosub-mcp.git /opt/hosub-mcp
cd /opt/hosub-mcp
sudo bash deploy/bootstrap.sh      # 전용유저·venv·의존성·.env(토큰 자동)·systemd 유닛
```

### 3.2 .env 채우기
```bash
sudo -u hosub vi /opt/hosub-mcp/.env
```
- `HOSUB_DASH_PASSWORD=` 대시보드 로그인 비밀번호 (직접 입력)
- `HOSUB_PUBLIC_URL=https://<서브도메인>.duckdns.org` (OAuth 메타데이터 issuer — **필수**)
- `HOSUB_ALLOWED_HOSTS=<서브도메인>.duckdns.org`
- `DUCKDNS_DOMAIN=<서브도메인>` (예: hosub)
- `DUCKDNS_TOKEN=<DuckDNS 토큰>` (⚠️ 비밀, 커밋 금지)
- `HOSUB_MCP_TOKEN` / `HOSUB_SESSION_SECRET` 은 bootstrap 이 자동 발급(그대로 둠)

### 3.3 sudo 권한 (서버 전체 제어)
```bash
echo 'hosub ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/hosub-mcp
sudo chmod 440 /etc/sudoers.d/hosub-mcp && sudo visudo -c
sudo usermod -aG systemd-journal hosub      # journalctl 읽기
```
> 화이트리스트만 원하면 SETUP.md 4절의 제한형 sudoers 로 대체(단, `run_command` root 작업 불가).

### 3.4 서비스 + 자동 업데이트 + DuckDNS 타이머 기동
```bash
sudo systemctl enable --now hosub-mcp
sudo systemctl enable --now hosub-mcp-update.timer
# DuckDNS IP 자동 갱신
sudo install -m 644 /opt/hosub-mcp/deploy/duckdns-update.service /etc/systemd/system/
sudo install -m 644 /opt/hosub-mcp/deploy/duckdns-update.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now duckdns-update.timer
sudo -u hosub bash -c 'set -a; . /opt/hosub-mcp/.env; set +a; /opt/hosub-mcp/deploy/duckdns-update.sh'  # OK 확인
```
로컬 스모크(401 이면 정상):
```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8700/mcp
```

### 3.5 iptime 포트포워딩 (관리자 페이지, 수동)
`192.168.0.1` → 고급설정 → NAT/라우터 → 포트포워드:
| 외부 | 내부 IP | 내부 포트 |
|---|---|---|
| 80 | 192.168.0.3 | 80 |
| 443 | 192.168.0.3 | 443 |

### 3.6 Caddy (자동 TLS)
```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
sudo mkdir -p /var/log/caddy
sudo cp /opt/hosub-mcp/deploy/Caddyfile /etc/caddy/Caddyfile
sudo sed -i 's/hosub\.duckdns\.org/<서브도메인>.duckdns.org/' /etc/caddy/Caddyfile
sudo systemctl restart caddy
journalctl -u caddy -n 40 --no-pager     # "certificate obtained" 확인
```

## 4. 검증

```bash
# 외부 HTTPS 도달 + 인증 (401 이면 정상: TLS OK + 인증 요구)
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://<서브도메인>.duckdns.org/mcp
# OAuth 메타데이터 issuer 가 공개 URL 인지
curl -s https://<서브도메인>.duckdns.org/.well-known/oauth-authorization-server | python3 -m json.tool
# 대시보드: 브라우저에서 https://<서브도메인>.duckdns.org/ → 비밀번호 로그인 → Bootstrap admin 대시보드
```

## 5. Claude 커넥터 연결 (OAuth)

1. Claude.ai → Settings → Connectors → Add custom connector
2. URL: `https://<서브도메인>.duckdns.org/mcp` (**헤더 입력 불필요**)
3. 연결(Connect) → 브라우저 창에서 **대시보드 비밀번호** 입력 → 승인
4. 대화창 + → Connectors 활성화 → "hosub 시스템 상태 알려줘"(`get_system_status`) 테스트

## 6. 자동 배포 확인

이후 main 에 머지되는 코드는 5분 내 자동 반영된다.
```bash
journalctl -u hosub-mcp-update.service -n 20 --no-pager
sudo -u hosub git -C /opt/hosub-mcp rev-parse HEAD    # 최신 커밋과 일치하는지
```

## 7. 트러블슈팅 요약 (자세히는 SETUP.md 10절)

- 로컬 curl 이 401 아님 → 서비스 미기동: `journalctl -u hosub-mcp -n 50`
- `HOSUB_MCP_TOKEN 미설정` 에러 → `.env` 토큰 32자 미만/빈 값
- Caddy 인증서 실패 → 80/443 포워딩 확인, `journalctl -u caddy`
- DuckDNS OK 아님 → `DUCKDNS_DOMAIN/TOKEN` 확인, `getent hosts <sub>.duckdns.org`
- 커넥터 연결 후 메타데이터 이슈 → `HOSUB_PUBLIC_URL` 미설정이 가장 흔함
- `restart_service` 실패 → sudoers 권한

## 8. 완료 후 보고 (로컬 세션 → 사용자)

아래를 확인해 결과를 요약 보고한다:
- [ ] `systemctl is-active hosub-mcp` = active
- [ ] `systemctl list-timers` 에 hosub-mcp-update / duckdns-update 등록
- [ ] 로컬 `POST /mcp` → 401
- [ ] 외부 `https://<sub>.duckdns.org/mcp` → 401 (TLS OK)
- [ ] `.well-known/oauth-authorization-server` issuer = 공개 URL
- [ ] 대시보드 로그인 성공(Bootstrap admin 표시)
- [ ] Claude 커넥터 OAuth 연결 성공 + `get_system_status` 응답
- [ ] `git rev-parse HEAD` = main 최신

## 9. 보안 체크리스트

- [ ] `.env` 권한 600, 소유자 hosub, 절대 커밋 금지(`DUCKDNS_TOKEN`/`HOSUB_MCP_TOKEN`)
- [ ] 프로세스 hosub 유저 실행(root 금지)
- [ ] 서버 로그인 비밀번호 강화 + SSH 키 인증 권장(비번 로그인 비활성)
- [ ] 감사 DB(`data/audit.db`) 주기 확인 — 임의 명령 실행 이력
- [ ] 가능하면 Cloudflare Access 등 앞단 보호 추가
