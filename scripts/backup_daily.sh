#!/usr/bin/env bash
# 일일 백업 — 모든 운영 데이터를 /backup 에 남긴다 (매일 04:30 KST, systemd 타이머).
#
# 대상: TNM Postgres(pg_dump) · 트레이딩 SQLite · MCP 감사/OAuth · 게이트웨이 잡 ·
#       설정(.env 제외) . 보관 14일.
# SQLite 는 .backup 명령(온라인 백업)으로 뜬다 — 서비스 정지 불필요.
set -uo pipefail

DEST="${BACKUP_DIR:-/backup}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
STAMP="$(date +%Y%m%d)"
OUT="$DEST/daily/$STAMP"
LOG_TAG="backup_daily"
ok=0; fail=0

log() { echo "[$(date '+%F %T')] $*"; }

mkdir -p "$OUT" || { log "백업 디렉터리 생성 실패: $OUT"; exit 1; }

# --- 1) TNM Postgres (도커 컨테이너) ---
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^tnm-postgres$'; then
  if docker exec tnm-postgres pg_dump -U tnm -d tnm --no-owner 2>/dev/null \
      | gzip > "$OUT/tnm_pg.sql.gz" && [ -s "$OUT/tnm_pg.sql.gz" ]; then
    log "TNM Postgres 덤프 OK ($(du -h "$OUT/tnm_pg.sql.gz" | cut -f1))"; ok=$((ok+1))
  else
    log "TNM Postgres 덤프 실패"; rm -f "$OUT/tnm_pg.sql.gz"; fail=$((fail+1))
  fi
else
  log "tnm-postgres 컨테이너 없음 — 건너뜀"
fi

# --- 2) SQLite 파일들 (온라인 백업) ---
backup_sqlite() {   # $1=원본 경로, $2=대상 파일명
  local src="$1" name="$2"
  [ -f "$src" ] || { log "없음(건너뜀): $src"; return 0; }
  if sqlite3 "$src" ".backup '$OUT/$name'" 2>/dev/null && [ -s "$OUT/$name" ]; then
    gzip -f "$OUT/$name"
    log "SQLite OK: $name ($(du -h "$OUT/$name.gz" | cut -f1))"; ok=$((ok+1))
  else
    log "SQLite 실패: $src"; rm -f "$OUT/$name"; fail=$((fail+1))
  fi
}

backup_sqlite /data/trading/trading.db      trading.db
backup_sqlite /data/trading/market.db       market.db
backup_sqlite /opt/hosub-mcp/data/audit.db  mcp_audit.db
backup_sqlite /opt/hosub-mcp/data/oauth.db  mcp_oauth.db
backup_sqlite /data/llm-gateway/llmgw.db    llmgw.db

# --- 3) 런타임 설정(시크릿 제외) ---
tar czf "$OUT/configs.tar.gz" \
  --exclude='*.env' --exclude='.env' \
  -C / \
  data/trading/risk.json data/trading/rules.json data/trading/rule_sweep.json \
  data/trading/night_bias.json \
  opt/hosub-mcp/config/registry.yaml \
  opt/hosub-mcp/llm-gateway/config \
  opt/hosub-trading/trading/config.yaml \
  opt/hosub-mcp/tnm/config.yaml 2>/dev/null \
  && log "설정 아카이브 OK" && ok=$((ok+1))

# --- 4) 보관 기간 정리 ---
find "$DEST/daily" -maxdepth 1 -type d -mtime "+$KEEP_DAYS" -exec rm -rf {} + 2>/dev/null

TOTAL="$(du -sh "$OUT" 2>/dev/null | cut -f1)"
log "완료: 성공 $ok · 실패 $fail · 크기 $TOTAL · 경로 $OUT"
[ "$fail" -eq 0 ] || exit 1
