from __future__ import annotations

import base64
import inspect
import mimetypes
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import mcp.types
from fastmcp import FastMCP
from fastmcp.resources import Resource as FastMCPResource
from fastmcp.tools.function_tool import FunctionTool
from kosong.message import (
    AudioURLPart,
    ContentPart,
    ImageURLPart,
    TextPart,
    ThinkPart,
    ToolCallPart,
    VideoURLPart,
)
from kosong.tooling import CallableTool, CallableTool2, ToolError, ToolOk, ToolReturnValue

from kimi_cli.mcp.sampling import MCPSamplingHandler

if TYPE_CHECKING:
    from kimi_cli.soul.agent import Runtime


def _sanitize_param_name(name: str) -> str:
    """Convert a JSON schema property name into a valid Python identifier."""
    sanitized = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in name)
    sanitized = sanitized.lstrip("0123456789")
    if not sanitized or not sanitized[0].isalpha():
        sanitized = "param_" + sanitized
    return sanitized


def _tool_result_to_text(return_value: ToolReturnValue) -> str:
    """Flatten a kosong tool return value into a plain text string."""
    parts: list[ContentPart]
    if isinstance(return_value, ToolOk | ToolError):
        output = return_value.output
        if isinstance(output, list):
            parts = output
        elif isinstance(output, TextPart):
            parts = [output]
        else:
            parts = [TextPart(text=str(output))]
    else:
        return str(return_value)

    lines: list[str] = []
    for part in parts:
        if isinstance(part, TextPart):
            lines.append(part.text)
        elif isinstance(part, ThinkPart):
            lines.append(f"<thinking>\n{part.think}\n</thinking>")
        elif isinstance(part, ImageURLPart):
            lines.append(f"[image: {part.image_url.url[:80]}...]")
        elif isinstance(part, AudioURLPart):
            lines.append(f"[audio: {part.audio_url.url[:80]}...]")
        elif isinstance(part, VideoURLPart):
            lines.append(f"[video: {part.video_url.url[:80]}...]")
        elif isinstance(part, ToolCallPart):
            lines.append(f"[tool call: {part.arguments_part}]")
        else:
            lines.append(f"[{part.type}]")
    return "\n".join(lines)


class MCPKimixServer:
    """Factory that exposes a Kimix runtime as an MCP server."""

    def __init__(
        self,
        runtime: Runtime,
        *,
        agent: Any | None = None,
        name: str = "kimix",
        instructions: str | None = None,
        include_resources: bool = True,
        include_prompts: bool = True,
    ) -> None:
        self._runtime = runtime
        self._agent = agent if agent is not None else getattr(runtime, "agent", None)
        self._name = name
        self._instructions = instructions
        self._include_resources = include_resources
        self._include_prompts = include_prompts

        sampling_handler = MCPSamplingHandler(delegate=self._sampling_delegate)
        self._server = FastMCP(
            name=name,
            instructions=instructions or "Kimix agent runtime exposed over MCP.",
            sampling_handler=sampling_handler.to_fastmcp_sampling_handler(),
        )

        self._register_tools()
        if include_resources:
            self._register_resources()
        if include_prompts:
            self._register_prompts()

    @property
    def server(self) -> FastMCP:
        return self._server

    def _register_tools(self) -> None:
        if self._agent is None:
            return
        toolset = getattr(self._agent, "toolset", None)
        if toolset is None:
            return

        # Use the internal callable-tool registry so we can invoke tools directly.
        hidden_tools = getattr(toolset, "_hidden_tools", set())
        for tool in getattr(toolset, "_tool_dict", {}).values():
            base = getattr(tool, "base", None)
            if base is None:
                continue
            if base.name in hidden_tools:
                continue
            self._add_tool(tool)

    def _add_tool(self, tool: CallableTool | CallableTool2[Any]) -> None:
        """Add a kosong tool to the FastMCP server."""
        base = tool.base
        schema = base.parameters or {}
        description = base.description or f"Tool {base.name}"
        tool_name = base.name

        async def _execute(**kwargs: Any) -> str:
            try:
                result = await tool.call(kwargs)
            except Exception as exc:
                return f"Error calling tool {tool_name}: {exc}"
            return _tool_result_to_text(result)

        # Preserve tool name inside closure
        _execute.__name__ = tool_name
        _execute.__doc__ = description

        # Build a synthetic signature so FunctionTool does not reject **kwargs.
        # Sanitize property names so they are valid Python identifiers; the
        # original names are restored inside _execute via the name map.
        params: list[inspect.Parameter] = []
        properties = dict(schema.get("properties", {}))
        required = set(schema.get("required", []))
        name_map: dict[str, str] = {}
        sanitized_properties: dict[str, Any] = {}
        for original_name, prop_schema in properties.items():
            sanitized = _sanitize_param_name(original_name)
            name_map[sanitized] = original_name
            sanitized_properties[sanitized] = prop_schema
            params.append(
                inspect.Parameter(
                    sanitized,
                    inspect.Parameter.KEYWORD_ONLY,
                    default=inspect.Parameter.empty if original_name in required else None,
                )
            )

        async def _execute_with_map(**kwargs: Any) -> str:
            original_kwargs = {name_map.get(k, k): v for k, v in kwargs.items()}
            return await _execute(**original_kwargs)

        _execute_with_map.__name__ = tool_name
        _execute_with_map.__doc__ = description
        _execute_with_map.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
        _execute_with_map.__annotations__ = {}

        fn_tool = FunctionTool.from_function(
            _execute_with_map,
            name=tool_name,
            description=description,
        )
        fn_tool.parameters = {
            **schema,
            "properties": sanitized_properties,
            "required": [_sanitize_param_name(r) for r in schema.get("required", [])],
        }
        self._server.add_tool(fn_tool)

    def _register_resources(self) -> None:
        work_dir = Path(str(self._runtime.session.work_dir))

        # Static resources for AGENTS.md and README.md
        for filename in ("AGENTS.md", "README.md"):
            path = work_dir / filename
            if path.exists():
                from pydantic import AnyUrl

                self._server.add_resource(
                    FastMCPResource(
                        uri=AnyUrl(f"file://{path.resolve().as_posix()}"),
                        name=filename,
                        mime_type="text/markdown",
                    )
                )

        # Template resource for project files
        @self._server.resource("file:///{path}", mime_type="application/octet-stream")
        async def project_file(path: str) -> str | bytes:
            file_path = work_dir / PurePosixPath(path)
            file_path = file_path.resolve()
            if not str(file_path).startswith(str(work_dir.resolve())):
                return "Access denied: path is outside the workspace."
            if not file_path.exists():
                return f"File not found: {path}"
            mime_type, _ = mimetypes.guess_type(str(file_path))
            mime_type = mime_type or "application/octet-stream"
            try:
                if mime_type.startswith("text/") or mime_type in (
                    "application/json",
                    "application/javascript",
                    "application/xml",
                ):
                    return file_path.read_text(encoding="utf-8")
                data = file_path.read_bytes()
                return f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"
            except Exception as exc:
                return f"Error reading file: {exc}"

    def _register_prompts(self) -> None:
        if self._agent is None:
            return
        system_prompt = getattr(self._agent, "system_prompt", None)
        if not system_prompt:
            return

        @self._server.prompt("system")
        async def system_prompt_template() -> str:
            return system_prompt

    async def _sampling_delegate(
        self,
        messages: list[mcp.types.SamplingMessage],
        params: mcp.types.CreateMessageRequestParams,
    ) -> mcp.types.CreateMessageResult:
        """Delegate sampling requests to the runtime LLM."""
        llm = self._runtime.llm
        if llm is None:
            return mcp.types.CreateMessageResult(
                role="assistant",
                content=mcp.types.TextContent(
                    type="text",
                    text="No LLM is configured in this Kimix runtime.",
                ),
                model="none",
                stopReason="endTurn",
            )

        # Simple text-only sampling fallback. Full multi-turn sampling via the
        # runtime LLM requires mapping SamplingMessage objects to kosong messages
        # and is left for a future iteration.
        texts: list[str] = []
        for message in messages:
            content = message.content
            blocks = content if isinstance(content, list) else [content]
            for block in blocks:
                if isinstance(block, mcp.types.TextContent):
                    texts.append(f"{message.role}: {block.text}")

        prompt = "\n".join(texts)
        try:
            response = await self._runtime_llm_complete(prompt)
        except Exception as exc:
            return mcp.types.CreateMessageResult(
                role="assistant",
                content=mcp.types.TextContent(
                    type="text",
                    text=f"Sampling failed: {exc}",
                ),
                model=llm.model_name or "unknown",
                stopReason="endTurn",
            )

        return mcp.types.CreateMessageResult(
            role="assistant",
            content=mcp.types.TextContent(type="text", text=response),
            model=llm.model_name or "unknown",
            stopReason="endTurn",
        )

    async def _runtime_llm_complete(self, prompt: str) -> str:
        """Best-effort single-turn completion using the runtime chat provider."""
        llm = self._runtime.llm
        if llm is None:
            return ""
        chat_provider = llm.chat_provider
        from kosong.message import Message, TextPart

        messages = [
            Message(role="user", content=[TextPart(text=prompt)]),
        ]
        try:
            streamed = await chat_provider.generate(
                system_prompt="",
                tools=[],
                history=messages,
            )
            # Drain async generator
            response_text = ""
            async for chunk in streamed:
                text = getattr(chunk, "text", None)
                if text:
                    response_text += text
            return response_text
        except Exception:
            # Fallback for non-streaming or different interface
            raise


async def serve_stdio(runtime: Runtime, **server_options: Any) -> None:
    """Run the Kimix MCP server over stdio."""
    server = MCPKimixServer(runtime, **server_options).server
    await server.run_stdio_async()


async def serve_http(
    runtime: Runtime,
    *,
    host: str = "127.0.0.1",
    port: int = 4097,
    **server_options: Any,
) -> None:
    """Run the Kimix MCP server over streamable HTTP."""
    server = MCPKimixServer(runtime, **server_options).server
    await server.run_http_async(host=host, port=port)
