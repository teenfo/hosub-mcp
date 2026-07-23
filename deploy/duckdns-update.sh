#!/usr/bin/env bash
# DuckDNS IP 자동 갱신.
#
# iptime.org 는 CAA 레코드로 인증서 발급이 막혀 있어(CAA 0 issue ";"),
# 같은 집 공인 IP를 가리키는 DuckDNS 서브도메인으로 HTTPS 인증서를 받는다.
# 이 스크립트는 DuckDNS 에 현재 공인 IP를 등록/갱신한다.
# duckdns-update.timer 가 주기적으로 호출한다.
#
# 필요한 환경변수 (.env):
#   DUCKDNS_DOMAIN  서브도메인만 (예: hosub  → hosub.duckdns.org)
#   DUCKDNS_TOKEN   DuckDNS 토큰 (비밀)
set -euo pipefail

DOMAIN="${DUCKDNS_DOMAIN:-}"
TOKEN="${DUCKDNS_TOKEN:-}"

if [ -z "$DOMAIN" ] || [ -z "$TOKEN" ]; then
  echo "[duckdns] DUCKDNS_DOMAIN / DUCKDNS_TOKEN 가 .env 에 설정되지 않았습니다." >&2
  exit 1
fi

# ip 파라미터를 비우면 DuckDNS 가 요청 출발지 IP(집 공인 IP)로 자동 설정한다.
resp="$(curl -fsS "https://www.duckdns.org/update?domains=${DOMAIN}&token=${TOKEN}&ip=")" || {
  echo "[duckdns] 업데이트 요청 실패 (네트워크/토큰 확인)" >&2
  exit 1
}

echo "[duckdns] ${DOMAIN}.duckdns.org -> ${resp}"
if [ "$resp" != "OK" ]; then
  echo "[duckdns] DuckDNS 응답이 OK 가 아님 (도메인/토큰 확인)" >&2
  exit 1
fi
