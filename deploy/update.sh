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
DASH_SERVICE="${HOSUB_DASH_SERVICE:-hosub-dash.service}"

log() { echo "[hosub-mcp-update] $*"; }

cd "$APP_DIR"

# 원격 최신 상태 가져오기 (코드 변경 없음)
git fetch --quiet origin "$BRANCH"

LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse "origin/${BRANCH}")"

# --- llm-gateway 드리프트 점검 (early exit 보다 먼저!) ---
#
# 게이트웨이는 **의도적으로** 여기서 재기동하지 않는다. 잡 큐를 들고 있어 다른
# 서비스 배포에 끌려 재시작되면 실행 중이던 추론이 끊기고 모델 다운로드가 중단된다.
#
# 다만 코드만 내려오고 컨테이너가 옛 이미지로 돌면 조용히 어긋난다. 그래서
# "이번 pull 에 게이트웨이 변경이 있었나"가 아니라 **"지금 배포된 것과 디스크가
# 같은가"** 를 본다. deploy_service("dash"/"tnm") 이 이미 git pull 해버린 뒤라
# LOCAL == REMOTE 여도 드리프트는 남아 있을 수 있기 때문이다.
gateway_drift_check() {
  local want have
  want="$(git rev-parse "HEAD:llm-gateway" 2>/dev/null || true)"
  [ -n "$want" ] || return 0
  have="$(cat llm-gateway/.deployed-tree 2>/dev/null || true)"
  [ "$want" != "$have" ] || return 0
  log "주의: llm-gateway 컨테이너가 현재 코드와 다릅니다(자동 재빌드 안 함)."
  log "      디스크=${want:0:12} 배포됨=${have:0:12}"
  log "      반영: sudo systemctl reload llm-gateway"
  log "      또는: MCP deploy_service(service_name='llm-gateway', confirm=true)"
}
gateway_drift_check

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
#
# 대시보드는 별도 프로세스인데 static/ 은 같은 클론에서 즉시 서빙된다. MCP 만
# 재시작하면 브라우저는 새 JS, 서버는 옛 라우트가 되어 버전이 어긋난다.
# (예: /api/llm/jobs/{id} 404) — 있으면 함께 재시작한다.
sudo systemctl restart "$SERVICE"
if systemctl list-unit-files "$DASH_SERVICE" >/dev/null 2>&1 \
   && systemctl is-enabled --quiet "$DASH_SERVICE" 2>/dev/null; then
  log "대시보드도 재시작: $DASH_SERVICE"
  sudo systemctl restart "$DASH_SERVICE" || log "경고: $DASH_SERVICE 재시작 실패"
fi
sleep 2

if systemctl is-active --quiet "$SERVICE"; then
  log "재시작 완료, ${SERVICE} active (${REMOTE:0:8})"
else
  log "오류: 재시작 후 ${SERVICE} 가 active 아님"
  systemctl status "$SERVICE" --no-pager -l | tail -20 || true
  exit 1
fi
