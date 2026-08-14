#!/usr/bin/env bash
# 검열우회 VPN(sing-box VLESS+WebSocket)을 기존 Caddy 443 뒤에 설치한다.
#
# 이건 **Tailscale exit node 가 막혔을 때의 2차 수단**이다. 먼저
# deploy/vpn-exit-node.sh 를 쓰고, 중국에서 불안정할 때 이걸 켠다.
# → 배경·클라이언트 설정은 docs/VPN.md
#
# 설계 제약 (이 스크립트가 이렇게 생긴 이유):
#   - 공유기에 새 포트를 못 연다. 사용자가 중국에 있고 iptime 관리자에 못 닿는다.
#     열려 있는 건 80/443 뿐 → **443 을 Caddy 와 공유**한다.
#   - 그래서 TLS 는 Caddy 가 끝내고 sing-box 는 루프백 평문 WS 만 듣는다.
#     인증서 발급 0건, 공유기 변경 0건, 롤백 = 유닛 정지 + 드롭인 삭제.
#
# 비밀 취급:
#   UUID 와 WS 경로가 곧 접속 자격이다. **표준출력으로 찍지 않는다** —
#   이 서버는 MCP run_command 로 조작되고 그 출력은 감사 DB(data/audit.db)와
#   대화 기록에 남는다. 접속 문자열은 0600 파일로만 남기고, 화면에는 지문만 찍는다.
#
# 사용:
#   sudo /opt/hosub-mcp/deploy/vpn-singbox-install.sh            # 설치(멱등)
#   sudo /opt/hosub-mcp/deploy/vpn-singbox-install.sh --rotate   # 자격 재발급
#
# 되돌리기:
#   sudo systemctl disable --now vpn-singbox
#   sudo rm -f /etc/systemd/system/caddy.service.d/hosub-vpn.conf
#   sudo systemctl daemon-reload && sudo systemctl restart caddy
set -euo pipefail

APP_DIR="${HOSUB_MCP_APP_DIR:-/opt/hosub-mcp}"
CONF_DIR=/etc/hosub-vpn
CONF="$CONF_DIR/singbox.json"
LINK="$CONF_DIR/client-link.txt"
QR_TXT="$CONF_DIR/client-qr.txt"
QR_PNG="$CONF_DIR/client-qr.png"
DROPIN_DIR=/etc/systemd/system/caddy.service.d
DROPIN="$DROPIN_DIR/hosub-vpn.conf"
PORT=8605
ROTATE=0
[ "${1:-}" = "--rotate" ] && ROTATE=1

log() { echo "[vpn-singbox] $*"; }
die() { echo "[vpn-singbox] 오류: $*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "root 로 실행해야 한다 (sudo)"

# 공개 도메인은 DuckDNS 설정에서 끌어온다 — 하드코딩하지 않는다.
DOMAIN="${HOSUB_VPN_DOMAIN:-}"
if [ -z "$DOMAIN" ] && [ -f "$APP_DIR/.env" ]; then
  DOMAIN="$(sed -n 's/^HOSUB_PUBLIC_URL=https\?:\/\/\([^/ ]*\).*/\1/p' "$APP_DIR/.env" | head -1)"
  [ -n "$DOMAIN" ] || DOMAIN="$(sed -n 's/^DUCKDNS_DOMAIN=[[:space:]]*\([^ #]*\).*/\1.duckdns.org/p' "$APP_DIR/.env" | head -1)"
fi
[ -n "$DOMAIN" ] || die "공개 도메인을 못 찾았다. HOSUB_VPN_DOMAIN=hosub.duckdns.org 로 지정해 다시 실행."
log "공개 도메인: $DOMAIN"

# --- 1. sing-box 설치 --------------------------------------------------------
# 공식 APT 저장소를 쓴다. GitHub 릴리스 바이너리를 직접 받는 것보다 GPG 검증과
# 이후 보안 업데이트가 apt 흐름에 얹히는 게 낫다.
# ⚠️ 서드파티 저장소를 추가한다는 뜻이다 — 이 서버는 root 제어 대상이므로
#    공급망 신뢰 범위가 한 곳 늘어난다는 점을 알고 쓴다.
if ! command -v sing-box >/dev/null 2>&1; then
  log "sing-box 설치 (deb.sagernet.org)"
  install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://sing-box.app/gpg.key -o /etc/apt/keyrings/sagernet.asc
  chmod a+r /etc/apt/keyrings/sagernet.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/sagernet.asc] https://deb.sagernet.org/ * *" \
    > /etc/apt/sources.list.d/sagernet.list
  apt-get update -qq
  apt-get install -y -qq sing-box
  # 패키지가 딸려 오는 유닛은 쓰지 않는다 — 우리 유닛이 설정 경로·권한을 따로 잡는다.
  systemctl disable --now sing-box.service 2>/dev/null || true
fi
command -v qrencode >/dev/null 2>&1 || apt-get install -y -qq qrencode
log "sing-box $(sing-box version 2>/dev/null | head -1)"

# --- 2. 전용 계정 + 설정 디렉터리 --------------------------------------------
id -u hosub-vpn >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin hosub-vpn
install -d -m 0750 -o root -g hosub-vpn "$CONF_DIR"

# --- 3. 자격 생성 (멱등 — 있으면 재사용) --------------------------------------
# 재사용이 기본인 이유: 사용자가 중국에서 기기 3대에 이미 넣어둔 설정을 스크립트
# 재실행 한 번으로 무효화하면 원격에서 복구할 방법이 없다.
if [ "$ROTATE" = "1" ] || [ ! -f "$CONF" ]; then
  UUID="$(cat /proc/sys/kernel/random/uuid)"
  WSPATH="/_ws/$(openssl rand -hex 12)"
  [ "$ROTATE" = "1" ] && log "자격 재발급 — 기존 클라이언트 3대 모두 재설정 필요"
else
  UUID="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["inbounds"][0]["users"][0]["uuid"])' "$CONF")"
  WSPATH="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["inbounds"][0]["transport"]["path"])' "$CONF")"
  log "기존 자격 재사용 (재발급하려면 --rotate)"
fi

# --- 4. sing-box 설정 --------------------------------------------------------
# TLS 블록이 없다 — Caddy 가 이미 끝냈다. listen 은 루프백 고정.
umask 027
cat > "$CONF" <<EOF
{
  "log": { "level": "warn", "timestamp": true },
  "inbounds": [
    {
      "type": "vless",
      "tag": "vless-ws-in",
      "listen": "127.0.0.1",
      "listen_port": $PORT,
      "users": [ { "uuid": "$UUID" } ],
      "transport": { "type": "ws", "path": "$WSPATH" }
    }
  ],
  "outbounds": [ { "type": "direct", "tag": "direct" } ]
}
EOF
chown root:hosub-vpn "$CONF"; chmod 0640 "$CONF"
sing-box check -c "$CONF" || die "sing-box 설정 검증 실패"

# --- 5. 접속 문자열 + QR (0600, 화면에 안 찍는다) ------------------------------
ESCAPED_PATH="$(printf '%s' "$WSPATH" | sed 's|/|%2F|g')"
VLESS="vless://${UUID}@${DOMAIN}:443?encryption=none&security=tls&sni=${DOMAIN}&type=ws&host=${DOMAIN}&path=${ESCAPED_PATH}#hosub-vpn"
printf '%s\n' "$VLESS" > "$LINK"; chmod 0600 "$LINK"
qrencode -t UTF8 -o "$QR_TXT" "$VLESS" 2>/dev/null || true
qrencode -s 8 -o "$QR_PNG" "$VLESS" 2>/dev/null || true
chmod 0600 "$QR_TXT" "$QR_PNG" 2>/dev/null || true

# --- 6. Caddy 에 비밀 경로 주입 -----------------------------------------------
# 경로를 Caddyfile 에 직접 박지 않는다 — Caddyfile 은 깃에 커밋되는 파일이다.
# systemd 드롭인의 환경변수로 넣고 Caddyfile 은 {$HOSUB_VPN_PATH} 로 받는다.
install -d -m 0755 "$DROPIN_DIR"
cat > "$DROPIN" <<EOF
# deploy/vpn-singbox-install.sh 가 생성. 비밀 경로 — 커밋하지 않는다.
[Service]
Environment=HOSUB_VPN_PATH=$WSPATH
EOF
chmod 0600 "$DROPIN"

# --- 7. 유닛 설치 + 기동 ------------------------------------------------------
install -m 0644 "$APP_DIR/deploy/vpn-singbox.service" /etc/systemd/system/vpn-singbox.service
systemctl daemon-reload
systemctl enable --now vpn-singbox
sleep 1
systemctl is-active --quiet vpn-singbox || {
  journalctl -u vpn-singbox --no-pager -n 20 || true
  die "vpn-singbox 가 active 가 아니다"
}
log "vpn-singbox active (127.0.0.1:$PORT)"

# --- 8. Caddyfile 반영 --------------------------------------------------------
# 마커가 없으면 레포 판으로 교체한다. 교체 전 백업하고, validate 실패하면 되돌린다.
if ! grep -q 'HOSUB_VPN_PATH' /etc/caddy/Caddyfile 2>/dev/null; then
  BAK="/etc/caddy/Caddyfile.bak.$(date +%Y%m%d%H%M%S)"
  log "Caddyfile 에 @vpn 블록이 없다 → 레포 판으로 교체 (백업: $BAK)"
  cp /etc/caddy/Caddyfile "$BAK" 2>/dev/null || true
  cp "$APP_DIR/deploy/Caddyfile" /etc/caddy/Caddyfile
  if ! caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
    [ -f "$BAK" ] && cp "$BAK" /etc/caddy/Caddyfile
    die "Caddyfile 검증 실패 — 원복했다. 수동으로 deploy/Caddyfile 차이를 확인할 것"
  fi
fi
# reload 가 아니라 restart 다: 새 환경변수는 기존 프로세스 환경에 안 들어간다.
log "caddy 재시작 (환경변수 주입 — reload 로는 안 된다)"
systemctl restart caddy
sleep 1
systemctl is-active --quiet caddy || die "caddy 가 재시작 후 active 가 아니다"

# --- 9. 검증 -----------------------------------------------------------------
# 경로가 실제로 sing-box 까지 가는지 본다. WS 업그레이드 요청에 101 이 오면 성공.
# (VLESS 인증 전 단계라 UUID 없이도 101 이 뜬다 → 자격이 로그에 안 남는다)
#
# --resolve 로 루프백에 못 박는다. 공개 도메인을 그냥 부르면 공인 IP 로 나갔다가
# 공유기로 되돌아오는데(헤어핀 NAT), iptime 이 이걸 지원하지 않으면 멀쩡한 설정도
# 타임아웃으로 잡힌다. 우리가 볼 것은 "Caddy 가 이 경로를 sing-box 로 보내는가"뿐이다.
#
# `|| true` 인 이유: 101 이 오면 연결이 업그레이드된 채 열려 있어 --max-time 에
# 걸린다(exit 28). 그래도 curl 은 -w 를 찍으므로 코드는 정상적으로 잡힌다.
probe() {
  curl -s -o /dev/null -w '%{http_code}' --max-time 5 --resolve "${DOMAIN}:443:127.0.0.1" "$@" || true
}
code="$(probe -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
  -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
  "https://${DOMAIN}${WSPATH}")"
if [ "$code" = "101" ]; then
  log "검증 통과: 비밀 경로 → sing-box WebSocket 101"
else
  log "경고: 비밀 경로 응답이 ${code:-000} (101 기대). Caddy 경로 매처를 확인할 것."
  log "      caddy 가 HOSUB_VPN_PATH 를 못 받았을 가능성이 크다 → systemctl show caddy -p Environment"
fi
# 엉뚱한 경로는 평범하게 보여야 한다(액티브 프로빙 대비)
dummy="$(probe "https://${DOMAIN}/_ws/$(openssl rand -hex 4)")"
log "위장 확인: 잘못된 경로는 ${dummy:-000} 응답 (VPN 존재가 드러나지 않음)"

echo
log "설치 완료. 접속 정보는 화면에 찍지 않았다 — 아래 파일에서 직접 읽을 것:"
log "  접속 문자열 : $LINK"
log "  QR(터미널)  : sudo cat $QR_TXT      ← 폰 카메라로 스캔"
log "  QR(이미지)  : $QR_PNG"
log "  지문        : sha256 $(sha256sum "$LINK" | cut -c1-16)…  (값 대조용)"
echo
log "클라이언트 앱 설정은 docs/VPN.md 3절을 볼 것."
