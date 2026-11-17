"""Gemini thought-signature round-trip through the OpenAI-compatible provider.

Gemini 3 rejects a replayed assistant tool call that lost the
``thought_signature`` the model attached to it::

    Function call is missing a thought_signature in functionCall parts.

Google's OpenAI-compatible endpoint returns the signature as
``tool_calls[].extra_content.google.thought_signature`` (and some proxies also
flatten it to ``tool_calls[].thought_signature``).  The provider must keep that
opaque payload in :attr:`kosong.message.ToolCall.extras` and replay it on the
next request instead of dropping it while converting messages.

Tool calls written before signatures were captured (legacy sessions or histories
imported from another provider) have nothing to replay; those fall back to
Gemini's documented ``skip_thought_signature_validator`` placeholder so the
session can continue instead of failing with the same 400.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from openai.types.chat import ChatCompletion, ChatCompletionChunk

from kosong.chat_provider.openai_common import (
    apply_tool_call_extras,
    extract_tool_call_extras,
)
from kosong.contrib.chat_provider.openai_legacy import (
    OpenAILegacy,
    OpenAILegacyStreamedMessage,
)
from kosong.message import Message, ToolCall

SIGNATURE = "AY89a1/thought-signature=="
EXTRA_CONTENT = {"google": {"thought_signature": SIGNATURE}}


def _tool_call_payload(**extra: Any) -> dict[str, Any]:
    return {
        "id": "call_read_1",
        "type": "function",
        "function": {"name": "read", "arguments": '{"path":"a.txt"}'},
        **extra,
    }


def _completion(tool_call: dict[str, Any] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": ""}
    if tool_call is not None:
        message["tool_calls"] = [tool_call]
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": "gemini-4-hydrogen",
        "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _stream_chunk(tool_call: dict[str, Any]) -> ChatCompletionChunk:
    return ChatCompletionChunk.model_validate(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "gemini-4-hydrogen",
            "choices": [{"index": 0, "delta": {"tool_calls": [tool_call]}}],
        }
    )


async def _parts(*chunks: ChatCompletionChunk) -> list[Any]:
    async def _aiter() -> AsyncIterator[ChatCompletionChunk]:
        for chunk in chunks:
            yield chunk

    stream = OpenAILegacyStreamedMessage(_aiter(), reasoning_key=None)
    return [part async for part in stream]


class TestExtractToolCallExtras:
    def test_captures_extra_content(self) -> None:
        tool_call = (
            ChatCompletion.model_validate(
                _completion(_tool_call_payload(extra_content=EXTRA_CONTENT))
            )
            .choices[0]
            .message.tool_calls[0]
        )
        assert extract_tool_call_extras(tool_call) == {"extra_content": EXTRA_CONTENT}

    def test_normalizes_top_level_thought_signature(self) -> None:
        tool_call = (
            ChatCompletion.model_validate(
                _completion(_tool_call_payload(thought_signature=SIGNATURE))
            )
            .choices[0]
            .message.tool_calls[0]
        )
        assert extract_tool_call_extras(tool_call) == {"extra_content": EXTRA_CONTENT}

    def test_returns_none_without_extras(self) -> None:
        tool_call = (
            ChatCompletion.model_validate(_completion(_tool_call_payload()))
            .choices[0]
            .message.tool_calls[0]
        )
        assert extract_tool_call_extras(tool_call) is None

    def test_ignores_non_google_extra_content(self) -> None:
        tool_call = (
            ChatCompletion.model_validate(
                _completion(_tool_call_payload(extra_content={"other": {"data": 1}}))
            )
            .choices[0]
            .message.tool_calls[0]
        )
        assert extract_tool_call_extras(tool_call) is None

    def test_extras_survive_session_serialization(self) -> None:
        """Sessions are persisted as JSON lines; the signature must survive a
        write/read cycle so resumed sessions can still replay it."""
        message = Message(
            role="assistant",
            content=[],
            tool_calls=[
                ToolCall(
                    id="call_read_1",
                    function=ToolCall.FunctionBody(name="read", arguments="{}"),
                    extras={"extra_content": EXTRA_CONTENT},
                )
            ],
        )
        restored = Message.model_validate_json(message.model_dump_json(exclude_none=True))
        assert restored.tool_calls is not None
        assert restored.tool_calls[0].extras == {"extra_content": EXTRA_CONTENT}


class TestStreamToolCallExtras:
    async def test_streamed_extra_content_is_kept_on_the_tool_call(self) -> None:
        parts = await _parts(
            _stream_chunk(
                {
                    "index": 0,
                    **_tool_call_payload(extra_content=EXTRA_CONTENT),
                }
            )
        )
        tool_calls = [part for part in parts if isinstance(part, ToolCall)]
        assert len(tool_calls) == 1
        assert tool_calls[0].extras == {"extra_content": EXTRA_CONTENT}

    async def test_extra_content_arriving_before_the_name_is_buffered(self) -> None:
        parts = await _parts(
            _stream_chunk(
                {
                    "index": 0,
                    "id": "call_read_1",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                    "extra_content": EXTRA_CONTENT,
                }
            ),
            _stream_chunk(
                {
                    "index": 0,
                    "function": {"name": "read", "arguments": '{"path":"a.txt"}'},
                }
            ),
        )
        tool_calls = [part for part in parts if isinstance(part, ToolCall)]
        assert len(tool_calls) == 1
        assert tool_calls[0].extras == {"extra_content": EXTRA_CONTENT}

    async def test_top_level_thought_signature_is_normalized(self) -> None:
        parts = await _parts(
            _stream_chunk(
                {
                    "index": 0,
                    **_tool_call_payload(thought_signature=SIGNATURE),
                }
            )
        )
        tool_calls = [part for part in parts if isinstance(part, ToolCall)]
        assert tool_calls[0].extras == {"extra_content": EXTRA_CONTENT}

    async def test_tool_call_without_signature_is_unchanged(self) -> None:
        parts = await _parts(
            _stream_chunk(
                {
                    "index": 0,
                    **_tool_call_payload(),
                }
            )
        )
        tool_calls = [part for part in parts if isinstance(part, ToolCall)]
        assert tool_calls[0].extras is None

    async def test_generate_merge_keeps_extras_on_the_final_tool_call(self) -> None:
        """The stream is emitted as a header plus argument deltas; merging them
        into one ToolCall must not drop the signature carried by the header."""
        from kosong import generate
        from kosong.chat_provider.mock import MockChatProvider
        from kosong.message import ToolCallPart

        provider = MockChatProvider(
            message_parts=[
                ToolCall(
                    id="call_read_1",
                    function=ToolCall.FunctionBody(name="read", arguments=""),
                    extras={"extra_content": EXTRA_CONTENT},
                ),
                ToolCallPart(arguments_part='{"path":"a.txt"}'),
            ]
        )
        result = await generate(provider, system_prompt="", tools=[], history=[])
        assert result.message.tool_calls is not None
        assert result.message.tool_calls[0].function.arguments == '{"path":"a.txt"}'
        assert result.message.tool_calls[0].extras == {"extra_content": EXTRA_CONTENT}


class TestReplayExtras:
    def _provider(
        self,
        *,
        model: str = "gemini-4-hydrogen",
        base_url: str = "https://v2.example.com/llmproxy",
    ) -> OpenAILegacy:
        return OpenAILegacy(model=model, api_key="k", base_url=base_url)

    def test_convert_message_replays_extra_content_and_drops_extras(self) -> None:
        message = Message(
            role="assistant",
            content=[],
            tool_calls=[
                ToolCall(
                    id="call_read_1",
                    function=ToolCall.FunctionBody(name="read", arguments='{"path":"a.txt"}'),
                    extras={"extra_content": EXTRA_CONTENT},
                )
            ],
        )
        converted = self._provider()._convert_message(message)  # type: ignore[reportPrivateUsage]
        tool_call = converted["tool_calls"][0]  # type: ignore[index]
        assert tool_call["extra_content"] == EXTRA_CONTENT
        assert "extras" not in tool_call

    def test_convert_message_folds_top_level_signature_into_extra_content(self) -> None:
        extras = {"thought_signature": SIGNATURE}
        converted: dict[str, Any] = {}
        apply_tool_call_extras(converted, extras)
        assert converted == {"extra_content": EXTRA_CONTENT}

    def test_convert_message_does_not_leak_other_provider_extras(self) -> None:
        message = Message(
            role="assistant",
            content=[],
            tool_calls=[
                ToolCall(
                    id="call_read_1",
                    function=ToolCall.FunctionBody(name="read", arguments="{}"),
                    extras={"thought_signature_b64": "Z2VtaW5p"},
                )
            ],
        )
        provider = self._provider(model="gpt-4.1", base_url="https://api.openai.com/v1")
        converted = provider._convert_message(message)  # type: ignore[reportPrivateUsage]
        tool_call = converted["tool_calls"][0]  # type: ignore[index]
        assert "extra_content" not in tool_call
        assert "extras" not in tool_call

    def test_unsigned_gemini_tool_call_gets_the_documented_placeholder(self) -> None:
        """Legacy histories have no captured signature; Gemini accepts the
        documented ``skip_thought_signature_validator`` placeholder."""
        from kosong.chat_provider.openai_common import GEMINI_SKIP_THOUGHT_SIGNATURE

        message = Message(
            role="assistant",
            content=[],
            tool_calls=[
                ToolCall(
                    id="call_read_1",
                    function=ToolCall.FunctionBody(name="read", arguments="{}"),
                )
            ],
        )
        converted = self._provider()._convert_message(message)  # type: ignore[reportPrivateUsage]
        tool_call = converted["tool_calls"][0]  # type: ignore[index]
        assert tool_call["extra_content"] == {
            "google": {"thought_signature": GEMINI_SKIP_THOUGHT_SIGNATURE}
        }
        assert "extras" not in tool_call

    def test_real_signature_wins_over_the_placeholder(self) -> None:
        message = Message(
            role="assistant",
            content=[],
            tool_calls=[
                ToolCall(
                    id="call_read_1",
                    function=ToolCall.FunctionBody(name="read", arguments="{}"),
                    extras={"extra_content": EXTRA_CONTENT},
                )
            ],
        )
        converted = self._provider()._convert_message(message)  # type: ignore[reportPrivateUsage]
        tool_call = converted["tool_calls"][0]  # type: ignore[index]
        assert tool_call["extra_content"] == EXTRA_CONTENT

    def test_unsigned_parallel_calls_only_need_one_signature(self) -> None:
        """Gemini puts one signature on the first parallel function call; the
        siblings stay unsigned, so the fallback only patches the first."""
        from kosong.chat_provider.openai_common import GEMINI_SKIP_THOUGHT_SIGNATURE

        message = Message(
            role="assistant",
            content=[],
            tool_calls=[
                ToolCall(
                    id="call_read_1",
                    function=ToolCall.FunctionBody(name="read", arguments='{"path":"a"}'),
                ),
                ToolCall(
                    id="call_read_2",
                    function=ToolCall.FunctionBody(name="read", arguments='{"path":"b"}'),
                ),
            ],
        )
        converted = self._provider()._convert_message(message)  # type: ignore[reportPrivateUsage]
        first, second = converted["tool_calls"]  # type: ignore[index]
        assert first["extra_content"] == {
            "google": {"thought_signature": GEMINI_SKIP_THOUGHT_SIGNATURE}
        }
        assert "extra_content" not in second

    def test_non_gemini_tool_calls_payload_is_unchanged(self) -> None:
        message = Message(
            role="assistant",
            content=[],
            tool_calls=[
                ToolCall(
                    id="call_read_1",
                    function=ToolCall.FunctionBody(name="read", arguments="{}"),
                )
            ],
        )
        provider = self._provider(model="gpt-4.1", base_url="https://api.openai.com/v1")
        converted = provider._convert_message(message)  # type: ignore[reportPrivateUsage]
        assert "extra_content" not in converted["tool_calls"][0]  # type: ignore[index]
        assert "extras" not in converted["tool_calls"][0]  # type: ignore[index]


async def test_generate_round_trips_thought_signature() -> None:
    """The live failure mode: first response yields a tool call, and the next
    request must replay the signature on the assistant tool call."""
    import respx
    from httpx import Response

    provider = OpenAILegacy(
        model="gemini-4-hydrogen",
        api_key="k",
        base_url="https://v2.example.com/llmproxy",
        stream=False,
    )
    try:
        with respx.mock(base_url="https://v2.example.com") as mock:
            route = mock.post("/llmproxy/chat/completions").mock(
                side_effect=[
                    Response(
                        200,
                        json=_completion(_tool_call_payload(extra_content=EXTRA_CONTENT)),
                    ),
                    Response(200, json=_completion()),
                ]
            )
            history = [Message(role="user", content="read a.txt")]
            stream = await provider.generate("You are helpful.", [], history)
            parts = [part async for part in stream]
            tool_calls = [part for part in parts if isinstance(part, ToolCall)]
            assert tool_calls[0].extras == {"extra_content": EXTRA_CONTENT}

            history.extend(
                [
                    Message(role="assistant", content=[], tool_calls=tool_calls),
                    Message(
                        role="tool",
                        tool_call_id=tool_calls[0].id,
                        content="file contents",
                    ),
                ]
            )
            stream = await provider.generate("You are helpful.", [], history)
            async for _ in stream:
                pass

            second_body = json.loads(route.calls[1].request.content.decode())
            assistant = second_body["messages"][-2]
            sent_tool_call = assistant["tool_calls"][0]
            assert sent_tool_call["extra_content"] == EXTRA_CONTENT
            assert "extras" not in sent_tool_call
    finally:
        await provider.aclose()
