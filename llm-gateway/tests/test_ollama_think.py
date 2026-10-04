"""OllamaClient.generate — 역할 options 의 think 를 Ollama 최상위 필드로 올린다."""

from __future__ import annotations

import json

import httpx
import pytest

from app.ollama import OllamaClient


def _client(seen: list[dict]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"response": "ok", "eval_count": 1})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_think_moves_to_top_level():
    seen: list[dict] = []
    async with _client(seen) as c:
        res = await OllamaClient("http://mac.test:11434").generate(
            model="qwen3.5:27b", prompt="p", options={"think": False, "temperature": 0.4}, client=c,
        )
    assert res.response == "ok"
    assert seen[0]["think"] is False
    assert seen[0]["options"] == {"temperature": 0.4}


@pytest.mark.asyncio
async def test_no_think_key_when_absent():
    seen: list[dict] = []
    async with _client(seen) as c:
        await OllamaClient("http://mac.test:11434").generate(
            model="qwen2.5:32b", prompt="p", options={"temperature": 0.3}, client=c,
        )
    assert "think" not in seen[0]
    assert seen[0]["options"] == {"temperature": 0.3}


@pytest.mark.asyncio
async def test_only_think_sends_no_empty_options():
    seen: list[dict] = []
    async with _client(seen) as c:
        await OllamaClient("http://mac.test:11434").generate(
            model="qwen3.5:27b", prompt="p", options={"think": False}, client=c,
        )
    assert seen[0]["think"] is False
    assert "options" not in seen[0]
