"""ASGI 진입점. uvicorn src.asgi:app 로 기동한다.

환경변수에서 설정을 읽어 앱을 조립한다. 필수 시크릿이 없으면 즉시 기동 거부.
"""

from __future__ import annotations

import os

from .audit import AuditLog
from .oauth import OAuthStore
from .registry import Registry
from .runner import SubprocessRunner
from .server import build_app

_MIN_TOKEN_LEN = 32


def _require(name: str, *, min_len: int = 0) -> str:
    val = os.environ.get(name, "")
    if not val:
        raise RuntimeError(f"환경변수 {name} 가 설정되지 않았습니다.")
    if min_len and len(val) < min_len:
        raise RuntimeError(f"환경변수 {name} 는 최소 {min_len}자 이상이어야 합니다.")
    return val


def create_app():
    token = _require("HOSUB_MCP_TOKEN", min_len=_MIN_TOKEN_LEN)
    dash_password = _require("HOSUB_DASH_PASSWORD")
    session_secret = _require("HOSUB_SESSION_SECRET", min_len=16)

    registry_path = os.environ.get("HOSUB_MCP_REGISTRY", "config/registry.yaml")
    db_path = os.environ.get("HOSUB_MCP_DB", "data/audit.db")
    strict = os.environ.get("HOSUB_MCP_STRICT", "false").lower() in ("1", "true", "yes")

    allowed_hosts_raw = os.environ.get("HOSUB_ALLOWED_HOSTS", "").strip()
    allowed_hosts = (
        [h.strip() for h in allowed_hosts_raw.split(",") if h.strip()]
        if allowed_hosts_raw
        else None
    )

    oauth_db = os.environ.get("HOSUB_OAUTH_DB", "data/oauth.db")
    public_url = os.environ.get("HOSUB_PUBLIC_URL", "").strip() or None

    registry = Registry.load(registry_path, strict=strict)
    audit = AuditLog(db_path)
    runner = SubprocessRunner()
    oauth_store = OAuthStore(oauth_db)

    return build_app(
        registry=registry,
        runner=runner,
        audit=audit,
        mcp_token=token,
        dash_password=dash_password,
        session_secret=session_secret,
        allowed_hosts=allowed_hosts,
        oauth_store=oauth_store,
        public_url=public_url,
    )


app = create_app()
