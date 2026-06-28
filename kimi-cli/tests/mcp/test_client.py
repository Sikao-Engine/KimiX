from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import mcp.types
import pytest
from kosong.message import TextPart
from pydantic import AnyUrl

from kimi_cli.mcp.client import MCPClient


def _make_fake_fastmcp_client() -> Any:
    client = MagicMock()
    client.list_tools = AsyncMock(return_value=[])
    client.list_resources_mcp = AsyncMock(return_value=MagicMock(resources=[], nextCursor=None))
    client.list_resource_templates = AsyncMock(return_value=[])
    client.read_resource = AsyncMock(return_value=[])
    client.get_prompt = AsyncMock(return_value=MagicMock(messages=[]))
    client.list_prompts = AsyncMock(return_value=[])
    client.call_tool = AsyncMock(return_value=MagicMock(content=[], is_error=False))
    client.session = MagicMock()
    client.session.subscribe_resource = AsyncMock()
    client.session.unsubscribe_resource = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


@pytest.mark.asyncio
async def test_mcp_client_list_tools_delegates_to_inner(monkeypatch) -> None:
    fake_inner = _make_fake_fastmcp_client()

    def _fake_fastmcp_client(config, **kwargs):
        return fake_inner

    monkeypatch.setattr("fastmcp.Client", _fake_fastmcp_client)

    client = MCPClient({"mcpServers": {"test": {"command": "echo"}}}, name="test")
    tools = await client.list_tools()
    assert tools == []
    fake_inner.list_tools.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_client_call_tool_passes_timeout(monkeypatch) -> None:
    fake_inner = _make_fake_fastmcp_client()

    def _fake_fastmcp_client(config, **kwargs):
        return fake_inner

    monkeypatch.setattr("fastmcp.Client", _fake_fastmcp_client)

    client = MCPClient({"mcpServers": {"test": {"command": "echo"}}}, timeout_ms=5000)
    result = await client.call_tool("ping", {"text": "hi"})
    assert result is not None
    fake_inner.call_tool.assert_awaited_once()
    _, kwargs = fake_inner.call_tool.call_args
    assert kwargs["timeout"] == timedelta(milliseconds=5000)


@pytest.mark.asyncio
async def test_mcp_client_read_resource_returns_content_parts(monkeypatch) -> None:
    fake_inner = _make_fake_fastmcp_client()
    fake_inner.read_resource = AsyncMock(
        return_value=[
            mcp.types.TextResourceContents(
                uri=AnyUrl("file:///test.txt"),
                mimeType="text/plain",
                text="hello",
            )
        ]
    )

    def _fake_fastmcp_client(config, **kwargs):
        return fake_inner

    monkeypatch.setattr("fastmcp.Client", _fake_fastmcp_client)

    client = MCPClient({"mcpServers": {"test": {"command": "echo"}}})
    parts = await client.read_resource("file:///test.txt")
    assert len(parts) == 1
    assert isinstance(parts[0], TextPart)
    assert parts[0].text == "hello"
