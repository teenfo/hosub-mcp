"""대시보드 전용 ASGI 진입점. uvicorn src.asgi_dash:app --host 127.0.0.1 --port 8701.

MCP 서버(src.asgi:app, :8700)와 프로세스를 분리해, 대시보드(웹 UI·정적 자산·
트레이딩 프록시)를 배포·재시작해도 MCP 세션이 끊기지 않게 한다.
Caddy 가 /login·/static·/api 등 대시보드 경로만 이 프로세스로 라우팅한다.
"""

from __future__ import annotations

import os

from .audit import AuditLog
from .registry import Registry
from .runner import SubprocessRunner
from .server import build_dash_app


def _require(name: str, *, min_len: int = 0) -> str:
    # src.asgi 의 것과 동일 — asgi 모듈은 임포트 시점에 MCP 앱을 만들므로
    # 여기서 임포트하지 않고 복제해 둔다.
    val = os.environ.get(name, "")
    if not val:
        raise RuntimeError(f"환경변수 {name} 가 설정되지 않았습니다.")
    if min_len and len(val) < min_len:
        raise RuntimeError(f"환경변수 {name} 는 최소 {min_len}자 이상이어야 합니다.")
    return val


def create_app():
    dash_password = _require("HOSUB_DASH_PASSWORD")
    session_secret = _require("HOSUB_SESSION_SECRET", min_len=16)

    registry_path = os.environ.get("HOSUB_MCP_REGISTRY", "config/registry.yaml")
    db_path = os.environ.get("HOSUB_MCP_DB", "data/audit.db")
    strict = os.environ.get("HOSUB_MCP_STRICT", "false").lower() in ("1", "true", "yes")

    registry = Registry.load(registry_path, strict=strict)
    audit = AuditLog(db_path)   # MCP 프로세스와 같은 SQLite 파일 공유
    runner = SubprocessRunner()

    return build_dash_app(
        registry=registry,
        runner=runner,
        audit=audit,
        dash_password=dash_password,
        session_secret=session_secret,
    )


app = create_app()
