"""Tests for CompactReminderProvider."""

from __future__ import annotations

from unittest.mock import MagicMock

from kimi_cli.soul.dynamic_injections.compact_reminder import (
    _COMPACT_REMINDER_TYPE,
    CompactReminderProvider,
)


def _mock_soul(
    context_usage: float = 0.0,
    context_tokens: int = 1000,
    max_context_tokens: int = 100000,
    is_subagent: bool = False,
    step_no: int = 1,
) -> MagicMock:
    soul = MagicMock()
    soul.is_subagent = is_subagent
    soul._current_step_no = step_no
    status = MagicMock()
    status.context_usage = context_usage
    status.context_tokens = context_tokens
    status.max_context_tokens = max_context_tokens
    soul.status = status
    # The provider now reads the pending-inclusive token count from the context,
    # so derive it from the requested usage for a consistent mock.
    context = MagicMock()
    context.token_count_with_pending = int(context_usage * max_context_tokens)
    soul.context = context
    return soul


async def test_reminder_injected_when_usage_high() -> None:
    """Verify reminder is injected when context_usage >= threshold."""
    provider = CompactReminderProvider(threshold=0.70)
    result = await provider.get_injections([], _mock_soul(context_usage=0.75))
    assert len(result) == 1
    assert result[0].type == _COMPACT_REMINDER_TYPE
    assert "75%" in result[0].content
    assert "Compact" in result[0].content
    assert "auto-compaction" in result[0].content


async def test_no_reminder_when_usage_low() -> None:
    """Verify no reminder when context_usage < threshold."""
    provider = CompactReminderProvider(threshold=0.70)
    result = await provider.get_injections([], _mock_soul(context_usage=0.50))
    assert result == []


async def test_reminder_throttled_by_steps() -> None:
    """Verify reminder is not re-injected on consecutive steps
    without enough steps passing, even when usage has grown."""
    provider = CompactReminderProvider(threshold=0.70, cooldown_steps=5)

    # First injection at step 1
    result1 = await provider.get_injections(
        [], _mock_soul(context_usage=0.80, step_no=1)
    )
    assert len(result1) == 1

    # Usage has grown significantly but not enough steps passed — throttled
    result2 = await provider.get_injections(
        [], _mock_soul(context_usage=0.90, step_no=2)
    )
    assert result2 == []

    # After cooldown_steps and usage grown — should inject again
    result3 = await provider.get_injections(
        [], _mock_soul(context_usage=0.90, step_no=7)
    )
    assert len(result3) == 1


async def test_reminder_throttled_by_usage_growth() -> None:
    """Verify reminder is not re-injected without significant usage growth
    even if steps have passed."""
    provider = CompactReminderProvider(threshold=0.70, cooldown_steps=5)

    # First injection
    result1 = await provider.get_injections(
        [], _mock_soul(context_usage=0.75, step_no=1)
    )
    assert len(result1) == 1

    # After enough steps but usage hasn't grown by 5% — throttled
    result2 = await provider.get_injections(
        [], _mock_soul(context_usage=0.77, step_no=7)
    )
    assert result2 == []

    # After enough steps and usage grown by >= 5% — injects again
    result3 = await provider.get_injections(
        [], _mock_soul(context_usage=0.82, step_no=13)
    )
    assert len(result3) == 1


async def test_reminder_reset_after_compaction() -> None:
    """Verify on_context_compacted() resets throttling."""
    provider = CompactReminderProvider(threshold=0.70, cooldown_steps=5)

    # Inject once
    result1 = await provider.get_injections(
        [], _mock_soul(context_usage=0.80, step_no=1)
    )
    assert len(result1) == 1

    # Next step — throttled
    result2 = await provider.get_injections(
        [], _mock_soul(context_usage=0.80, step_no=2)
    )
    assert result2 == []

    # Simulate compaction
    await provider.on_context_compacted()

    # After compaction, should inject again even at same step/usage
    result3 = await provider.get_injections(
        [], _mock_soul(context_usage=0.80, step_no=3)
    )
    assert len(result3) == 1


async def test_no_reminder_for_subagent() -> None:
    """Verify subagent sessions don't get the reminder."""
    provider = CompactReminderProvider(threshold=0.70)
    result = await provider.get_injections(
        [], _mock_soul(context_usage=0.90, is_subagent=True)
    )
    assert result == []


async def test_reminder_content_includes_usage() -> None:
    """Verify the injected text contains the usage percentage and token counts."""
    provider = CompactReminderProvider(threshold=0.70)
    result = await provider.get_injections(
        [],
        _mock_soul(
            context_usage=0.72,
            context_tokens=72000,
            max_context_tokens=100000,
        ),
    )
    assert len(result) == 1
    content = result[0].content
    assert "72%" in content
    assert "72000" in content
    assert "100000" in content


async def test_reminder_at_threshold_boundary() -> None:
    """Verify reminder is injected when usage equals threshold exactly."""
    provider = CompactReminderProvider(threshold=0.70)
    result = await provider.get_injections(
        [], _mock_soul(context_usage=0.70)
    )
    assert len(result) == 1


async def test_on_afk_changed_resets_throttling() -> None:
    """Verify on_afk_changed() resets throttling state."""
    provider = CompactReminderProvider(threshold=0.70, cooldown_steps=5)

    # Inject once
    result1 = await provider.get_injections(
        [], _mock_soul(context_usage=0.80, step_no=1)
    )
    assert len(result1) == 1

    # Throttled
    result2 = await provider.get_injections(
        [], _mock_soul(context_usage=0.80, step_no=2)
    )
    assert result2 == []

    # AFK changed
    await provider.on_afk_changed(True)

    # Should be able to inject again
    result3 = await provider.get_injections(
        [], _mock_soul(context_usage=0.80, step_no=3)
    )
    assert len(result3) == 1
