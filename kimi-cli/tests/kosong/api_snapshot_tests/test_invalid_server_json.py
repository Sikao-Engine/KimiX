"""Trigger tests: the remote server returns invalid JSON.

A misbehaving or glitching backend can answer a *successful* (200) request
with a body that is not valid JSON — truncated mid-object, plain prose, or a
schema that does not match the API contract. The agent loop only understands
``kosong.chat_provider.ChatProviderError`` and its retryable subclasses
(APIConnectionError / APITimeoutError / APIStatusError); any foreign
exception (raw ``json.JSONDecodeError``, pydantic ``ValidationError``,
SDK-internal errors) escapes the retry machinery and aborts the whole turn
as an unhandled fatal error.

Contract under test: no matter how the server garbles a 200 response, the
provider must surface a ``ChatProviderError`` — and for "body arrived but is
unusable" cases a *retryable* ``APIConnectionError`` so the step retry loop
can recover from a transient backend glitch.

These tests were written before the fix; the ones asserting the contract
fail on the unhandled-exception paths and pass once the parsers are hardened.
"""

from __future__ import annotations

import json as std_json

import httpx
import pytest
import respx

from kosong.chat_provider import (
    APIConnectionError,
    ChatProviderError,
    StreamedMessagePart,
)
from kosong.message import Message, TextPart

from common import make_httpx2_client

SSE_HEADERS = {"Content-Type": "text/event-stream"}


def _chat_chunk(delta: dict, chunk_id: str = "chatcmpl-1") -> str:
    return (
        "data: "
        + std_json.dumps(
            {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "m",
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
        )
        + "\n\n"
    )


async def _collect(stream) -> list[StreamedMessagePart]:
    return [part async for part in stream]


# ---------------------------------------------------------------------------
# OpenAI-compatible (Chat Completions) streaming
# ---------------------------------------------------------------------------


@respx.mock
async def test_openai_stream_malformed_data_line_is_skipped():
    """A malformed ``data:`` line mid-stream must not abort the response."""
    from kosong.contrib.chat_provider.openai_legacy import OpenAILegacy

    body = (
        _chat_chunk({"role": "assistant", "content": "Hel"})
        + "data: {invalid json\n\n"
        + _chat_chunk({"content": "lo"})
        + "data: [DONE]\n\n"
    )
    respx.post("https://api.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, headers=SSE_HEADERS, content=body)
    )
    provider = OpenAILegacy(model="m", api_key="k", base_url="https://api.test/v1")
    stream = await provider.generate("", [], [Message(role="user", content=[TextPart(text="hi")])])
    parts = await _collect(stream)
    text = "".join(p.text for p in parts if isinstance(p, TextPart))
    assert text == "Hello"


@respx.mock
async def test_openai_stream_schema_invalid_chunk_raises_provider_error():
    """Valid JSON that violates the chunk schema must raise ChatProviderError,
    not a raw pydantic ValidationError."""
    from kosong.contrib.chat_provider.openai_legacy import OpenAILegacy

    body = "data: " + std_json.dumps({"not": "a chunk"}) + "\n\n" + "data: [DONE]\n\n"
    respx.post("https://api.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, headers=SSE_HEADERS, content=body)
    )
    provider = OpenAILegacy(model="m", api_key="k", base_url="https://api.test/v1")
    stream = await provider.generate("", [], [Message(role="user", content=[TextPart(text="hi")])])
    with pytest.raises(ChatProviderError):
        await _collect(stream)


# ---------------------------------------------------------------------------
# OpenAI-compatible (Chat Completions) non-streaming
# ---------------------------------------------------------------------------


@respx.mock
async def test_openai_non_stream_invalid_json_body_is_retryable():
    """200 + non-JSON body: must surface as retryable APIConnectionError."""
    from kosong.contrib.chat_provider.openai_legacy import OpenAILegacy

    respx.post("https://api.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=b"this is not json at all",
        )
    )
    provider = OpenAILegacy(model="m", api_key="k", base_url="https://api.test/v1", stream=False)
    with pytest.raises(APIConnectionError):
        stream = await provider.generate("", [], [Message(role="user", content=[TextPart(text="hi")])])
        await _collect(stream)


@respx.mock
async def test_openai_non_stream_schema_invalid_body_is_provider_error():
    """200 + JSON body that violates the response schema: must surface as a
    ChatProviderError (foreign ValidationError must not escape)."""
    from kosong.contrib.chat_provider.openai_legacy import OpenAILegacy

    respx.post("https://api.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=b'{"unexpected": "shape"}',
        )
    )
    provider = OpenAILegacy(model="m", api_key="k", base_url="https://api.test/v1", stream=False)
    with pytest.raises(ChatProviderError):
        stream = await provider.generate("", [], [Message(role="user", content=[TextPart(text="hi")])])
        await _collect(stream)


@respx.mock
async def test_openai_non_stream_truncated_json_body_is_retryable():
    """200 + truncated JSON object: same contract as garbage body."""
    from kosong.contrib.chat_provider.openai_legacy import OpenAILegacy

    respx.post("https://api.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=b'{"id": "chatcmpl-x", "choices": [',
        )
    )
    provider = OpenAILegacy(model="m", api_key="k", base_url="https://api.test/v1", stream=False)
    with pytest.raises(APIConnectionError):
        stream = await provider.generate("", [], [Message(role="user", content=[TextPart(text="hi")])])
        await _collect(stream)


# ---------------------------------------------------------------------------
# Anthropic Messages
# ---------------------------------------------------------------------------


@respx.mock
async def test_anthropic_stream_malformed_event_is_provider_error():
    """Malformed SSE event JSON from an Anthropic backend must surface as a
    ChatProviderError, not a raw json.JSONDecodeError."""
    from kosong.contrib.chat_provider.anthropic import Anthropic

    body = (
        'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","type":"message","role":"assistant","model":"m","content":[],"stop_reason":null,"usage":{"input_tokens":1,"output_tokens":1}}}\n\n'
        "event: content_block_delta\ndata: {broken\n\n"
    )
    with respx.mock(base_url="https://api.anthropic.com") as mock:
        mock.post("/v1/messages").mock(
            return_value=httpx.Response(200, headers=SSE_HEADERS, content=body)
        )
        provider = Anthropic(
            model="claude-sonnet-4-20250514",
            api_key="k",
            default_max_tokens=1024,
            http_client=make_httpx2_client(mock),
        )
        stream = await provider.generate("", [], [Message(role="user", content=[TextPart(text="hi")])])
        with pytest.raises(ChatProviderError):
            await _collect(stream)


@respx.mock
async def test_anthropic_non_stream_invalid_json_body_is_provider_error():
    """200 + non-JSON body on the Messages endpoint."""
    from kosong.contrib.chat_provider.anthropic import Anthropic

    with respx.mock(base_url="https://api.anthropic.com") as mock:
        mock.post("/v1/messages").mock(
            return_value=httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=b"<html>Bad Gateway</html>",
            )
        )
        provider = Anthropic(
            model="claude-sonnet-4-20250514",
            api_key="k",
            default_max_tokens=1024,
            stream=False,
            http_client=make_httpx2_client(mock),
        )
        with pytest.raises(ChatProviderError):
            stream = await provider.generate("", [], [Message(role="user", content=[TextPart(text="hi")])])
            await _collect(stream)


# ---------------------------------------------------------------------------
# Google GenAI
# ---------------------------------------------------------------------------


@respx.mock
async def test_google_genai_invalid_json_body_is_provider_error():
    """200 + non-JSON body on the Gemini endpoint."""
    from kosong.contrib.chat_provider.google_genai import GoogleGenAI

    respx.post(
        url__regex=r"https://generativelanguage.googleapis.com/v1beta/models/.+:(stream)?[Gg]enerateContent"
    ).mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=b"Internal Server Error",
        )
    )
    provider = GoogleGenAI(model="gemini-2.5-flash", api_key="k")
    with pytest.raises(ChatProviderError):
        stream = await provider.generate("", [], [Message(role="user", content=[TextPart(text="hi")])])
        await _collect(stream)
