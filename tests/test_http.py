"""HTTP 스모크 테스트: Bearer 인증 + MCP 초기화 + 도구 목록 + 승인 흐름.

실제 uvicorn 서버를 ephemeral 포트에 띄우고 공식 MCP 클라이언트로 접속한다.
FakeRunner 를 주입하므로 systemd 없이도 동작한다.
"""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import time

import httpx
import pytest
import uvicorn

from src.audit import AuditLog
from src.registry import Registry
from src.server import build_app
from tests.conftest import FakeRunner

TOKEN = "t" * 40
REG = {
    "services": {"ollama": {"unit": "ollama.service"}},
    "scripts": {"daily_backup": {"path": "/opt/x.sh"}},
    "backup_script": "daily_backup",
}


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def server():
    app = build_app(
        registry=Registry.from_dict(REG),
        runner=FakeRunner(),
        audit=AuditLog(tempfile.mktemp(suffix=".db")),
        mcp_token=TOKEN,
        dash_password="pw",
        session_secret="session-secret-000",
    )
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    # 기동 대기
    deadline = time.time() + 10
    while not srv.started and time.time() < deadline:
        time.sleep(0.05)
    assert srv.started, "server did not start"
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=5)


def test_missing_token_401(server):
    r = httpx.post(f"{server}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401


def test_wrong_token_401(server):
    r = httpx.post(
        f"{server}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert r.status_code == 401


@pytest.mark.anyio
async def test_mcp_session(server):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with streamablehttp_client(f"{server}/mcp", headers=headers) as (
        read,
        write,
        _,
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert len(names) == 13
            assert {"run_command", "write_file", "get_system_status"} <= names

            # Medium/High 도구는 confirm 없이 승인 요청 반환
            payload = _payload(
                await session.call_tool("restart_service", {"service_name": "ollama"})
            )
            assert payload["status"] == "approval_required"
            assert payload["risk"] == "medium"

            rc = _payload(await session.call_tool("run_command", {"command": "echo hi"}))
            assert rc["status"] == "approval_required"
            assert rc["risk"] == "high"


def _payload(result) -> dict:
    """도구 결과에서 JSON 페이로드를 추출 (structuredContent 우선, 없으면 text 파싱)."""
    if result.structuredContent:
        return result.structuredContent
    return json.loads(result.content[0].text)


@pytest.fixture
def anyio_backend():
    return "asyncio"
