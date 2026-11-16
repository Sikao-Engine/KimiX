"""Tests for kimi_cli.utils.tokens — model-aware token counting."""

from __future__ import annotations

import orjson
import pytest

from kimi_cli.utils.tokens import (
    _estimate_chars_tokens,
    _is_cjk_text,
    count_message_tokens,
    count_tokens,
    count_tool_tokens,
    estimate_request_overhead,
)
from kimi_cli.wire.types import TextPart
from kosong.message import Message
from kosong.tooling import Tool


class TestIsCjkText:
    def test_empty_is_not_cjk(self):
        assert _is_cjk_text("") is False

    def test_english_is_not_cjk(self):
        assert _is_cjk_text("Hello world") is False

    def test_mixed_below_threshold(self):
        # Use a long ASCII prefix to drop CJK ratio below 0.15
        assert _is_cjk_text("Hello world 世") is False  # 1 CJK / 14 chars ≈ 0.07

    def test_mixed_above_threshold(self):
        text = "Hello世界"
        cjk_ratio = 2 / len(text)
        assert cjk_ratio > 0.15
        assert _is_cjk_text(text) is True

    def test_pure_cjk(self):
        assert _is_cjk_text("你好世界") is True

    def test_korean(self):
        assert _is_cjk_text("안녕하세요") is True

    def test_japanese(self):
        assert _is_cjk_text("こんにちは") is True


class TestEstimateCharsTokens:
    def test_empty(self):
        assert _estimate_chars_tokens("") == 0

    def test_english_ascii(self):
        text = "a" * 400
        assert _estimate_chars_tokens(text) == 100  # 400 // 4

    def test_cjk_text(self):
        text = "你" * 300
        assert _estimate_chars_tokens(text) == 100  # 300 // 3

    def test_mixed_code(self):
        text = "def foo():\n    return '你好'"
        expected = max(1, int(len(text) / 3.5))
        assert _estimate_chars_tokens(text) == expected

    def test_near_boundary_ascii(self):
        """Text with >95% ASCII should use // 4."""
        text = "a" * 96 + "你" * 4
        assert _estimate_chars_tokens(text) == max(1, len(text) // 4)

    def test_mixed_not_cjk_uses_3_5(self):
        """Mixed text where CJK is below threshold uses // 3.5."""
        text = "a" * 95 + "你" * 5
        expected = max(1, int(len(text) / 3.5))
        assert _estimate_chars_tokens(text) == expected


class TestCountTokens:
    def test_empty(self):
        assert count_tokens("") == 0

    def test_english_fallback(self):
        text = "a" * 400
        assert count_tokens(text) == 100

    def test_cjk_fallback(self):
        text = "你" * 300
        assert count_tokens(text) == 100

    def test_with_model_no_tiktoken(self):
        """If tiktoken is not installed or model is unknown, fall back."""
        text = "hello world"
        result = count_tokens(text, model="unknown-model-xyz")
        assert result > 0

    def test_model_aware_when_tiktoken_available(self):
        pytest.importorskip("tiktoken")
        text = "hello world"
        # tiktoken should give a concrete count for a known model
        result = count_tokens(text, model="gpt-4")
        assert result == 2  # "hello" = 1, " world" = 1 for cl100k_base


class TestCountMessageTokens:
    def test_empty_messages(self):
        assert count_message_tokens([]) == 0

    def test_single_text_message(self):
        msg = Message(role="user", content=[TextPart(text="a" * 400)])
        assert count_message_tokens([msg]) == 100

    def test_ignores_non_text_parts(self):
        from kimi_cli.wire.types import ThinkPart

        msg = Message(
            role="user",
            content=[TextPart(text="a" * 40), ThinkPart(think="b" * 400)],
        )
        assert count_message_tokens([msg]) == 10

    def test_multiple_messages(self):
        msgs = [
            Message(role="user", content=[TextPart(text="a" * 200)]),
            Message(role="assistant", content=[TextPart(text="b" * 400)]),
        ]
        assert count_message_tokens(msgs) == 50 + 100

    def test_model_param_passed_through(self):
        pytest.importorskip("tiktoken")
        msg = Message(role="user", content=[TextPart(text="hello world")])
        assert count_message_tokens([msg], model="gpt-4") == 2


class TestBackwardsCompatibility:
    """Ensure the new token estimator stays within 5% of old behaviour on English."""

    def test_english_estimate_within_five_percent(self):
        text = "The quick brown fox jumps over the lazy dog. " * 20
        old_estimate = len(text) // 4
        new_estimate = count_tokens(text)
        diff = abs(new_estimate - old_estimate) / old_estimate
        assert diff <= 0.05


def _tool(name: str = "probe", description: str = "d" * 400) -> Tool:
    return Tool(
        name=name,
        description=description,
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )


class TestCountToolTokens:
    """Tool schemas are request input but live outside the message history."""

    def test_empty_toolset_costs_nothing(self):
        assert count_tool_tokens([]) == 0
        assert count_tool_tokens(()) == 0

    def test_schema_is_counted(self):
        assert count_tool_tokens([_tool()]) > 0

    def test_more_tools_cost_more(self):
        one = count_tool_tokens([_tool("a")])
        three = count_tool_tokens([_tool("a"), _tool("b"), _tool("c")])
        assert three > one

    def test_longer_descriptions_cost_more(self):
        short = count_tool_tokens([_tool(description="x")])
        long = count_tool_tokens([_tool(description="x" * 4_000)])
        assert long > short

    def test_count_matches_the_serialized_schemas(self):
        tool = _tool()
        expected = count_tokens(
            orjson.dumps([tool.model_dump(exclude_none=True)]).decode()
        )
        assert count_tool_tokens([tool]) == expected

    def test_broken_serialization_falls_back_to_the_tool_name(self):
        class Broken:
            name = "broken-tool"

            def model_dump(self, **kwargs):
                raise TypeError("boom")

        assert count_tool_tokens([Broken()]) > 0


class TestEstimateRequestOverhead:
    """The non-history part of a request: system prompt + tool schemas."""

    def test_nothing_to_count(self):
        assert estimate_request_overhead(None, []) == 0

    def test_system_prompt_only(self):
        prompt = "You are a helpful agent."
        assert estimate_request_overhead(prompt, []) == count_tokens(prompt)

    def test_tools_only(self):
        tools = [_tool()]
        assert estimate_request_overhead(None, tools) == count_tool_tokens(tools)

    def test_prompt_and_tools_add_up(self):
        tools = [_tool()]
        prompt = "You are a helpful agent."
        assert estimate_request_overhead(prompt, tools) == (
            count_tokens(prompt) + count_tool_tokens(tools)
        )
