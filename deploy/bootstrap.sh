#!/usr/bin/env bash
# hosub-mcp 최초 설치 부트스트랩 (서버에서 1회 실행).
#
# 전제: 이 저장소가 이미 /opt/hosub-mcp 에 clone 되어 있고, 스크립트를
# 그 안에서 실행한다. venv 생성 → 의존성 설치 → .env 템플릿 준비 →
# systemd 유닛 설치 → 자동 업데이트 타이머 등록까지 수행한다.
#
# 사용법:
#   sudo bash deploy/bootstrap.sh
#
# 실행 후 반드시 /opt/hosub-mcp/.env 를 편집해 시크릿을 채우고,
# 마지막에 안내되는 명령으로 서비스를 기동한다.
set -euo pipefail

APP_DIR="${HOSUB_MCP_APP_DIR:-/opt/hosub-mcp}"
RUN_USER="${HOSUB_MCP_USER:-hosub}"

log() { echo "[bootstrap] $*"; }

if [ "$(id -u)" -ne 0 ]; then
  echo "root 로 실행하세요: sudo bash deploy/bootstrap.sh" >&2
  exit 1
fi

# 1) 전용 유저
if ! id "$RUN_USER" >/dev/null 2>&1; then
  log "전용 유저 생성: $RUN_USER"
  useradd -r -m -d "$APP_DIR" -s /bin/bash "$RUN_USER"
fi

# 2) 소유권
log "소유권 설정: $APP_DIR -> $RUN_USER"
chown -R "$RUN_USER:$RUN_USER" "$APP_DIR"

# 3) venv + 의존성 (전용 유저 권한으로)
log "venv 생성 및 의존성 설치"
sudo -u "$RUN_USER" bash -c "
  cd '$APP_DIR'
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
"

# 4) .env 템플릿 + 시크릿 자동 생성 (없을 때만)
if [ ! -f "$APP_DIR/.env" ]; then
  log ".env 생성 (시크릿 자동 발급)"
  TOKEN="$(openssl rand -hex 32)"
  SESSION="$(openssl rand -hex 32)"
  sudo -u "$RUN_USER" bash -c "cat > '$APP_DIR/.env'" <<EOF
# hosub MCP 서버 환경변수 (자동 생성됨 — 대시보드 비밀번호는 직접 채우세요)
HOSUB_MCP_TOKEN=$TOKEN
HOSUB_SESSION_SECRET=$SESSION
HOSUB_DASH_PASSWORD=
HOSUB_MCP_DB=data/audit.db
HOSUB_MCP_REGISTRY=config/registry.yaml
HOSUB_MCP_STRICT=false
HOSUB_MCP_HOST=127.0.0.1
HOSUB_MCP_PORT=8700
# 자동 업데이트가 추적할 브랜치
HOSUB_MCP_BRANCH=main
# (선택) DNS 리바인딩 보호용 허용 Host. 예: mcp.example.com
HOSUB_ALLOWED_HOSTS=
EOF
  chmod 600 "$APP_DIR/.env"
  chown "$RUN_USER:$RUN_USER" "$APP_DIR/.env"
  log "⚠️  $APP_DIR/.env 의 HOSUB_DASH_PASSWORD 를 반드시 채우세요."
else
  log ".env 이미 존재 — 건너뜀"
fi

# 5) systemd 유닛 설치
log "systemd 유닛 설치"
install -m 644 "$APP_DIR/deploy/hosub-mcp.service" /etc/systemd/system/hosub-mcp.service
install -m 644 "$APP_DIR/deploy/hosub-mcp-update.service" /etc/systemd/system/hosub-mcp-update.service
install -m 644 "$APP_DIR/deploy/hosub-mcp-update.timer" /etc/systemd/system/hosub-mcp-update.timer
chmod +x "$APP_DIR/deploy/update.sh"
systemctl daemon-reload

log "완료. 다음 단계:"
cat <<EOF

  1) 대시보드 비밀번호 설정:
       sudo -u $RUN_USER vi $APP_DIR/.env      # HOSUB_DASH_PASSWORD 채우기

  2) sudo 권한 부여 (docs/SETUP.md 참고):
       echo '$RUN_USER ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/hosub-mcp

  3) journald 읽기 권한:
       sudo usermod -aG systemd-journal $RUN_USER

  4) 서비스 + 자동 업데이트 타이머 기동:
       sudo systemctl enable --now hosub-mcp
       sudo systemctl enable --now hosub-mcp-update.timer

  5) 스모크 테스트 (401 이면 정상):
       curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8700/mcp

EOF
