# hosub MCP 서버 설치·연동 가이드

이 문서는 코드 개발 완료 후 **hosub 서버에서 직접 수행**하는 설치·노출·연동 절차다.
(개발 스펙 Phase 4에 해당)

---

## 0. 사전 준비

- Ubuntu 서버, Python 3.11+
- Cloudflare 계정 + `cloudflared` (기존 Tunnel 구조 재사용)
- 제어 대상 서비스(BCL Portal, Ollama 등)가 systemd 유닛으로 등록되어 있을 것

---

## 1. 전용 유저 + 코드 배치

```bash
sudo useradd -r -m -d /opt/hosub-mcp -s /bin/bash hosub || true
sudo git clone <this-repo> /opt/hosub-mcp
sudo chown -R hosub:hosub /opt/hosub-mcp
sudo -u hosub bash -c '
  cd /opt/hosub-mcp
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
'
```

> **root 로 실행 금지.** 반드시 전용 `hosub` 유저로 프로세스를 띄운다.

---

## 2. sudo 권한 (⚠️ 서버 전체 제어)

이 서버는 대화로 **서버 전체를 제어**하도록 설계되었다(`run_command`, `write_file`,
임의 패키지 설치 등). 이를 위해 전용 유저에 전체 sudo 를 부여한다:

```bash
# /etc/sudoers.d/hosub-mcp  (visudo -f 로 편집 권장)
hosub ALL=(ALL) NOPASSWD: ALL
```

> **리스크 인지 필수.** 이 설정은 Bearer 토큰이 유출되면 서버 root 가 완전히
> 장악됨을 의미한다. 방어선은 (a) 강한 Bearer 토큰 (b) 도구의 `confirm` 게이트
> (c) 감사 로그뿐이다. 아래 **보안 체크리스트**를 반드시 지킬 것.

### 화이트리스트만 원한다면 (대안, 더 안전)

서버 전체 제어가 필요 없다면 전체 sudo 대신 유닛별로 제한한다. 단, 이 경우
`run_command`/`write_file` 로 하는 root 작업은 실패한다.

```bash
# /etc/sudoers.d/hosub-mcp (제한형)
hosub ALL=(root) NOPASSWD: /usr/bin/systemctl restart bcl-portal.service
hosub ALL=(root) NOPASSWD: /usr/bin/systemctl restart ollama.service
hosub ALL=(root) NOPASSWD: /usr/bin/systemctl restart hosub-mcp
```

### journald 로그 읽기 권한

```bash
sudo usermod -aG systemd-journal hosub
```

---

## 3. 환경변수(.env)

```bash
sudo -u hosub cp /opt/hosub-mcp/.env.example /opt/hosub-mcp/.env
sudo -u hosub vi /opt/hosub-mcp/.env
```

| 변수 | 설명 |
|---|---|
| `HOSUB_MCP_TOKEN` | Claude 커넥터 Bearer 토큰. `openssl rand -hex 32` (32자 이상 필수) |
| `HOSUB_DASH_PASSWORD` | 대시보드 웹 로그인 비밀번호 |
| `HOSUB_SESSION_SECRET` | 세션 쿠키 서명 키. `openssl rand -hex 32` |
| `HOSUB_MCP_DB` | 감사 DB 경로 (기본 `data/audit.db`) |
| `HOSUB_MCP_REGISTRY` | 레지스트리 경로 (기본 `config/registry.yaml`) |
| `HOSUB_MCP_STRICT` | `true` 면 스크립트 경로 존재/실행권한 검증 |
| `HOSUB_ALLOWED_HOSTS` | (선택) DNS 리바인딩 보호용 허용 Host. 예: `mcp.example.com`. 비우면 보호 비활성(Bearer+Cloudflare 의존) |

> `.env` 는 절대 Git 에 커밋하지 않는다 (`.gitignore` 에 포함됨).

---

## 4. systemd 등록

```bash
sudo cp /opt/hosub-mcp/deploy/hosub-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hosub-mcp
systemctl status hosub-mcp
```

로컬 스모크:

```bash
# 토큰 없이 → 401
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8700/mcp
```

---

## 5. Cloudflare Tunnel 노출

기존 tunnel 설정(`~/.cloudflared/config.yml`)에 ingress 추가:

```yaml
ingress:
  - hostname: mcp.example.com
    service: http://localhost:8700
  # ... 기존 규칙 ...
  - service: http_status:404
```

MCP 엔드포인트와 대시보드는 **같은 오리진**을 쓴다:
- MCP: `https://mcp.example.com/mcp`
- 대시보드: `https://mcp.example.com/`

```bash
cloudflared tunnel route dns <tunnel-name> mcp.example.com
sudo systemctl restart cloudflared
```

---

## 6. Claude Custom Connector 연동

1. Claude.ai → Settings → Connectors → **Add custom connector**
2. **URL**: `https://mcp.example.com/mcp`
3. **Advanced → Request Headers**:
   `Authorization: Bearer <HOSUB_MCP_TOKEN 값>`
4. 대화창 좌측 하단 **+ → Connectors** 에서 활성화
5. Low 위험 도구부터 테스트: "hosub 시스템 상태 알려줘" → `get_system_status`

> 커넥터 UI에서 커스텀 헤더를 지원하지 않는 요금제라면, 대안으로 Cloudflare
> Access **서비스 토큰**을 터널 엣지에서 강제하라. 토큰을 URL 쿼리에 넣는 방식은
> 절대 사용하지 말 것.

---

## 7. 대시보드 접속

브라우저에서 `https://mcp.example.com/` → `HOSUB_DASH_PASSWORD` 로 로그인.
시스템 리소스 / 서비스 상태 / 최근 잡 / 감사 로그가 카드로 표시된다(조회 전용).

> **권장:** 대시보드에도 Cloudflare Access(이메일 OTP/SSO)를 병행 적용하라.
> 비밀번호는 2차 방어선일 뿐이다.

---

## 8. 보안 체크리스트

- [ ] `HOSUB_MCP_TOKEN` 32자 이상 무작위, 유출 시 즉시 재발급
- [ ] `.env` 파일 권한 `600`, 소유자 `hosub`
- [ ] 프로세스는 `hosub` 유저로 실행 (root 금지)
- [ ] Cloudflare Access 로 MCP/대시보드 엔드포인트 추가 보호
- [ ] 감사 DB(`data/audit.db`) 를 주기적으로 확인 — 임의 명령 실행 이력 추적
- [ ] 레지스트리(`config/registry.yaml`) 변경은 Git PR 리뷰를 거쳐서만
