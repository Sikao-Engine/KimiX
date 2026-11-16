"""The compaction reservation must follow the live provider output budget.

``config.max_tokens`` is only the *configured* budget. The value the provider is
really asked for lives in the provider's generation kwargs and can drift:

- providers inject their own default when no budget is configured;
- ``KimiSoul._before_step_retry_sleep`` raises the cap by 1.5x per think-only
  retry without touching the config.

Reserving from a stale/smaller value under-estimates the next request's output
need, so :func:`effective_max_output_tokens` is the single source of truth.
"""

from __future__ import annotations

from types import SimpleNamespace

from kimi_cli.llm import effective_max_output_tokens, output_token_kwarg_name
from kimi_cli.soul.kimisoul import KimiSoul
from kimi_cli.utils.tokens import (
    count_message_tokens,
    count_tool_tokens,
    estimate_request_overhead,
)
from kimi_cli.wire.types import TextPart
from kosong.message import Message
from kosong.tooling import Tool


class _FakeProvider:
    """Minimal stand-in for a kosong chat provider."""

    def __init__(
        self,
        name: str = "kimi",
        *,
        model_name: str = "fake-model",
        generation_kwargs: dict[str, object] | None = None,
        max_completion_tokens: int | None = None,
    ) -> None:
        self.name = name
        self.model_name = model_name
        self._generation_kwargs = generation_kwargs
        if max_completion_tokens is not None:
            self.max_completion_tokens = max_completion_tokens


class TestOutputTokenKwargName:
    def test_openai_responses(self):
        assert output_token_kwarg_name(_FakeProvider("openai-responses")) == "max_output_tokens"

    def test_google_genai(self):
        assert output_token_kwarg_name(_FakeProvider("google_genai")) == "max_output_tokens"

    def test_other_providers_use_max_tokens(self):
        for name in ("kimi", "anthropic", "openai_legacy", "deepseek"):
            assert output_token_kwarg_name(_FakeProvider(name)) == "max_tokens"

    def test_provider_without_a_name(self):
        assert output_token_kwarg_name(object()) == "max_tokens"


class TestEffectiveMaxOutputTokens:
    def test_generation_kwarg_is_the_live_cap(self):
        provider = _FakeProvider(generation_kwargs={"max_tokens": 96_000})
        assert effective_max_output_tokens(provider) == 96_000

    def test_max_output_tokens_providers(self):
        provider = _FakeProvider(
            "openai-responses", generation_kwargs={"max_output_tokens": 32_768}
        )
        assert effective_max_output_tokens(provider) == 32_768

    def test_max_completion_tokens_attribute(self):
        provider = _FakeProvider(max_completion_tokens=8_192)
        assert effective_max_output_tokens(provider) == 8_192

    def test_generation_kwarg_wins_over_the_attribute(self):
        provider = _FakeProvider(
            generation_kwargs={"max_tokens": 96_000}, max_completion_tokens=8_192
        )
        assert effective_max_output_tokens(provider) == 96_000

    def test_no_cap_returns_none(self):
        assert effective_max_output_tokens(_FakeProvider()) is None
        assert effective_max_output_tokens(_FakeProvider(generation_kwargs={})) is None
        assert effective_max_output_tokens(_FakeProvider(generation_kwargs=None)) is None

    def test_invalid_values_are_ignored(self):
        assert effective_max_output_tokens(_FakeProvider(generation_kwargs={"max_tokens": 0})) is None
        assert (
            effective_max_output_tokens(_FakeProvider(generation_kwargs={"max_tokens": "big"}))
            is None
        )
        assert (
            effective_max_output_tokens(
                _FakeProvider(generation_kwargs={"max_tokens": None})
            )
            is None
        )


def _bare_soul(provider: _FakeProvider | None, config_max_tokens: int | None) -> KimiSoul:
    soul = object.__new__(KimiSoul)
    soul._runtime = SimpleNamespace(
        llm=None
        if provider is None
        else SimpleNamespace(chat_provider=provider, max_context_size=200_000),
        config=SimpleNamespace(max_tokens=config_max_tokens),
    )
    return soul


class TestSoulOutputBudget:
    def test_escalated_provider_cap_wins_over_the_config(self):
        """A think-only retry raised 64K -> 96K; reserve 96K, not the stale 64K."""
        soul = _bare_soul(_FakeProvider(generation_kwargs={"max_tokens": 96_000}), 64_000)
        assert soul._effective_max_output_tokens() == 96_000

    def test_falls_back_to_the_config(self):
        soul = _bare_soul(_FakeProvider(), 64_000)
        assert soul._effective_max_output_tokens() == 64_000

    def test_unset_everywhere_returns_none(self):
        soul = _bare_soul(_FakeProvider(), None)
        assert soul._effective_max_output_tokens() is None

    def test_no_llm_returns_none(self):
        assert _bare_soul(None, 64_000)._effective_max_output_tokens() is None


class TestPostCompactionEstimate:
    """The rebuilt-context estimate must include the non-history request parts."""

    @staticmethod
    def _soul(tools: list[Tool], history: list[Message]) -> KimiSoul:
        soul = object.__new__(KimiSoul)
        soul._agent = SimpleNamespace(toolset=SimpleNamespace(tools=tools))
        soul._runtime = SimpleNamespace(
            llm=SimpleNamespace(
                chat_provider=_FakeProvider(model_name="gpt-4"),
                max_context_size=200_000,
            ),
            config=SimpleNamespace(max_tokens=64_000),
        )
        soul._context = SimpleNamespace(history=history)
        return soul

    def test_tool_schemas_are_counted(self):
        tools = [
            Tool(
                name="read_file",
                description="Read a file from disk. " * 20,
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            )
        ]
        history = [Message(role="user", content=[TextPart(text="hello world")])]
        soul = self._soul(tools, history)

        history_tokens = count_message_tokens(history, model=soul.model_name)
        assert soul._estimate_context_tokens(None) == history_tokens + count_tool_tokens(
            tools, model=soul.model_name
        )
        assert soul._estimate_context_tokens(None) > history_tokens

    def test_system_prompt_is_counted(self):
        history = [Message(role="user", content=[TextPart(text="hello")])]
        soul = self._soul([], history)
        with_prompt = soul._estimate_context_tokens("You are a helpful agent. " * 10)
        without_prompt = soul._estimate_context_tokens(None)
        assert with_prompt > without_prompt
        assert with_prompt == without_prompt + estimate_request_overhead(
            "You are a helpful agent. " * 10, [], model=soul.model_name
        )

    def test_empty_toolset_adds_nothing(self):
        history = [Message(role="user", content=[TextPart(text="hi")])]
        soul = self._soul([], history)
        assert soul._estimate_context_tokens(None) == count_message_tokens(
            history, model=soul.model_name
        )
