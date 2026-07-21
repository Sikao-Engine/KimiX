from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import mcp.types
import pytest
from kosong.tooling import CallableTool, ToolOk

from kimi_cli.mcp.server import (
    MCPKimixServer,
    _is_localhost_host,
    _is_localhost_origin,
    _LocalhostDNSRebindingMiddleware,
)


def _make_fake_runtime(tmp_path: Path) -> Any:
    runtime = MagicMock()
    runtime.session.work_dir = tmp_path
    runtime.llm = None

    class EchoTool(CallableTool):
        def __init__(self) -> None:
            super().__init__(
                name="echo",
                description="Echo the input",
                parameters={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            )

        async def __call__(self, *args: Any, **kwargs: Any) -> ToolOk:
            return ToolOk(output=f"echo: {kwargs.get('text', '')}")

    tool = EchoTool()

    toolset = MagicMock()
    toolset._tool_dict = {"echo": tool}
    toolset._hidden_tools = set()

    agent = MagicMock()
    agent.toolset = toolset
    agent.system_prompt = "You are a helpful assistant."

    runtime.agent = agent
    return runtime, tool


@pytest.mark.asyncio
async def test_server_registers_tools_and_prompts(tmp_path: Path) -> None:
    runtime, _tool = _make_fake_runtime(tmp_path)
    server = MCPKimixServer(runtime, name="test-kimix")

    tools = await server.server.list_tools()
    tool_names = {t.name for t in tools}
    assert "echo" in tool_names

    prompts = await server.server.list_prompts()
    prompt_names = {p.name for p in prompts}
    assert "system" in prompt_names


@pytest.mark.asyncio
async def test_server_registers_project_file_resource(tmp_path: Path) -> None:
    runtime, _tool = _make_fake_runtime(tmp_path)
    (tmp_path / "AGENTS.md").write_text("# Agents", encoding="utf-8")

    server = MCPKimixServer(runtime, name="test-kimix")
    resources = await server.server.list_resources()
    uris = {str(r.uri) for r in resources}
    assert any("AGENTS.md" in uri for uri in uris)


@pytest.mark.asyncio
async def test_system_prompt_has_description(tmp_path: Path) -> None:
    """The exposed ``system`` prompt must carry a description (prompts/list)."""
    runtime, _tool = _make_fake_runtime(tmp_path)
    server = MCPKimixServer(runtime, name="test-kimix")

    prompts = await server.server.list_prompts()
    system_prompt = next(p for p in prompts if p.name == "system")
    assert system_prompt.description


@pytest.mark.asyncio
async def test_static_markdown_resource_is_readable(tmp_path: Path) -> None:
    """Static AGENTS.md/README.md resources must actually serve content."""
    runtime, _tool = _make_fake_runtime(tmp_path)
    (tmp_path / "AGENTS.md").write_text("# Agents", encoding="utf-8")

    server = MCPKimixServer(runtime, name="test-kimix")
    resources = await server.server.list_resources()
    uri = next(str(r.uri) for r in resources if "AGENTS.md" in str(r.uri))

    result = await server.server.read_resource(uri)
    assert not isinstance(result, mcp.types.CreateTaskResult)
    assert result.contents
    content = result.contents[0].content
    assert isinstance(content, str)
    assert "# Agents" in content


@pytest.mark.asyncio
async def test_binary_project_file_returns_blob_content(tmp_path: Path) -> None:
    """Binary files read via ``file:///{path}`` must return blob contents."""
    runtime, _tool = _make_fake_runtime(tmp_path)
    payload = b"\x89PNG\x00\x01\x02\x03"
    (tmp_path / "img.png").write_bytes(payload)

    server = MCPKimixServer(runtime, name="test-kimix")
    result = await server.server.read_resource("file:///img.png")
    assert not isinstance(result, mcp.types.CreateTaskResult)
    assert result.contents
    content = result.contents[0]
    assert content.content == payload
    assert content.mime_type == "image/png"


@pytest.mark.asyncio
async def test_text_project_file_returns_text_content(tmp_path: Path) -> None:
    """Text files read via ``file:///{path}`` must keep returning text."""
    runtime, _tool = _make_fake_runtime(tmp_path)
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")

    server = MCPKimixServer(runtime, name="test-kimix")
    result = await server.server.read_resource("file:///note.txt")
    assert not isinstance(result, mcp.types.CreateTaskResult)
    assert result.contents
    assert result.contents[0].content == "hello"


def test_localhost_host_header_detection() -> None:
    assert _is_localhost_host("127.0.0.1:4097")
    assert _is_localhost_host("localhost")
    assert _is_localhost_host("localhost:80")
    assert _is_localhost_host("[::1]:4097")
    assert not _is_localhost_host("evil.example.com")
    assert not _is_localhost_host("")


def test_localhost_origin_detection() -> None:
    assert _is_localhost_origin("http://127.0.0.1:4097")
    assert _is_localhost_origin("http://localhost:3000")
    assert _is_localhost_origin("https://[::1]:8443")
    assert not _is_localhost_origin("http://evil.example.com")
    assert not _is_localhost_origin("not a url")


@pytest.mark.asyncio
async def test_dns_rebinding_middleware_rejects_and_allows() -> None:
    """The middleware must reject foreign Host/Origin and allow localhost."""
    from starlette.responses import PlainTextResponse

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await PlainTextResponse("ok")(scope, receive, send)

    middleware = _LocalhostDNSRebindingMiddleware(app)

    async def call(headers: list[tuple[bytes, bytes]]) -> int:
        status = 0

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "headers": headers,
        }
        await middleware(scope, receive, send)
        return status

    evil_headers = [
        (b"host", b"evil.example.com"),
        (b"origin", b"http://evil.example.com"),
    ]
    assert await call(evil_headers) == 403

    good_headers = [
        (b"host", b"127.0.0.1:4097"),
        (b"origin", b"http://127.0.0.1:4097"),
    ]
    assert await call(good_headers) == 200

    # Foreign Origin with localhost Host is still rejected (browser attack).
    mixed_headers = [
        (b"host", b"127.0.0.1:4097"),
        (b"origin", b"http://evil.example.com"),
    ]
    assert await call(mixed_headers) == 403
