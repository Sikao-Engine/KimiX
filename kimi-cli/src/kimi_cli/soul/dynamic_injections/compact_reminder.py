from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from kosong.message import Message

from kimi_cli.soul.dynamic_injection import DynamicInjection, DynamicInjectionProvider

if TYPE_CHECKING:
    from kimi_cli.soul.kimisoul import KimiSoul

_COMPACT_REMINDER_TYPE = "compact_reminder"

# Hard floor: never inject the compact reminder while context usage is below
# this ratio, regardless of the configured ``threshold``. Context is not "full"
# enough to warrant compaction below this point.
MIN_CONTEXT_USAGE = 0.30

_COMPACT_REMINDER_TEMPLATE = (
    "Context {usage:.0%} full ({tokens}/{max_tokens} tokens). "
    "Call `Compact` after completing the current atomic task, "
    "before auto-compaction forces it. "
    "Optionally pass an instruction to guide what to preserve."
)


class CompactReminderProvider(DynamicInjectionProvider):
    """Injects a context-compaction reminder when context usage exceeds a threshold."""

    def __init__(
        self,
        threshold: float = 0.70,
        cooldown_steps: int = 5,
        min_usage: float = MIN_CONTEXT_USAGE,
    ) -> None:
        self._threshold = threshold
        self._cooldown_steps = cooldown_steps
        # Never trigger below this absolute floor, even if ``threshold`` is set
        # lower than it.
        self._min_usage = min_usage
        self._last_injected_step: int | None = None
        self._last_injected_usage: float = 0.0

    async def get_injections(
        self,
        history: Sequence[Message],
        soul: KimiSoul,
    ) -> list[DynamicInjection]:
        _ = history

        # Only inject for root sessions (skip subagents).
        if soul.is_subagent:
            return []

        # Use the pending-inclusive token count so the reminder fires based on
        # the real context size (tool results appended since the last LLM call
        # are counted), matching the auto-compaction trigger.
        max_tokens = soul.status.max_context_tokens
        if max_tokens:
            context_usage = soul.context.token_count_with_pending / max_tokens
        else:
            context_usage = soul.status.context_usage

        if context_usage < self._min_usage:
            # Context is nowhere near full; never nag about compaction below
            # the hard floor (defaults to 30%), even if ``threshold`` is set
            # lower than it.
            return []

        if context_usage < self._threshold:
            return []

        # Throttle: skip if we already injected and usage hasn't grown enough
        # or if not enough steps have passed since the last injection.
        step_no = soul._current_step_no
        if self._last_injected_step is not None:
            steps_since = step_no - self._last_injected_step
            usage_growth = context_usage - self._last_injected_usage
            if steps_since <= self._cooldown_steps or usage_growth < 0.05:
                return []

        self._last_injected_step = step_no
        self._last_injected_usage = context_usage

        status = soul.status
        content = _COMPACT_REMINDER_TEMPLATE.format(
            usage=context_usage,
            tokens=status.context_tokens,
            max_tokens=status.max_context_tokens,
        )
        return [DynamicInjection(type=_COMPACT_REMINDER_TYPE, content=content)]

    async def on_context_compacted(self) -> None:
        """Reset throttling state so the reminder can fire again after compaction."""
        self._last_injected_step = None
        self._last_injected_usage = 0.0

    async def on_afk_changed(self, enabled: bool) -> None:
        """Reset throttling state (same pattern as afk provider)."""
        _ = enabled
        self._last_injected_step = None
        self._last_injected_usage = 0.0
