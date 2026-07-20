from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

import mcp.types
from kosong.message import ContentPart
from pydantic import AnyUrl

from kimi_cli.mcp.resources import MCPResourceManager
from kimi_cli.mcp.roots import MCPRootsHandler
from kimi_cli.mcp.sampling import MCPSamplingHandler

if TYPE_CHECKING:
    import fastmcp
    from fastmcp.client.client import CallToolResult
    from fastmcp.mcp_config import MCPConfig


class MCPClient:
    """Thin wrapper around ``fastmcp.Client`` for Kimix MCP client operations.

    The wrapper exists to isolate Kimix from future fastmcp API changes and to
    centralize lifecycle handling (roots, sampling, timeout) in one place.
    """

    def __init__(
        self,
        config: MCPConfig | dict[str, Any],
        *,
        name: str | None = None,
        timeout_ms: int = 60000,
        roots_handler: MCPRootsHandler | None = None,
        sampling_handler: MCPSamplingHandler | None = None,
    ) -> None:
        import fastmcp

        self._name = name
        self._timeout = timedelta(milliseconds=timeout_ms)
        self._roots_handler = roots_handler
        self._sampling_handler = sampling_handler

        roots: Any = None
        if roots_handler is not None:
            roots = roots_handler.to_fastmcp_roots()

        sampling: Any = None
        sampling_capabilities: mcp.types.SamplingCapability | None = None
        if sampling_handler is not None:
            sampling = sampling_handler.to_fastmcp_sampling_handler()
            sampling_capabilities = sampling_handler.capabilities

        self._client: fastmcp.Client[Any] = fastmcp.Client(
            config,
            name=name,
            timeout=self._timeout,
            roots=roots,
            sampling_handler=sampling,
            sampling_capabilities=sampling_capabilities,
        )

    @property
    def inner(self) -> fastmcp.Client[Any]:
        """Access the underlying ``fastmcp.Client``."""
        return self._client

    async def __aenter__(self) -> fastmcp.Client[Any]:
        return await self._client.__aenter__()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self._client.__aexit__(exc_type, exc, tb)

    async def close(self) -> None:
        await self._client.close()

    async def list_tools(self) -> list[mcp.types.Tool]:
        async with self._client as client:
            return await client.list_tools()

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout_ms: int | None = None,
    ) -> CallToolResult:
        timeout = (
            timedelta(milliseconds=timeout_ms) if timeout_ms is not None else self._timeout
        )
        async with self._client as client:
            return await client.call_tool(
                name,
                arguments,
                timeout=timeout,
                raise_on_error=False,
            )

    async def list_resources(
        self, cursor: str | None = None
    ) -> tuple[list[mcp.types.Resource], str | None]:
        async with self._client as client:
            result = await client.list_resources_mcp(cursor=cursor)
            next_cursor = result.nextCursor if result.nextCursor else None
            return result.resources, next_cursor

    async def list_resource_templates(self) -> list[mcp.types.ResourceTemplate]:
        async with self._client as client:
            return await client.list_resource_templates()

    async def read_resource(self, uri: str) -> list[ContentPart]:
        async with self._client as client:
            contents = await client.read_resource(uri)
            return MCPResourceManager.convert_contents(contents)

    async def subscribe_resource(self, uri: str) -> None:
        async with self._client as client:
            await client.session.subscribe_resource(AnyUrl(uri))

    async def unsubscribe_resource(self, uri: str) -> None:
        async with self._client as client:
            await client.session.unsubscribe_resource(AnyUrl(uri))

    async def list_prompts(self) -> list[mcp.types.Prompt]:
        async with self._client as client:
            return await client.list_prompts()

    async def get_prompt(
        self, name: str, arguments: dict[str, str] | None = None
    ) -> list[mcp.types.PromptMessage]:
        async with self._client as client:
            result = await client.get_prompt(name, arguments=arguments)
            return result.messages

    async def send_roots_list_changed(self) -> None:
        async with self._client as client:
            await client.session.send_roots_list_changed()

    def set_roots_handler(self, handler: MCPRootsHandler) -> None:
        self._roots_handler = handler
        self._client.set_roots(handler.to_fastmcp_roots())

    def set_sampling_handler(self, handler: MCPSamplingHandler) -> None:
        self._sampling_handler = handler
        self._client.set_sampling_callback(
            handler.to_fastmcp_sampling_handler(),
            handler.capabilities,
        )

    @property
    def name(self) -> str | None:
        return self._name

    @property
    def timeout_ms(self) -> int:
        return int(self._timeout.total_seconds() * 1000)
