#!/usr/bin/env bash
# 이 서버를 Tailscale **exit node** 로 만든다.
#
# 용도: 집 밖(특히 중국처럼 인터넷이 검열·열화된 망)에서 기기의 트래픽 전체를
# 이 서버로 뽑아 한국 회선으로 나가게 한다. 맥북·아이폰·안드로이드 모두
# Tailscale 공식 앱에서 "Exit node → hosub" 하나만 고르면 된다.
#
# ⚠️ `tailscale up --advertise-exit-node` 를 쓰지 않는다.
#    이 서버는 **이미** tailnet 에 붙어 맥 스튜디오(100.69.201.28)의 Ollama 를
#    부르고 있다(llm-gateway 의 유일한 백엔드 경로). `tailscale up` 은 명시하지
#    않은 플래그를 전부 기본값으로 되돌리므로, 기존 설정이 조용히 날아가
#    게이트웨이가 맥을 못 찾는 사고가 난다. `tailscale set` 은 준 플래그만 바꾼다.
#
# ⚠️ 이 스크립트만으로 켜지지 않는다. 관리 콘솔 승인이 필요하다:
#      https://login.tailscale.com/admin/machines
#      → hosub → ⋯ → Edit route settings → "Use as exit node" 체크
#    **이 콘솔은 중국에서 막힐 수 있다.** 연결이 되는 동안 미리 해둘 것.
#
# 설치/실행:
#   sudo /opt/hosub-mcp/deploy/vpn-exit-node.sh
#
# 되돌리기:
#   sudo tailscale set --advertise-exit-node=false
set -euo pipefail

log() { echo "[vpn-exit-node] $*"; }

command -v tailscale >/dev/null 2>&1 || {
  echo "[vpn-exit-node] tailscale 이 없습니다. 이 서버는 이미 tailnet 에 붙어 있어야 합니다." >&2
  exit 1
}

# --- 1. IP 포워딩 -----------------------------------------------------------
# exit node 는 남의 패킷을 대신 내보내는 라우터가 된다. 커널 포워딩이 꺼져 있으면
# 핸드셰이크는 되는데 트래픽만 조용히 죽는다 — 중국에서 디버깅하기 최악인 증상이라
# 여기서 확실히 켜고 검증까지 한다.
SYSCTL_FILE=/etc/sysctl.d/99-tailscale.conf
if ! grep -qs 'net.ipv4.ip_forward *= *1' "$SYSCTL_FILE" 2>/dev/null; then
  log "IP 포워딩 활성화 → $SYSCTL_FILE"
  cat > "$SYSCTL_FILE" <<'EOF'
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1
EOF
fi
sysctl -p "$SYSCTL_FILE" >/dev/null
[ "$(sysctl -n net.ipv4.ip_forward)" = "1" ] || { echo "[vpn-exit-node] ip_forward 적용 실패" >&2; exit 1; }
log "ip_forward=1 확인"

# --- 2. UDP GRO 튜닝 (처리량) ------------------------------------------------
# 이게 없으면 exit node 처리량이 체감상 몇 배 떨어진다(Tailscale 공식 권고).
# 중국↔한국은 이미 RTT 가 길어 여유가 없으므로 반드시 켠다.
# networkd-dispatcher 훅으로 남겨야 재부팅·링크 재협상 후에도 유지된다.
NETDEV="$(ip -o route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}' | head -1)"
if [ -n "$NETDEV" ] && command -v ethtool >/dev/null 2>&1; then
  ethtool -K "$NETDEV" rx-udp-gro-forwarding on rx-gro-list off 2>/dev/null \
    && log "UDP GRO 튜닝 적용: $NETDEV" \
    || log "경고: $NETDEV 가 rx-udp-gro-forwarding 을 지원하지 않음 (치명적이지 않음)"
  HOOK=/etc/networkd-dispatcher/routable.d/50-tailscale
  if [ -d /etc/networkd-dispatcher/routable.d ]; then
    cat > "$HOOK" <<EOF
#!/bin/sh
# Tailscale exit node 처리량 튜닝 (deploy/vpn-exit-node.sh 가 설치)
ethtool -K $NETDEV rx-udp-gro-forwarding on rx-gro-list off || true
EOF
    chmod 0755 "$HOOK"
    log "재부팅 후에도 유지되도록 훅 설치: $HOOK"
  else
    log "경고: networkd-dispatcher 없음 — GRO 튜닝이 재부팅 시 초기화된다"
  fi
else
  log "경고: 기본 인터페이스 탐지 실패 또는 ethtool 없음 — GRO 튜닝 건너뜀"
fi

# --- 3. exit node 광고 -------------------------------------------------------
# set 은 멱등하다. 이미 켜져 있으면 아무 일도 안 한다.
log "tailscale set --advertise-exit-node (기존 플래그는 건드리지 않음)"
tailscale set --advertise-exit-node

# --- 4. 상태 + 남은 수동 단계 -------------------------------------------------
echo
log "현재 tailnet 상태:"
tailscale status || true
echo
log "여기까지는 '광고'다. 실제로 쓰려면 관리 콘솔에서 승인해야 한다:"
log "  https://login.tailscale.com/admin/machines → hosub → ⋯ → Edit route settings"
log "  → [x] Use as exit node"
echo
log "그리고 기기(맥북·아이폰·안드로이드)마다:"
log "  Tailscale 앱 → Exit Node → hosub 선택"
log "  (집 밖 로컬 프린터/공유기도 같이 쓰려면 '로컬 네트워크 접근 허용' 켜기)"
echo
log "⚠️ 중국에 있는 기기는 **키 만료 해제**를 반드시 해둘 것."
log "   기본 180일 후 키가 만료되면 재인증이 필요한데, Tailscale 로그인(SSO)은"
log "   중국에서 막히는 경우가 많아 그대로 접속 불능이 된다."
log "   관리 콘솔 → Machines → 해당 기기 → ⋯ → Disable key expiry"
