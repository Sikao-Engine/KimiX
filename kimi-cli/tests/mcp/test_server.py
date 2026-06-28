from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from kosong.tooling import CallableTool, ToolOk

from kimi_cli.mcp.server import MCPKimixServer


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
