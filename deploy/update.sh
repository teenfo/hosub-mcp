#!/usr/bin/env bash
# hosub-mcp 자동 업데이트 스크립트 (pull 기반).
#
# 지정 브랜치(기본 main)의 원격 커밋과 로컬을 비교해, 변경이 있을 때만
# git pull + 의존성 설치 + 서비스 재시작을 수행한다. systemd 타이머
# (hosub-mcp-update.timer)가 주기적으로 이 스크립트를 호출한다. 수동 배포에도
# 그대로 쓸 수 있다.
#
# NAT 뒤 홈서버에 적합한 방식: 외부에서 서버로 들어오는 경로(SSH 개방)가
# 필요 없고, 서버가 능동적으로 GitHub 를 폴링한다.
set -euo pipefail

APP_DIR="${HOSUB_MCP_APP_DIR:-/opt/hosub-mcp}"
BRANCH="${HOSUB_MCP_BRANCH:-main}"
SERVICE="${HOSUB_MCP_SERVICE:-hosub-mcp}"

log() { echo "[hosub-mcp-update] $*"; }

cd "$APP_DIR"

# 원격 최신 상태 가져오기 (코드 변경 없음)
git fetch --quiet origin "$BRANCH"

LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse "origin/${BRANCH}")"

if [ "$LOCAL" = "$REMOTE" ]; then
  log "이미 최신 (${LOCAL:0:8})"
  exit 0
fi

log "업데이트 감지: ${LOCAL:0:8} -> ${REMOTE:0:8}"

# fast-forward 만 허용 (히스토리 꼬임 방지)
git merge --ff-only "origin/${BRANCH}"

# 의존성 변경이 있을 수 있으니 항상 반영 (이미 설치돼 있으면 빠르게 통과)
if [ -x ".venv/bin/pip" ]; then
  .venv/bin/pip install --quiet --upgrade -r requirements.txt
else
  log "경고: .venv/bin/pip 없음 — 의존성 설치 건너뜀"
fi

# 서비스 재시작 (sudoers 에 systemctl restart 권한 필요)
sudo systemctl restart "$SERVICE"
sleep 2

if systemctl is-active --quiet "$SERVICE"; then
  log "재시작 완료, ${SERVICE} active (${REMOTE:0:8})"
else
  log "오류: 재시작 후 ${SERVICE} 가 active 아님"
  systemctl status "$SERVICE" --no-pager -l | tail -20 || true
  exit 1
fi
