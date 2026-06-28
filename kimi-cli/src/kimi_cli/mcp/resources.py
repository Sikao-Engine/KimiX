from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import mcp.types
from kosong.message import (
    AudioURLPart,
    ContentPart,
    ImageURLPart,
    TextPart,
    VideoURLPart,
)

from kimi_cli import logger


class MCPResourceManager:
    """Manager for MCP resource content conversion and operations."""

    @staticmethod
    def convert_contents(contents: Sequence[mcp.types.ResourceContents]) -> list[ContentPart]:
        """Convert MCP resource contents to Kimi ``ContentPart`` objects."""
        parts: list[ContentPart] = []
        for content in contents:
            try:
                part = MCPResourceManager._convert_content(content)
            except ValueError as exc:
                logger.warning("Skipping unsupported MCP resource content: {error}", error=exc)
                part = TextPart(text=f"[Unsupported resource content: {exc}]")
            parts.append(part)
        return parts

    @staticmethod
    def _convert_content(content: mcp.types.ResourceContents) -> ContentPart:
        if isinstance(content, mcp.types.TextResourceContents):
            return TextPart(text=content.text)

        if isinstance(content, mcp.types.BlobResourceContents):
            mime_type = content.mimeType or "application/octet-stream"
            data_uri = f"data:{mime_type};base64,{content.blob}"
            if mime_type.startswith("image/"):
                return ImageURLPart(
                    image_url=ImageURLPart.ImageURL(url=data_uri),
                )
            if mime_type.startswith("audio/"):
                return AudioURLPart(
                    audio_url=AudioURLPart.AudioURL(url=data_uri),
                )
            if mime_type.startswith("video/"):
                return VideoURLPart(
                    video_url=VideoURLPart.VideoURL(url=data_uri),
                )
            return TextPart(
                text=f"[Binary resource {content.uri} ({mime_type}) cannot be displayed inline]"
            )

        raise ValueError(f"Unsupported MCP resource content type: {type(content).__name__}")

    @staticmethod
    async def list_resources(
        client: Any, cursor: str | None = None
    ) -> tuple[list[mcp.types.Resource], str | None]:
        """List resources from an MCP server."""
        result = await client.list_resources_mcp(cursor=cursor)
        next_cursor = result.nextCursor if result.nextCursor else None
        return result.resources, next_cursor

    @staticmethod
    async def list_resource_templates(client: Any) -> list[mcp.types.ResourceTemplate]:
        """List resource templates from an MCP server."""
        return await client.list_resource_templates()

    @staticmethod
    async def read_resource(client: Any, uri: str) -> list[ContentPart]:
        """Read a resource from an MCP server and convert it to Kimi content parts."""
        contents = await client.read_resource(uri)
        return MCPResourceManager.convert_contents(contents)

    @staticmethod
    async def subscribe_resource(client: Any, uri: str) -> None:
        """Subscribe to resource updates."""
        from pydantic import AnyUrl

        await client.session.subscribe_resource(AnyUrl(uri))

    @staticmethod
    async def unsubscribe_resource(client: Any, uri: str) -> None:
        """Unsubscribe from resource updates."""
        from pydantic import AnyUrl

        await client.session.unsubscribe_resource(AnyUrl(uri))
