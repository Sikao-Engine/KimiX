from __future__ import annotations

from typing import Any

import mcp.types
from kosong.message import ContentPart, TextPart
from kosong.tooling.mcp import convert_mcp_content

from kimi_cli import logger


class MCPPromptManager:
    """Manager for MCP prompt discovery and retrieval."""

    @staticmethod
    def convert_messages(messages: list[mcp.types.PromptMessage]) -> list[ContentPart]:
        """Convert MCP prompt messages to Kimi ``ContentPart`` objects.

        The role of each message is ignored; callers should prepend the returned
        parts to the system prompt or turn context as appropriate.
        """
        parts: list[ContentPart] = []
        for message in messages:
            try:
                part = convert_mcp_content(message.content)
            except ValueError as exc:
                logger.warning("Skipping unsupported MCP prompt content: {error}", error=exc)
                part = TextPart(text=f"[Unsupported prompt content: {exc}]")
            parts.append(part)
        return parts

    @staticmethod
    async def list_prompts(client: Any) -> list[mcp.types.Prompt]:
        """List prompts from an MCP server."""
        return await client.list_prompts()

    @staticmethod
    async def get_prompt(
        client: Any,
        name: str,
        arguments: dict[str, str] | None = None,
    ) -> list[mcp.types.PromptMessage]:
        """Get a prompt from an MCP server."""
        result = await client.get_prompt(name, arguments=arguments)
        return result.messages
