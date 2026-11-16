"""Tests for the Gemini tool-schema compatibility patch in OpenAI-compatible providers.

Gemini rejects ``GenerateContent`` requests whose array parameter schemas omit
``items`` (``...any_of[0].items: missing field.``) — the shape Pydantic emits for
``tuple[int, int]`` fields such as ``computer_use``'s ``coordinate``. OpenRouter
and other gateways that proxy Gemini inherit the restriction, so the Chat
Completions providers patch the schema *only* for Gemini-backed endpoints and
leave every other provider's payload byte-identical.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import respx
from httpx import Response

from kosong.chat_provider.compat import ThinkingControlledOpenAIProvider
from kosong.chat_provider.openai_common import (
    looks_like_gemini_backend,
    tool_to_openai,
    tool_to_openai_gemini,
)
from kosong.contrib.chat_provider.google_genai import tool_to_google_genai
from kosong.contrib.chat_provider.openai_legacy import OpenAILegacy
from kosong.message import Message
from kosong.tooling import Tool


def _tuple_tool() -> Tool:
    """A tool with a ``tuple[int, int] | None`` parameter, i.e. computer_use's shape."""
    return Tool(
        name="computer_use",
        description="Control the desktop.",
        parameters={
            "type": "object",
            "properties": {
                "coordinate": {
                    "anyOf": [
                        {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": 2,
                            "prefixItems": [{"type": "integer"}, {"type": "integer"}],
                        },
                        {"type": "null"},
                    ],
                    "default": None,
                    "description": "[x,y] pixels.",
                }
            },
        },
    )


def _completion(model: str = "test-model") -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "OK"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


@pytest.mark.parametrize(
    ("model", "base_url", "expected"),
    [
        ("gemini-4-hydrogen", "https://v2.example.com/llmproxy", True),
        ("gemini-2.5-pro", "https://generativelanguage.googleapis.com/v1beta/openai", True),
        ("models/gemini-3-pro", None, True),
        ("google/gemini-3-pro", "https://openrouter.ai/api/v1", True),
        ("gpt-4.1", "https://generativelanguage.googleapis.com/v1beta/openai", True),
        ("gpt-4.1", "https://aiplatform.googleapis.com/v1/projects/x", True),
        ("gpt-4.1", "https://gemini-gateway.example.com/v1", True),
        ("gpt-4.1", "https://api.openai.com/v1", False),
        ("kimi-k2.5", "https://api.moonshot.cn/v1", False),
        ("claude-opus-4.8", "https://api.anthropic.com/v1", False),
        ("", "", False),
    ],
)
def test_looks_like_gemini_backend(model: str, base_url: str | None, expected: bool) -> None:
    assert looks_like_gemini_backend(model=model, base_url=base_url) is expected


def test_tool_to_openai_does_not_add_items() -> None:
    converted = tool_to_openai(_tuple_tool())

    coordinate = converted["function"]["parameters"]["properties"]["coordinate"]  # type: ignore[index]
    array_branch = coordinate["anyOf"][0]
    assert "items" not in array_branch


def test_tool_to_openai_gemini_adds_items() -> None:
    converted = tool_to_openai_gemini(_tuple_tool())

    coordinate = converted["function"]["parameters"]["properties"]["coordinate"]  # type: ignore[index]
    assert coordinate["anyOf"][0]["items"] == {"type": "integer"}


def test_tool_to_openai_gemini_does_not_mutate_the_tool() -> None:
    tool = _tuple_tool()
    tool_to_openai_gemini(tool)

    coordinate = tool.parameters["properties"]["coordinate"]
    assert "items" not in coordinate["anyOf"][0]


def test_provider_detects_gemini_from_config() -> None:
    provider = OpenAILegacy(
        model="gemini-4-hydrogen",
        api_key="k",
        base_url="http://v2.open.venus.oa.com/llmproxy",
    )

    assert provider._gemini_schema_compat is True  # type: ignore[reportPrivateUsage]


def test_provider_leaves_non_gemini_payload_alone() -> None:
    provider = OpenAILegacy(model="gpt-4.1", api_key="k", base_url="https://api.openai.com/v1")

    assert provider._gemini_schema_compat is False  # type: ignore[reportPrivateUsage]
    converted = provider._convert_tool(_tuple_tool())  # type: ignore[reportPrivateUsage]
    coordinate = converted["function"]["parameters"]["properties"]["coordinate"]  # type: ignore[index]
    assert "items" not in coordinate["anyOf"][0]


def test_provider_override_forces_and_disables_the_patch() -> None:
    forced = OpenAILegacy(
        model="gpt-4.1",
        api_key="k",
        base_url="https://api.openai.com/v1",
        gemini_schema_compat=True,
    )
    disabled = OpenAILegacy(
        model="gemini-4-hydrogen",
        api_key="k",
        base_url="https://v2.example.com/llmproxy",
        gemini_schema_compat=False,
    )

    forced_coordinate = forced._convert_tool(_tuple_tool())["function"]["parameters"][  # type: ignore[reportPrivateUsage,index]
        "properties"
    ]["coordinate"]
    disabled_coordinate = disabled._convert_tool(_tuple_tool())["function"]["parameters"][  # type: ignore[reportPrivateUsage,index]
        "properties"
    ]["coordinate"]

    assert forced_coordinate["anyOf"][0]["items"] == {"type": "integer"}
    assert "items" not in disabled_coordinate["anyOf"][0]


def test_provider_copy_keeps_the_patch() -> None:
    provider = OpenAILegacy(
        model="gemini-4-hydrogen",
        api_key="k",
        base_url="https://v2.example.com/llmproxy",
    )

    copied = provider.with_generation_kwargs(temperature=0.2)

    assert copied._gemini_schema_compat is True  # type: ignore[reportPrivateUsage]


def test_google_genai_tool_conversion_adds_items() -> None:
    converted = tool_to_google_genai(_tuple_tool())

    declaration = converted.function_declarations[0]
    coordinate = declaration.parameters_json_schema["properties"]["coordinate"]  # type: ignore[index]
    assert coordinate["anyOf"][0]["items"] == {"type": "integer"}


@pytest.mark.asyncio
async def test_generate_sends_items_for_gemini_backends() -> None:
    with respx.mock(base_url="https://v2.example.com") as mock:
        mock.post("/llmproxy/chat/completions").mock(return_value=Response(200, json=_completion()))
        provider = OpenAILegacy(
            model="gemini-4-hydrogen",
            api_key="k",
            base_url="https://v2.example.com/llmproxy",
            stream=False,
        )

        stream = await provider.generate(
            "You are helpful.", [_tuple_tool()], [Message(role="user", content="hi")]
        )
        async for _ in stream:
            pass

        body = json.loads(mock.calls.last.request.content.decode())
        coordinate = body["tools"][0]["function"]["parameters"]["properties"]["coordinate"]
        assert coordinate["anyOf"][0]["items"] == {"type": "integer"}


@pytest.mark.asyncio
async def test_generate_leaves_non_gemini_payload_byte_identical() -> None:
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(return_value=Response(200, json=_completion()))
        provider = OpenAILegacy(
            model="gpt-4.1",
            api_key="k",
            base_url="https://api.openai.com/v1",
            stream=False,
        )

        stream = await provider.generate(
            "You are helpful.", [_tuple_tool()], [Message(role="user", content="hi")]
        )
        async for _ in stream:
            pass

        body = json.loads(mock.calls.last.request.content.decode())
        sent = body["tools"][0]["function"]["parameters"]
        assert sent == _tuple_tool().parameters


@pytest.mark.asyncio
async def test_thinking_controlled_provider_also_patches_gemini_schemas() -> None:
    """Providers that re-implement ``generate`` (DeepSeek/ZAI style) must route
    tool conversion through the same helper."""

    class FakeProvider(ThinkingControlledOpenAIProvider):
        name = "fake"
        _DEFAULT_BASE_URL = "https://fake.example.com/v1"
        _ENV_VARS = ("FAKE_API_KEY",)

    with respx.mock(base_url="https://fake.example.com") as mock:
        mock.post("/v1/chat/completions").mock(return_value=Response(200, json=_completion()))
        provider = FakeProvider(
            model="gemini-4-hydrogen",
            api_key="k",
            base_url="https://fake.example.com/v1",
        )

        stream = await provider.generate(
            "You are helpful.", [_tuple_tool()], [Message(role="user", content="hi")]
        )
        async for _ in stream:
            pass

        body = json.loads(mock.calls.last.request.content.decode())
        coordinate = body["tools"][0]["function"]["parameters"]["properties"]["coordinate"]
        assert coordinate["anyOf"][0]["items"] == {"type": "integer"}
