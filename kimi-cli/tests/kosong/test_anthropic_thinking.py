"""Unit tests for Anthropic thinking mode dispatch."""

import pytest

pytest.importorskip("anthropic", reason="Optional contrib dependency not installed")

from kosong.contrib.chat_provider.anthropic import (
    _clamp_effort,  # pyright: ignore[reportPrivateUsage]
    _supports_adaptive_thinking,  # pyright: ignore[reportPrivateUsage]
    _supports_effort_param,  # pyright: ignore[reportPrivateUsage]
)


@pytest.mark.parametrize(
    "model,expected",
    [
        # Opus 4.7 family (adaptive-only per Anthropic docs)
        ("claude-opus-4-7", True),
        ("claude-opus-4-7-20260301", True),
        ("claude-opus-4.7", True),
        ("CLAUDE-OPUS-4-7", True),  # case-insensitive
        # Opus 4.6 / Sonnet 4.6 (adaptive preferred)
        ("claude-opus-4-6", True),
        ("claude-opus-4-6-20260205", True),
        ("claude-opus-4.6", True),
        ("claude-sonnet-4-6", True),
        ("claude-sonnet-4-6-20260301", True),
        ("claude-sonnet-4.6", True),
        # Mythos Preview (no version number, explicit marker)
        ("claude-mythos-preview", True),
        ("claude-mythos", True),
        # Future version extrapolation (regex-driven)
        ("claude-opus-4-8", True),
        ("claude-opus-4-9", True),
        ("claude-opus-4-10", True),  # two-digit minor
        ("claude-opus-5-0", True),
        ("claude-opus-5-0-20270101", True),
        ("claude-sonnet-5-0", True),
        ("claude-haiku-4-6", True),  # haiku family nominally included if >= 4.6
        ("claude-haiku-5-0", True),
        # Bedrock / Vertex / proxy prefixes must not defeat detection
        ("anthropic.claude-opus-4-7-v1:0", True),
        ("aws/claude-opus-4-7", True),
        ("bedrock/anthropic.claude-opus-4-6-v1:0", True),
        ("claude-opus-4-7@20260101", True),
        # Pre-4.6 models (legacy budget_tokens required)
        ("claude-opus-4", False),
        ("claude-opus-4-0", False),
        ("claude-opus-4-5", False),
        ("claude-opus-4-5-20251001", False),
        ("claude-opus-3-5", False),
        ("claude-opus-3-5-sonnet-20241022", False),  # edge: embedded "sonnet"
        ("claude-sonnet-4-20250514", False),  # Sonnet 4 with date, no minor
        ("claude-sonnet-4-5", False),
        ("claude-sonnet-4-5-20250929", False),
        ("claude-sonnet-3-5", False),
        ("claude-sonnet-3-7", False),
        ("claude-haiku-3-5", False),
        ("claude-haiku-4-5", False),
        ("claude-haiku-4-5-20251001", False),
        # Non-Claude models / garbage input
        ("gpt-4", False),
        ("gpt-4-turbo", False),
        ("gemini-2.5-pro", False),
        ("", False),
        ("unknown-model", False),
        ("claude", False),  # no family word
    ],
)
def test_supports_adaptive_thinking(model: str, expected: bool) -> None:
    assert _supports_adaptive_thinking(model) is expected


@pytest.mark.parametrize(
    "supported_efforts,effort,expected",
    [
        # Full effort set: every level passes through unchanged.
        ({"low", "medium", "high", "xhigh", "max"}, "low", "low"),
        ({"low", "medium", "high", "xhigh", "max"}, "medium", "medium"),
        ({"low", "medium", "high", "xhigh", "max"}, "high", "high"),
        ({"low", "medium", "high", "xhigh", "max"}, "xhigh", "xhigh"),
        ({"low", "medium", "high", "xhigh", "max"}, "max", "max"),
        # Cap at high: xhigh and max clamp down to high.
        ({"low", "medium", "high"}, "max", "high"),
        ({"low", "medium", "high"}, "xhigh", "high"),
        ({"low", "medium", "high"}, "high", "high"),
        # Cap at xhigh: max clamps down to xhigh.
        ({"low", "medium", "high", "xhigh"}, "max", "xhigh"),
        ({"low", "medium", "high", "xhigh"}, "xhigh", "xhigh"),
        # low/medium/high always pass through unchanged.
        ({"low", "medium", "high"}, "low", "low"),
        ({"low", "medium"}, "medium", "medium"),
        # Empty set: fall back to the safe universal ceiling.
        (set(), "max", "high"),
        (set(), "low", "high"),
        # off disables thinking regardless of supported set.
        ({"low", "medium", "high"}, "off", "off"),
        (set(), "off", "off"),
    ],
)
def test_clamp_effort(supported_efforts: set[str], effort: str, expected: str) -> None:
    assert _clamp_effort(effort, frozenset(supported_efforts)) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "model,expected",
    [
        # Adaptive-capable models all support effort (via adaptive path)
        ("claude-opus-4-7", True),
        ("claude-opus-4-7-20260301", True),
        ("claude-opus-4-6", True),
        ("claude-opus-4-6-20260205", True),
        ("claude-sonnet-4-6", True),
        ("claude-mythos-preview", True),
        ("claude-opus-5-0", True),  # future adaptive via regex extrapolation
        # Opus 4.5 is explicitly listed by Anthropic docs as supporting effort
        # alongside legacy budget_tokens thinking.
        ("claude-opus-4-5", True),
        ("claude-opus-4-5-20251001", True),
        ("claude-opus-4.5", True),
        ("anthropic.claude-opus-4-5-v1:0", True),  # Bedrock prefix
        # Other pre-4.6 Claude 4 models are NOT explicitly listed as supporting
        # effort. Be conservative and return False to avoid 400 errors — users
        # lose no capability since "high" is the default anyway.
        ("claude-sonnet-4-20250514", False),
        ("claude-sonnet-4-5", False),
        ("claude-sonnet-4-5-20250929", False),
        ("claude-haiku-4-5", False),
        ("claude-haiku-4-5-20251001", False),
        # Claude 3.x family does NOT support effort (predates the parameter).
        # Both the old and new naming formats must be detected.
        ("claude-sonnet-3-7", False),
        ("claude-sonnet-3-7-20250219", False),
        ("claude-sonnet-3-5", False),
        ("claude-opus-3-5", False),
        ("claude-haiku-3-5", False),
        ("claude-3-opus-20240229", False),  # old format
        ("claude-3-5-sonnet-20240620", False),  # old format
        ("claude-3-5-haiku-20241022", False),  # old format
        ("anthropic.claude-3-5-sonnet-20240620-v1:0", False),  # Bedrock + old format
        # Non-Claude / garbage
        ("gpt-4", False),
        ("", False),
        ("claude-2.1", False),
    ],
)
def test_supports_effort_param(model: str, expected: bool) -> None:
    assert _supports_effort_param(model) is expected
