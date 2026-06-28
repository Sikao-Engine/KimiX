from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import mcp.types

SamplingDelegate = Callable[
    [list[mcp.types.SamplingMessage], mcp.types.CreateMessageRequestParams],
    Awaitable[mcp.types.CreateMessageResult],
]


class MCPSamplingHandler:
    """Handler for MCP ``sampling/createMessage`` requests."""

    def __init__(
        self,
        delegate: SamplingDelegate | None = None,
        *,
        model: str = "kimi",
    ) -> None:
        self._delegate = delegate
        self._model = model

    @property
    def capabilities(self) -> mcp.types.SamplingCapability:
        """Return the sampling capabilities advertised by the client."""
        return mcp.types.SamplingCapability()

    def set_delegate(self, delegate: SamplingDelegate) -> None:
        """Set the delegate that will actually perform sampling."""
        self._delegate = delegate

    def to_fastmcp_sampling_handler(self) -> Any:
        """Return a handler compatible with fastmcp's ``sampling_handler`` argument."""
        return self._handle

    async def _handle(
        self,
        messages: list[mcp.types.SamplingMessage],
        params: mcp.types.CreateMessageRequestParams,
        ctx: Any,
    ) -> mcp.types.CreateMessageResult:
        if self._delegate is not None:
            return await self._delegate(messages, params)

        # Default fallback: concatenate text content and return it as assistant text.
        # This avoids crashing when a server requests sampling but no runtime is wired.
        text_parts: list[str] = []
        for message in messages:
            content = message.content
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, mcp.types.TextContent):
                        text_parts.append(block.text)
            elif isinstance(content, mcp.types.TextContent):
                text_parts.append(content.text)

        response_text = (
            "Sampling is not configured in this Kimix runtime. "
            "Please provide a sampling delegate when constructing MCPSamplingHandler."
        )
        if text_parts:
            response_text = "\n".join(text_parts)

        return mcp.types.CreateMessageResult(
            role="assistant",
            content=mcp.types.TextContent(type="text", text=response_text),
            model=self._model,
            stopReason="endTurn",
        )
