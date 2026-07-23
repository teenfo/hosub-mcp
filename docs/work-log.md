# 로컬 작업 내역서 — hosub MCP 배포 반영 체크리스트

> 이 문서는 지금까지 진행된 개발 내역과, **로컬 서버(kch83.iptime.org / 192.168.0.3)**
> 에서 확인·반영해야 할 후속 작업을 정리한 내역서다. 서버 작업은 로컬 Claude Code
> 또는 서버 콘솔에서 수행한다(클라우드 세션은 사설망에 닿지 못함).

## 1. 지금까지 머지된 작업 (main 기준)

| PR | 내용 |
|---|---|
| #1 | hosub MCP 서버 초기 구현 (도구 13종 + 정책 + 잡 + 감사 + 대시보드) |
| #2 | 도메인 없이 노출: Caddy 자동 TLS 자산 |
| #3 | DuckDNS 노출 지원 (iptime CAA 인증서 차단 우회) |
| #4 | OAuth 2.1 인증 서버 (claude.ai 커넥터 연결용) |
| #5 | 대시보드 Bootstrap 5 admin 테마 리디자인 |
| #6 | GitHub Actions push 배포 제거 (pull 방식으로 일원화) |
| #7 | 대시보드 멀티 페이지 + 데일리 브리핑·날씨·Docker |
| #8 | 데일리 브리핑 날짜별 디렉터리(html/morning-brief) |

현재 상태: MCP 서버 + OAuth + DuckDNS/Caddy TLS + 커넥터 연결 + Bootstrap 멀티 페이지
대시보드까지 동작 확인됨(커넥터로 `get_system_status` 실서버 응답 확인).

## 2. 자동 배포

`main` 에 머지되면 서버의 `hosub-mcp-update.timer` 가 **5분 내** `git pull` + `pip install`
+ `systemctl restart hosub-mcp` 로 자동 반영한다. 아래로 확인:

```bash
journalctl -u hosub-mcp-update.service -n 20 --no-pager
sudo -u hosub git -C /opt/hosub-mcp rev-parse HEAD    # main 최신(6e0b119 이후)과 일치?
systemctl is-active hosub-mcp
```

## 3. 이번 변경으로 로컬에서 반영할 것 (신규 페이지 관련)

### 3.1 `.env` 추가 설정 (없으면 추가)
```bash
sudo -u hosub vi /opt/hosub-mcp/.env
```
- `HOSUB_BRIEFING_DIR=html/morning-brief`   # 데일리 브리핑 디렉터리(기본값)
- `HOSUB_WEATHER_LATLON=37.5665,126.9780`  # 날씨 위치(기본 서울) — 원하는 도시로 변경
- `HOSUB_WEATHER_LABEL=서울`               # 날씨 표시 라벨
- (기존) `HOSUB_PUBLIC_URL=https://<서브도메인>.duckdns.org` — OAuth 메타데이터용, 필수

변경 후: `sudo systemctl restart hosub-mcp`

### 3.2 데일리 브리핑 폴더 (자동 생성됨)
`html/morning-brief/` 는 Claude 가 `write_file` 로 `<날짜>.html` 을 쓰면 자동 생성된다.
(`.gitignore` 에 포함되어 `git pull` 과 충돌하지 않음.) 수동 생성도 가능:
```bash
sudo -u hosub mkdir -p /opt/hosub-mcp/html/morning-brief
```

### 3.3 Docker 페이지 권한 (Docker 를 쓸 경우)
대시보드 Docker 페이지는 `docker ps` 를 실행한다. `hosub` 유저가 docker 를 쓰려면:
```bash
sudo usermod -aG docker hosub
sudo systemctl restart hosub-mcp    # 그룹 반영
```
> docker 미설치/권한 없으면 페이지가 "가져오지 못함" 안내를 표시(정상 graceful).

### 3.4 날씨 외부 호출
날씨 페이지는 서버가 Open-Meteo(`api.open-meteo.com`)를 조회한다. 서버 아웃바운드가
막혀 있지 않은지 확인(대개 홈서버는 자유). 실패 시 페이지가 graceful 안내 표시.

## 4. 데일리 브리핑 작성 방법 (Claude)

Claude(커넥터/Cowork/Routine)가 매일 아래처럼 브리핑을 쓴다:
```
write_file(
  path="/opt/hosub-mcp/html/morning-brief/2026-07-23.html",
  content="<h2>...</h2> ... (HTML)",
  confirm=true
)
```
- 파일명은 `YYYY-MM-DD.html`(또는 `.md`). 대시보드가 최신 날짜를 자동 표시.
- HTML 은 `<script>` 가 제거되어 렌더된다.
- **자동화(선택)**: 매일 아침 Claude Routine 을 걸어 서버 상태를 조회 → 브리핑을
  생성 → `write_file` 로 갱신하도록 예약할 수 있다.

## 5. 검증 체크리스트 (로컬/브라우저)

- [ ] `git rev-parse HEAD` = main 최신
- [ ] `systemctl is-active hosub-mcp` = active
- [ ] 대시보드 접속 `https://<서브도메인>.duckdns.org/` → 로그인 → 사이드바 4페이지
      (대시보드/데일리 브리핑/날씨/Docker)
- [ ] 날씨 페이지: 현재 날씨 + 예보 표시(위치 맞는지)
- [ ] 데일리 브리핑: 브리핑 파일 쓰면 표시, 날짜 드롭다운 동작
- [ ] Docker 페이지: 컨테이너 목록 또는 graceful 안내
- [ ] 커넥터: "hosub 시스템 상태 알려줘" → 응답

## 6. 참고 문서

- 전체 설치·연동: `docs/SETUP.md` (부록 B: DuckDNS + Caddy)
- 최초 배포 런북: `docs/requests/local-server-deployment.md`
- 대시보드 확장(새 페이지 추가): `static/pages/` 에 파일 1개 + `pages/index.js` 등록 1줄
