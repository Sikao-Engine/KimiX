from __future__ import annotations

import mcp.types
from kosong.message import TextPart

from kimi_cli.mcp.prompts import MCPPromptManager


def test_convert_text_prompt_message() -> None:
    messages = [
        mcp.types.PromptMessage(
            role="user",
            content=mcp.types.TextContent(type="text", text="hello"),
        )
    ]
    parts = MCPPromptManager.convert_messages(messages)
    assert len(parts) == 1
    assert isinstance(parts[0], TextPart)
    assert parts[0].text == "hello"
