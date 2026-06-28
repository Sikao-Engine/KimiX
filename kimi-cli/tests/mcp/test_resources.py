from __future__ import annotations

import base64

import mcp.types
from kosong.message import AudioURLPart, ImageURLPart, TextPart, VideoURLPart
from pydantic import AnyUrl

from kimi_cli.mcp.resources import MCPResourceManager


def test_convert_text_resource_contents() -> None:
    contents = [
        mcp.types.TextResourceContents(
            uri=AnyUrl("file:///test.txt"),
            mimeType="text/plain",
            text="hello world",
        )
    ]
    parts = MCPResourceManager.convert_contents(contents)
    assert len(parts) == 1
    assert isinstance(parts[0], TextPart)
    assert parts[0].text == "hello world"


def test_convert_blob_resource_image() -> None:
    data = b"fake-image-data"
    encoded = base64.b64encode(data).decode("ascii")
    contents = [
        mcp.types.BlobResourceContents(
            uri=AnyUrl("file:///test.png"),
            mimeType="image/png",
            blob=encoded,
        )
    ]
    parts = MCPResourceManager.convert_contents(contents)
    assert len(parts) == 1
    assert isinstance(parts[0], ImageURLPart)
    assert f"data:image/png;base64,{encoded}" in parts[0].image_url.url


def test_convert_blob_resource_audio() -> None:
    data = b"fake-audio-data"
    encoded = base64.b64encode(data).decode("ascii")
    contents = [
        mcp.types.BlobResourceContents(
            uri=AnyUrl("file:///test.mp3"),
            mimeType="audio/mpeg",
            blob=encoded,
        )
    ]
    parts = MCPResourceManager.convert_contents(contents)
    assert isinstance(parts[0], AudioURLPart)


def test_convert_blob_resource_video() -> None:
    data = b"fake-video-data"
    encoded = base64.b64encode(data).decode("ascii")
    contents = [
        mcp.types.BlobResourceContents(
            uri=AnyUrl("file:///test.mp4"),
            mimeType="video/mp4",
            blob=encoded,
        )
    ]
    parts = MCPResourceManager.convert_contents(contents)
    assert isinstance(parts[0], VideoURLPart)


def test_convert_binary_resource_becomes_placeholder() -> None:
    data = b"fake-binary-data"
    encoded = base64.b64encode(data).decode("ascii")
    contents = [
        mcp.types.BlobResourceContents(
            uri=AnyUrl("file:///test.bin"),
            mimeType="application/octet-stream",
            blob=encoded,
        )
    ]
    parts = MCPResourceManager.convert_contents(contents)
    assert isinstance(parts[0], TextPart)
    assert "cannot be displayed inline" in parts[0].text


def test_convert_unsupported_resource_returns_placeholder() -> None:
    class UnknownContents(mcp.types.ResourceContents):
        uri: AnyUrl = AnyUrl("unknown://test")
        mimeType: str | None = None

    contents = [UnknownContents()]
    parts = MCPResourceManager.convert_contents(contents)
    assert isinstance(parts[0], TextPart)
    assert "Unsupported resource content" in parts[0].text
