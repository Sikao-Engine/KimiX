"""Regression tests: the anthropic provider must not call SDK TypedDict types.

anthropic SDK 2.x ships its ``*Param`` types as ``typing.TypedDict`` unions
that are **not runtime-callable** — invoking ``MessageParam(role=..., ...)``
raises ``TypeError: 'typing.Union' object is not callable``.  The provider
must therefore build plain ``dict`` payloads everywhere.

These tests pin that behaviour by exercising every payload-construction path
(``generate()`` plus the module-level helpers) and asserting the wire shape.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

pytest.importorskip("anthropic", reason="Optional contrib dependency not installed")

import respx
from httpx import Response
import orjson

sys.path.insert(0, str(Path(__file__).parent / "api_snapshot_tests"))
from common import make_httpx2_client  # noqa: E402

from kosong.contrib.chat_provider.anthropic import (  # noqa: E402
    Anthropic,
    _image_url_part_to_anthropic,  # pyright: ignore[reportPrivateUsage]
    _tool_result_message_to_block,  # pyright: ignore[reportPrivateUsage]
)
from kosong.message import (  # noqa: E402
    ImageURLPart,
    Message,
    TextPart,
    ThinkPart,
    ToolCall,
)


def _provider(mock: respx.MockRouter, **kwargs: object) -> Anthropic:
    return Anthropic(
        http_client=make_httpx2_client(mock),
        model="claude-sonnet-4-20250514",
        api_key="test-api-key",
        default_max_tokens=1024,
        stream=False,
        **kwargs,
    )


def _anthropic_response() -> dict:
    return {
        "id": "msg_test_123",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-20250514",
        "content": [{"type": "text", "text": "Hello"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def test_sdk_union_types_are_not_runtime_callable() -> None:
    """Documents the SDK constraint this module works around.

    ``MessageParam`` is a ``typing.Union`` alias in anthropic SDK 2.x, and
    union aliases are not runtime-callable.  If the SDK ever ships a real
    class here, this test fails to remind us the plain-dict workaround can
    be relaxed.
    """
    from anthropic.types import MessageParam

    assert not isinstance(MessageParam, type)
    with pytest.raises(TypeError):
        MessageParam(type="text", text="x")  # type: ignore[operator]


# ---------------------------------------------------------------------------
# _convert_message / helpers return plain dicts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["user", "assistant"])
def test_convert_message_text_returns_plain_dict(role: str) -> None:
    provider = object.__new__(Anthropic)
    out = provider._convert_message(  # pyright: ignore[reportPrivateUsage]
        Message(role=role, content=[TextPart(text="hi")])
    )
    assert type(out) is dict
    assert out == {"role": role, "content": [{"type": "text", "text": "hi"}]}


def test_convert_message_system_role_returns_user_dict() -> None:
    provider = object.__new__(Anthropic)
    out = provider._convert_message(  # pyright: ignore[reportPrivateUsage]
        Message(role="system", content=[TextPart(text="be nice")])
    )
    assert type(out) is dict
    assert out["role"] == "user"
    assert out["content"] == [{"type": "text", "text": "<system>be nice</system>"}]


def test_convert_message_thinking_and_tool_use_blocks() -> None:
    provider = object.__new__(Anthropic)
    out = provider._convert_message(  # pyright: ignore[reportPrivateUsage]
        Message(
            role="assistant",
            content=[ThinkPart(think="hmm", encrypted="sig")],
            tool_calls=[
                ToolCall(
                    id="call_1",
                    function=ToolCall.FunctionBody(name="add", arguments='{"a": 1}'),
                )
            ],
        )
    )
    assert type(out) is dict
    assert out["content"] == [
        {"type": "thinking", "thinking": "hmm", "signature": "sig"},
        {"type": "tool_use", "id": "call_1", "name": "add", "input": {"a": 1}},
    ]


def test_tool_result_block_plain_dict() -> None:
    block = _tool_result_message_to_block("call_1", [TextPart(text="result")])
    assert type(block) is dict
    assert block == {
        "type": "tool_result",
        "tool_use_id": "call_1",
        "content": [{"type": "text", "text": "result"}],
    }


def test_image_url_part_to_anthropic_plain_dicts() -> None:
    # Plain URL source.
    out = _image_url_part_to_anthropic(ImageURLPart(image_url={"url": "https://x/y.png"}))
    assert type(out) is dict
    assert out == {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}}

    # Base64 data URL source.
    data = base64.b64encode(b"png-bytes").decode()
    out = _image_url_part_to_anthropic(
        ImageURLPart(image_url={"url": f"data:image/png;base64,{data}"})
    )
    assert type(out) is dict
    assert out["source"]["type"] == "base64"
    assert out["source"]["data"] == data

    # Unsupported media type degrades to a text error block.
    out = _image_url_part_to_anthropic(
        ImageURLPart(image_url={"url": "data:image/tiff;base64,AAAA"})
    )
    assert type(out) is dict
    assert out["type"] == "text"
    assert "Unsupported media type" in out["text"]


# ---------------------------------------------------------------------------
# generate() end-to-end against a mocked endpoint (no TypedDict construction)
# ---------------------------------------------------------------------------


async def test_generate_wire_payload_is_plain_dicts() -> None:
    history = [
        Message(role="system", content=[TextPart(text="be nice")]),
        Message(role="user", content=[TextPart(text="hi")]),
        Message(
            role="assistant",
            content=[TextPart(text="calling")],
            tool_calls=[
                ToolCall(
                    id="call_1",
                    function=ToolCall.FunctionBody(name="add", arguments='{"a": 1}'),
                )
            ],
        ),
        Message(role="tool", content=[TextPart(text="2")], tool_call_id="call_1"),
    ]
    with respx.mock(base_url="https://api.anthropic.com") as mock:
        mock.post("/v1/messages").mock(return_value=Response(200, json=_anthropic_response()))
        provider = _provider(mock)
        stream = await provider.generate("You are helpful.", [], history)
        parts = [part async for part in stream]
        body = mock.calls.last.request.content
    assert parts == [TextPart(text="Hello")]

    payload = orjson.loads(body)
    # System prompt carries the ephemeral cache_control block.
    assert payload["system"] == [
        {
            "text": "You are helpful.",
            "type": "text",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    # History converted to plain dict messages; tool result merged as a block.
    assert payload["messages"][0] == {
        "role": "user",
        "content": [{"type": "text", "text": "<system>be nice</system>"}],
    }
    tool_result_msg = payload["messages"][-1]
    assert tool_result_msg["role"] == "user"
    assert tool_result_msg["content"][0]["type"] == "tool_result"
    assert tool_result_msg["content"][0]["tool_use_id"] == "call_1"
    assert tool_result_msg["content"][0]["content"] == [{"type": "text", "text": "2"}]
    # Last history block gets the ephemeral cache_control injected.
    assert payload["messages"][-1]["content"][-1]["cache_control"] == {
        "type": "ephemeral"
    }
