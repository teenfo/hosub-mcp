# 작업 로그

서버에 수행한 작업 내역을 최신이 위로 오게 기록한다.

## 2026-07-23 — trading 서비스 최초 배포 (런북: docs/requests/trading-deploy.md)

수행 주체: 클라우드 Claude 세션 (hosub MCP `run_command`/`write_file` 경유, 사용자 즉시 실행 지시)

- `/opt/hosub-trading` 에 `claude/confirmation-needed-2z6swl` 브랜치 클론 (hosub 사용자, 기존 원격 재사용)
- `trading/.venv` 생성 + 의존성 설치
- `trading/.env` 작성(권한 600): 실전 키, `DATA_DIR=/data/trading`, `INTERNAL_TOKEN` 발급
- `trading.service` systemd 등록·기동 — 초기 기동 실패 원인은 `.gitignore data/` 패턴이
  `app/data` 소스를 커밋에서 누락시킨 것. `/data/` 앵커링으로 수정 후 정상 기동
- **실계좌 키 검증(읽기 전용) 성공**: 토큰 발급 + 삼성전자 1분봉 900건 수신, 주문 API 미호출
- `/opt/hosub-mcp/.env` 에 `HOSUB_TRADING_URL`/`HOSUB_TRADING_TOKEN` 추가
  (대시보드 트레이딩 메뉴는 main 머지 + 자동 배포 후 활성화)
- 서비스 레지스트리(`config/registry.yaml`)에 trading 등록 — Git 경유(PR 리뷰 정책 준수)

남은 일:
- [ ] main 머지 → 5분 내 자동 배포로 대시보드 메뉴 활성화 확인
- [ ] 대시보드 API 설정 화면에서 계좌번호 입력
- [ ] 키움 포털에서 앱키 재발급(채팅 노출분 로테이션) 후 설정 화면에서 갱신
- [ ] 핸드오프 끝난 `local-request/trading-deploy` 브랜치 정리
