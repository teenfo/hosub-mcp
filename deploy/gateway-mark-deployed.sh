#!/usr/bin/env bash
# 현재 llm-gateway/ 트리 해시를 "배포됨" 표시로 기록한다.
#
# 드리프트 감지의 기준점. git rev-parse HEAD:llm-gateway 는 그 디렉터리의 트리
# 오브젝트 해시라, llm-gateway/ 아래 무엇이든 바뀌면 값이 달라진다. 커밋 해시로
# 비교하면 무관한 커밋에도 드리프트로 잡히므로 트리 해시를 쓴다.
set -euo pipefail
APP_DIR="${HOSUB_MCP_APP_DIR:-/opt/hosub-mcp}"
git -C "$APP_DIR" rev-parse HEAD:llm-gateway > "$APP_DIR/llm-gateway/.deployed-tree"
chmod 0644 "$APP_DIR/llm-gateway/.deployed-tree" 2>/dev/null || true
