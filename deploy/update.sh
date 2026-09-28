#!/usr/bin/env bash
# hosub-mcp 자동 업데이트 스크립트 (pull 기반).
#
# 지정 브랜치(기본 main)를 fast-forward 로 받아 두고, **돌고 있는 프로세스가
# 기동한 커밋 이후 실제로 바뀐 경로**를 보고 재시작 대상을 정한다. systemd 타이머
# (hosub-mcp-update.timer)가 5분마다 호출한다. 수동 배포에도 그대로 쓸 수 있다.
#
# NAT 뒤 홈서버에 적합한 방식: 외부에서 서버로 들어오는 경로(SSH 개방)가
# 필요 없고, 서버가 능동적으로 GitHub 를 폴링한다.
#
# 왜 이렇게 하나 — 이 저장소 커밋은 대부분 trading/·tnm/·docs/ 다. 예전에는
# 커밋이 오기만 하면 hosub-mcp 를 재시작했고, MCP 잡은 인메모리·자식 프로세스라
# 그때마다 background 잡이 죽었다(2026-09-27, 55분짜리 스윕 사망).
#
#   1) 경로 필터 — 바뀐 파일로 재시작 범위를 정한다:
#        trading/ tnm/ docs/ tests/ scripts/ llm-gateway/ *.md → 재시작 안 함
#        static/ src/dashboard.py src/asgi_dash.py            → 대시보드만
#        그 외(src/ requirements.txt config/ deploy/ …)        → MCP + 대시보드
#      모르는 경로는 "재시작"으로 떨어진다(안전한 쪽).
#   2) 실행 중 잡이 있으면 연기 — 각 프로세스가 data/runstate-*.json 에 기동 커밋
#      (rev)과 active_jobs 를 적어 둔다. 0 이 아니면 다음 주기로 미루되, 최대
#      HOSUB_UPDATE_MAX_DEFER_SEC(기본 7200초) 가 지나면 강행한다.
#   3) 비교 기준은 "이번 pull 직전 HEAD" 가 아니라 **런스테이트의 rev** 다.
#      연기된 재시작이 다음 주기에도 남아 있고, deploy_service("tnm"/"dash") 가
#      같은 클론을 먼저 pull 해 버려도 놓치지 않는다. 런스테이트가 없거나
#      (구버전·서비스 정지) 기록한 프로세스가 지금의 MainPID 가 아니면
#      pull 직전 HEAD 로 되돌아간다 — 예전 동작과 같다.
set -euo pipefail

APP_DIR="${HOSUB_MCP_APP_DIR:-/opt/hosub-mcp}"
BRANCH="${HOSUB_MCP_BRANCH:-main}"
SERVICE="${HOSUB_MCP_SERVICE:-hosub-mcp}"
DASH_SERVICE="${HOSUB_DASH_SERVICE:-hosub-dash.service}"

log() { echo "[hosub-mcp-update] $*"; }

# --- 설정 ---
RUNSTATE_MCP="${HOSUB_RUNSTATE_MCP:-data/runstate-mcp.json}"
RUNSTATE_DASH="${HOSUB_RUNSTATE_DASH:-data/runstate-dash.json}"
MAX_DEFER="${HOSUB_UPDATE_MAX_DEFER_SEC:-7200}"
DEFER_DIR="${HOSUB_UPDATE_STATE_DIR:-data}"
PY="python3"
[ -x ".venv/bin/python" ] && PY=".venv/bin/python"

# 바뀐 파일 하나 → ignore | dash | full
classify_path() {
  case "$1" in
    static/*|src/dashboard.py|src/asgi_dash.py) echo dash ;;
    trading/*|tnm/*|docs/*|tests/*|scripts/*|llm-gateway/*|*.md) echo ignore ;;
    *) echo full ;;
  esac
}

# base..HEAD 사이 변경 → none | dash | full (가장 넓은 것)
classify_range() {
  local base="$1" f level=none
  [ "$base" = "$HEAD" ] && { echo none; return; }
  # base 를 모르면(히스토리 재작성 등) 판단 불가 → 안전하게 full
  git cat-file -e "${base}^{commit}" 2>/dev/null || { echo full; return; }
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    case "$(classify_path "$f")" in
      full) echo full; return ;;
      dash) level=dash ;;
    esac
  done < <(git diff --name-only "$base" "$HEAD")
  echo "$level"
}

# 런스테이트 읽기 → "rev active" (유효하지 않으면 "- 0")
# 기록한 pid 가 유닛의 현재 MainPID 와 다르면 죽은 프로세스의 흔적이라 버린다.
read_runstate() {
  local file="$1" unit="$2" main_pid
  main_pid="$(systemctl show -p MainPID --value "$unit" 2>/dev/null || echo 0)"
  [ -f "$file" ] || { echo "- 0"; return; }
  "$PY" - "$file" "$main_pid" <<'PYEOF' 2>/dev/null || echo "- 0"
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    print("- 0"); sys.exit()
if str(d.get("pid")) != sys.argv[2]:
    print("- 0"); sys.exit()
print(d.get("rev") or "-", int(d.get("active_jobs") or 0))
PYEOF
}

# 실행 중 잡이 있으면 연기할지 판정. 0=진행, 1=연기
should_defer() {
  local name="$1" active="$2" f="$DEFER_DIR/.update-deferred-$1" since now
  if [ "$active" -le 0 ]; then
    rm -f "$f"; return 1
  fi
  now="$(date +%s)"
  mkdir -p "$DEFER_DIR"
  [ -s "$f" ] || echo "$now" > "$f"
  since="$(cat "$f")"
  if [ $(( now - since )) -ge "$MAX_DEFER" ]; then
    log "경고: $name 실행 중 잡 ${active}개지만 최대 연기(${MAX_DEFER}s) 초과 — 재시작 강행 (잡은 lost_on_restart 로 남음)"
    rm -f "$f"; return 1
  fi
  log "$name 재시작 연기: 실행 중 잡 ${active}개 (연기 $(( now - since ))s / 최대 ${MAX_DEFER}s)"
  return 0
}

restart_unit() {
  local unit="$1"
  sudo systemctl restart "$unit"
}

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

if [ "$LOCAL" != "$REMOTE" ]; then
  log "업데이트 감지: ${LOCAL:0:8} -> ${REMOTE:0:8}"
  # fast-forward 만 허용 (히스토리 꼬임 방지)
  git merge --ff-only "origin/${BRANCH}"
fi
HEAD="$(git rev-parse HEAD)"

read -r MCP_REV MCP_ACTIVE <<<"$(read_runstate "$RUNSTATE_MCP" "$SERVICE")"
[ "$MCP_REV" = "-" ] && MCP_REV="$LOCAL"
NEED_MCP="$(classify_range "$MCP_REV")"

DASH_ON=0
if systemctl list-unit-files "$DASH_SERVICE" >/dev/null 2>&1 \
   && systemctl is-enabled --quiet "$DASH_SERVICE" 2>/dev/null; then
  DASH_ON=1
  read -r DASH_REV DASH_ACTIVE <<<"$(read_runstate "$RUNSTATE_DASH" "$DASH_SERVICE")"
  [ "$DASH_REV" = "-" ] && DASH_REV="$LOCAL"
  NEED_DASH="$(classify_range "$DASH_REV")"
else
  NEED_DASH=none; DASH_ACTIVE=0
fi

DO_MCP=0; DO_DASH=0
[ "$NEED_MCP" = full ] && DO_MCP=1
# 대시보드는 MCP 쪽 변경(src/ 등)에도, 자기 쪽 변경(static/ 등)에도 재시작한다.
# static/ 은 같은 클론에서 즉시 서빙되므로 대시보드 프로세스와 버전이 어긋나면
# 안 된다(예: /api/llm/jobs/{id} 404).
[ "$DASH_ON" = 1 ] && [ "$NEED_DASH" != none ] && DO_DASH=1

if [ "$DO_MCP" = 0 ] && [ "$DO_DASH" = 0 ]; then
  rm -f "$DEFER_DIR"/.update-deferred-*
  if [ "$LOCAL" = "$REMOTE" ]; then
    log "이미 최신 (${HEAD:0:8})"
  else
    log "재시작 불필요 — 서비스 코드 변경 없음 (${HEAD:0:8}, trading/·docs/ 등만 바뀜)"
  fi
  exit 0
fi

[ "$DO_MCP" = 1 ] && should_defer mcp "$MCP_ACTIVE" && DO_MCP=0
[ "$DO_DASH" = 1 ] && should_defer dash "$DASH_ACTIVE" && DO_DASH=0
[ "$DO_MCP" = 0 ] && [ "$DO_DASH" = 0 ] && exit 0

# 의존성 변경이 있을 수 있으니 재시작 전에 반영 (이미 설치돼 있으면 빠르게 통과)
if [ -x ".venv/bin/pip" ]; then
  .venv/bin/pip install --quiet --upgrade -r requirements.txt
else
  log "경고: .venv/bin/pip 없음 — 의존성 설치 건너뜀"
fi

# 서비스 재시작 (sudoers 에 systemctl restart 권한 필요)
if [ "$DO_DASH" = 1 ]; then
  log "대시보드 재시작: $DASH_SERVICE (${DASH_REV:0:8} -> ${HEAD:0:8}, $NEED_DASH)"
  restart_unit "$DASH_SERVICE" || log "경고: $DASH_SERVICE 재시작 실패"
fi
if [ "$DO_MCP" = 0 ]; then
  exit 0
fi
log "MCP 재시작: $SERVICE (${MCP_REV:0:8} -> ${HEAD:0:8})"
restart_unit "$SERVICE"
sleep 2

if systemctl is-active --quiet "$SERVICE"; then
  log "재시작 완료, ${SERVICE} active (${HEAD:0:8})"
else
  log "오류: 재시작 후 ${SERVICE} 가 active 아님"
  systemctl status "$SERVICE" --no-pager -l | tail -20 || true
  exit 1
fi
