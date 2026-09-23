"""Tests for OpenAILegacy provider format-error handling.

These tests guard against malformed messages crashing the client or causing
API 400 errors.  Invalid data is returned to the LLM as error text so the
model can recover.
"""

from __future__ import annotations

from kosong.contrib.chat_provider.openai_legacy import OpenAILegacy
from kosong.message import Message, TextPart, ToolCall


class TestInvalidToolCallArguments:
    """When an assistant message contains a tool call with malformed JSON arguments,
    _convert_message must not crash.  It should emit an error text block and
    replace the broken arguments with '{}' so the LLM sees the problem."""

    def test_invalid_json_returns_error_to_llm(self) -> None:
        provider = OpenAILegacy(
            model="gpt-4.1",
            api_key="test-key",
            stream=False,
        )
        message = Message(
            role="assistant",
            content=[TextPart(text="Let me call a tool.")],
            tool_calls=[
                ToolCall(
                    id="call_bad",
                    function=ToolCall.FunctionBody(
                        name="add",
                        arguments='{"a": 1, "b": 2',  # missing closing }
                    ),
                )
            ],
        )
        result = provider._convert_message(message)
        assert result["role"] == "assistant"
        content = result["content"]
        # Strict validation: json_repair-style lenient parsing is NOT accepted here
        # (it passes strings like '{}{}' that strict backends 400 on).  The broken
        # arguments are reset to '{}' and an error text block is prepended so the
        # LLM sees the problem.
        assert isinstance(content, list)
        assert "invalid JSON arguments" in content[0]["text"]
        assert content[1]["text"] == "Let me call a tool."
        assert result["tool_calls"][0]["function"]["arguments"] == "{}"

    def test_non_dict_json_returns_error_to_llm(self) -> None:
        provider = OpenAILegacy(
            model="gpt-4.1",
            api_key="test-key",
            stream=False,
        )
        message = Message(
            role="assistant",
            content=[],
            tool_calls=[
                ToolCall(
                    id="call_bad2",
                    function=ToolCall.FunctionBody(
                        name="add",
                        arguments="[1, 2]",  # array, not object
                    ),
                )
            ],
        )
        result = provider._convert_message(message)
        assert result["role"] == "assistant"
        content = result["content"]
        # When there is exactly one TextPart, Message serializes it as a string.
        assert isinstance(content, str)
        assert "must be a JSON object" in content
        assert result["tool_calls"][0]["function"]["arguments"] == "{}"


class TestGracefulDataErrors:
    """Data validation errors during message conversion must return text blocks
    to the LLM instead of crashing the client."""

    def test_missing_tool_call_id_returns_error_to_llm(self) -> None:
        provider = OpenAILegacy(
            model="gpt-4.1",
            api_key="test-key",
            stream=False,
        )
        message = Message(
            role="tool",
            content=[TextPart(text="The result is 5")],
            tool_call_id=None,
        )
        result = provider._convert_message(message)
        assert result["role"] == "user"
        assert "missing `tool_call_id`" in result["content"]
        assert "The result is 5" in result["content"]
