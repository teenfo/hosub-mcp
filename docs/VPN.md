# 집 밖에서 hosub 를 VPN 으로 쓰기 (검열망 대응)

집 서버(`192.168.0.3`, 한국 회선)를 출구로 삼아 **바깥 기기의 트래픽 전체**를
한국으로 뽑는다. 중국처럼 인터넷이 검열·열화된 망에서 쓰는 것을 전제로 한다.

## 0. 왜 두 겹인가

| | 1차 — Tailscale exit node | 2차 — sing-box (VLESS+WS) |
|---|---|---|
| 전송 | WireGuard (UDP) | TLS/WebSocket (TCP 443) |
| 서버 작업 | 스크립트 1개 + 콘솔 승인 | 스크립트 1개 |
| 공유기 변경 | 없음 | 없음 |
| 중국에서의 내성 | **낮음** — GFW 가 WireGuard 핸드셰이크를 지문으로 식별해 차단·스로틀한다 | **높음** — 기존 사이트에 대한 평범한 HTTPS 와 구분되지 않는다 |
| 딸려오는 것 | 집 LAN·맥 스튜디오까지 그대로 접근 | 트래픽 우회만 |

**둘 다 켜둔다.** 1차가 편하고 기능이 많지만 언제 막힐지 모르고, 막힌 뒤에
중국에서 2차를 설치하려면 서버에 닿을 방법 자체가 없다. 2차는 평소에 안 써도
살려두는 게 보험이다.

> ### ⚠️ 인터넷이 되는 동안 먼저 해둘 것
>
> 중국에서 막히는 건 터널만이 아니다. **설정에 필요한 관문**이 먼저 막힌다.
>
> 1. **Tailscale 관리 콘솔 접속** (`login.tailscale.com`) — exit node 승인·키
>    만료 해제를 여기서 한다. 나중에 못 들어가면 손쓸 수 없다.
> 2. **auth key 발급** — Settings → Keys → Generate auth key (reusable, 만료 90일).
>    기기를 붙일 때 브라우저 SSO(구글·깃허브) 대신 이 키를 쓸 수 있다.
>    **SSO 제공자가 중국에서 막히면 auth key 가 유일한 가입 경로다.**
> 3. **앱 설치** — 중국 App Store 계정에는 VPN 앱이 없다. 해외 계정으로 미리 받아둘 것.
> 4. **각 기기 키 만료 해제** — Machines → 기기 → ⋯ → Disable key expiry.
>    기본 180일 뒤 만료되는데, 재인증이 막히면 그대로 접속 불능이 된다.

---

## 1. 1차 — Tailscale exit node

이 서버는 이미 tailnet 에 있다(맥 스튜디오 `100.69.201.28` 의 Ollama 를 부르는
바로 그 경로). 그래서 **새로 설치할 게 없다.**

### 1.1 서버

```bash
sudo /opt/hosub-mcp/deploy/vpn-exit-node.sh
```

하는 일: IP 포워딩 활성화(+영속), UDP GRO 처리량 튜닝, `tailscale set
--advertise-exit-node`.

> ⚠️ `tailscale up --advertise-exit-node` 를 **직접 치지 말 것.** `up` 은 명시하지
> 않은 플래그를 전부 기본값으로 되돌린다. 이 서버의 tailnet 설정이 날아가면
> llm-gateway 가 맥을 못 찾아 LLM 전체가 죽는다. 스크립트가 쓰는 `set` 은
> 준 플래그만 바꾼다.

### 1.2 관리 콘솔 (필수)

`https://login.tailscale.com/admin/machines` → `hosub` → ⋯ →
**Edit route settings** → `[x] Use as exit node`

광고만으로는 안 켜진다. 이 승인이 빠지면 기기 목록에 hosub 가 안 뜬다.

### 1.3 기기

| 기기 | 앱 | 설정 |
|---|---|---|
| 맥북 | Tailscale (App Store 또는 tailscale.com) | 메뉴바 → Exit Node → **hosub** |
| 아이폰/아이패드 | Tailscale (해외 App Store 계정 필요) | 앱 → Exit Node → **hosub** |
| 안드로이드 | Tailscale (Play 스토어 또는 APK 직접) | 앱 → Exit Node → **hosub** |

맥북이 이미 tailnet 에 있다면(`100.107.151.46`) 로그인조차 필요 없다 — exit node
만 고르면 끝난다.

집 밖 로컬 네트워크(호텔 프린터, 공유기 관리 페이지)도 같이 쓰려면 **"로컬
네트워크 접근 허용"**(Allow LAN access)을 켠다. 끄면 exit node 가 로컬 대역까지
가로챈다.

---

## 2. 2차 — sing-box (Caddy 443 뒤에 숨기기)

1차가 불안정할 때 쓴다. 밖에서 보면 `hosub.duckdns.org` 에 대한 평범한 HTTPS
요청 하나이고, **비밀 경로**를 모르면 이 서비스가 있다는 것조차 알 수 없다.

### 2.1 왜 이 구조인가

공유기에 열린 포트는 80/443 뿐이고(`docs/SETUP.md` 부록 B.2), 사용자가 중국에
있어 iptime 관리자(`192.168.0.1`)에 못 닿는다. **새 포트를 열 수 없으므로 443 을
Caddy 와 공유하는 것이 유일한 길이었다.** 그 결과:

```
중국 기기 ──TLS/443──> Caddy(기존 인증서 그대로)
                         ├─ 비밀 경로 → 127.0.0.1:8605 sing-box → 인터넷(한국)
                         ├─ /llm/*    → llm-gateway   (기존 그대로)
                         ├─ /api/* /  → 대시보드      (기존 그대로)
                         └─ 그 외     → MCP           (기존 그대로)
```

- 인증서 발급 0건 (TLS 는 Caddy 가 끝내고 sing-box 는 루프백 평문 WS 만 듣는다)
- 공유기 변경 0건
- 기존 서비스 응답 변경 0건

### 2.2 설치

```bash
sudo /opt/hosub-mcp/deploy/vpn-singbox-install.sh
```

멱등하다. 두 번 돌려도 이미 만든 자격을 재사용한다(중국 기기 3대에 넣어둔 설정을
스크립트 재실행으로 무효화하면 원격 복구가 불가능하기 때문). 자격을 바꾸려면
`--rotate`.

### 2.3 접속 정보 꺼내기

**스크립트는 접속 문자열을 화면에 찍지 않는다.** 이 서버는 MCP `run_command` 로
조작되고 그 출력은 감사 DB(`data/audit.db`)와 대화 기록에 남는다. UUID 와 비밀
경로가 곧 접속 자격이므로 0600 파일로만 남긴다.

```bash
sudo cat /etc/hosub-vpn/client-link.txt   # vless://... (복사해서 앱에 붙여넣기)
sudo cat /etc/hosub-vpn/client-qr.txt     # 터미널 QR — 폰 카메라로 바로 스캔
```

### 2.4 클라이언트 앱

`vless://` 링크 하나(또는 QR)면 세 기기 모두 끝난다.

| 기기 | 추천 앱 | 비고 |
|---|---|---|
| 맥북 | **Hiddify** (오픈소스, sing-box 기반) | 링크 붙여넣기로 프로필 추가 |
| 아이폰/아이패드 | **Hiddify** 또는 **Shadowrocket**(유료, 가장 안정적) | 해외 App Store 계정 필요 |
| 안드로이드 | **Hiddify** 또는 **v2rayNG** | APK 직접 설치 가능 |

설정 시 확인할 것 두 가지:

1. **전역(Global) 모드로 둘 것.** 기본값인 룰 기반 모드는 중국 IP 로 판단한
   트래픽을 프록시 밖으로 흘린다. 전체 우회가 목적이면 전역이어야 한다.
2. **DNS 를 원격 해석으로 둘 것.** 로컬 해석이면 GFW 의 DNS 오염을 그대로 맞아서
   터널이 멀쩡해도 접속이 실패한다.

---

## 3. 어느 쪽이 살아 있는지 진단

증상이 "느리다/안 된다" 하나로 뭉개져서, 층을 나눠 봐야 한다.

```bash
# 서버에서 — exit node 광고 상태
tailscale status | head -20

# 서버에서 — 2차 터널이 떠 있는가
systemctl is-active vpn-singbox
sudo ss -ltnp | grep 8605          # 127.0.0.1:8605 로만 떠 있어야 정상
```

MCP 로도 볼 수 있다(중국에서 SSH 가 불안정할 때의 진단 경로):
`read_service_logs(service_name="vpn-singbox")` · `restart_service(service_name="vpn-singbox")`.

중국 기기에서:

| 증상 | 해석 | 대응 |
|---|---|---|
| Tailscale 이 "connecting" 에서 안 넘어감 | 컨트롤 플레인 차단 | 2차로 전환 |
| Tailscale 붙었는데 트래픽이 안 나감 | WireGuard 스로틀 또는 서버 IP 포워딩 | `vpn-exit-node.sh` 재실행 → 안 되면 2차 |
| 2차도 연결 실패, 도메인이 해석 안 됨 | `*.duckdns.org` DNS 오염 | 앱에서 서버 주소를 **공인 IP** 로 두고 SNI/Host 는 도메인 유지 |
| 둘 다 되는데 느림 | 집 회선 상향 대역 또는 중국↔한국 혼잡 | 시간대 바꿔 재시도 |

> **DuckDNS 오염 대비.** `*.duckdns.org` 는 우회 용도로 많이 쓰여 중국에서
> 오염·차단될 수 있다. 그 경우 앱에 도메인 대신 **집 공인 IP** 를 넣고 SNI/Host 만
> 도메인으로 남기면 된다(모든 추천 앱이 지원). 다만 집 IP 는 유동이라
> 바뀌면 다시 넣어야 한다 — 그래서 이게 1차 수단이 아니라 비상 수단이다.

---

## 4. 보안상 알고 쓸 것

- **이 서버는 `run_command` 로 root 까지 제어된다**(`docs/SETUP.md` 4절). VPN 자격이
  새면 공격자가 **한국 집 회선을 출구로 얻는 것**이지 서버를 장악하는 건 아니다
  (sing-box 는 전용 계정·루프백 전용·권한 제거 상태로 돈다). 그래도 자격 유출은
  즉시 `--rotate` 로 끊는다.
- **트래픽이 집 회선 명의로 나간다.** 바깥 기기가 하는 모든 것이 집 IP 로 기록된다.
- **서드파티 APT 저장소가 하나 늘어난다**(`deb.sagernet.org`). 공급망 신뢰 범위가
  넓어진다는 뜻이다. GitHub 릴리스 바이너리를 직접 받는 것보다는 GPG 검증과
  보안 업데이트가 붙는 쪽이 낫다고 보고 선택했다.
- **비밀은 커밋하지 않는다.** UUID·비밀 경로는 `/etc/hosub-vpn/` 와 Caddy systemd
  드롭인에만 있다. `deploy/Caddyfile` 은 `{$HOSUB_VPN_PATH}` 로 받기만 한다.
- **감사 로그에 자격을 남기지 않는다.** 설치 스크립트가 값을 출력하지 않는 이유다.

## 5. 되돌리기

```bash
# 1차만 끄기
sudo tailscale set --advertise-exit-node=false

# 2차만 끄기 (Caddy 는 원래 동작으로 복귀 — 다른 경로는 영향 없음)
sudo systemctl disable --now vpn-singbox
sudo rm -f /etc/systemd/system/caddy.service.d/hosub-vpn.conf
sudo systemctl daemon-reload && sudo systemctl restart caddy
```

두 경우 모두 **기존 서비스(대시보드·MCP·llm-gateway)는 건드리지 않는다.**
Caddyfile 의 `@vpn` 블록은 환경변수가 없으면 아무도 안 부르는 경로가 되어
사실상 없는 것과 같다.
