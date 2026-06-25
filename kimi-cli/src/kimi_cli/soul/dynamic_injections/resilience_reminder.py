from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

from kosong.message import Message, TextPart, ThinkPart

from kimi_cli.soul.dynamic_injection import DynamicInjection, DynamicInjectionProvider

if TYPE_CHECKING:
    from kimi_cli.soul.kimisoul import KimiSoul

_RESILIENCE_REMINDER_TYPE = "resilience_reminder"
_RESILIENCE_REMINDER_TEMPLATE = (
    "Do not give up or declare the task finished. The user's request must be "
    "fully addressed. Keep trying: research, decompose the problem, test a smaller "
    "case, or ask a targeted clarifying question. Persist until it is complete."
)

# Patterns that detect resignation, defeat, or "call it done" language in the
# assistant's latest output.  Each pattern is matched case-insensitively.
_GIVE_UP_PATTERNS: list[re.Pattern[str]] = [
    # Explicit defeat
    re.compile(r"\bgiv(?:ing\s+up|e\s+up)\b", re.IGNORECASE),
    re.compile(r"\b(?:this\s+)?(?:can'?t|cannot)\s+be\s+(?:done|expressed|implemented|fixed|addressed|resolved)\b", re.IGNORECASE),
    re.compile(r"\b(?:can'?t|cannot)\s+(?:fix|address|resolve)\b", re.IGNORECASE),
    re.compile(r"\b(?:not\s+possible|impossible|intractable|irreconcilable|insurmountable)\b", re.IGNORECASE),
    re.compile(r"\bno\s+viable\s+(?:approach|solution)\b", re.IGNORECASE),
    re.compile(r"\b(?:architectural|fundamental)\s+limitation(?:s)?\b", re.IGNORECASE),
    re.compile(r"\brequire(?:s|d)?\s+(?:significant|inline)\s+(?:compiler\s+changes|SPIR-V)\b", re.IGNORECASE),
    re.compile(r"\bnot\s+(?:feasible|workable|practical|realistic|worth\s+(?:the\s+)?effort|fixable)\b", re.IGNORECASE),
    re.compile(r"\b(?:won'?t|will\s+not)\s+fix\b", re.IGNORECASE),
    re.compile(r"\bunfixable\b", re.IGNORECASE),
    # Pragmatic resignation / documentation fallback
    re.compile(r"\bpragmatic(?:ally)?\b", re.IGNORECASE),
    re.compile(r"\bdocument\s+(?:what\s+works\s+and\s+what\s+doesn'?t|the\s+limitation)\b", re.IGNORECASE),
    re.compile(r"\bstep\s+back\b", re.IGNORECASE),
    re.compile(r"\bgiven\s+(?:the\s+time(?:\s+I'?ve\s+spent)?|the\s+(?:time|constraints))\b", re.IGNORECASE),
    re.compile(r"\blet\s+me\s+(?:finalize|update\s+the\s+report\s+and\s+finalize)\b", re.IGNORECASE),
    re.compile(r"\baccept\s+that\s+these\s+operations\s+can'?t\b", re.IGNORECASE),
    re.compile(r"\bthe\s+only\s+viable\s+approaches\s+are\b", re.IGNORECASE),
    re.compile(r"\bdiminishing\s+returns\b", re.IGNORECASE),
    re.compile(r"\btoo\s+(?:complex|complicated|risky)\s+to\b", re.IGNORECASE),
    # External / pre-existing excuses
    re.compile(r"\bpre[-\s]?existing\b", re.IGNORECASE),
    re.compile(r"\b(?:existing|legacy|inherited)\s+(?:limitation|issue|problem)\b", re.IGNORECASE),
    re.compile(r"\b(?:by\s+design|works\s+as\s+designed)\b", re.IGNORECASE),
    re.compile(r"\b(?:out\s+of\s+scope|not\s+in\s+scope|beyond\s+the\s+scope|outside\s+the\s+scope)\b", re.IGNORECASE),
    re.compile(r"\bnot\s+(?:my|our)\s+responsibility\b", re.IGNORECASE),
    re.compile(r"\b(?:upstream|third-party)\s+(?:issue|limitation)\b", re.IGNORECASE),
    re.compile(r"\bexternal\s+dependency\b", re.IGNORECASE),
    re.compile(r"\b(?:blocked\s+(?:by|on)|waiting\s+for|depends\s+on)\b", re.IGNORECASE),
    # Abandonment framing
    re.compile(r"\b(?:I\s+think\s+)?(?:we\s+should|let'?s|it\s+is\s+time\s+to)\s+stop\b", re.IGNORECASE),
    re.compile(r"\bcall\s+it\s+(?:done|complete)\b", re.IGNORECASE),
    re.compile(r"\bdeclare\s+(?:victory|defeat)\b", re.IGNORECASE),
    re.compile(r"\bthrow\s+in\s+the\s+towel\b", re.IGNORECASE),
    re.compile(r"\bcut\s+our\s+losses\b", re.IGNORECASE),
    re.compile(r"\b(?:let'?s\s+|we\s+should\s+|\b)move\s+on\b", re.IGNORECASE),
    re.compile(r"\b(?:park|shelve|table|put\s+(?:this|it)\s+(?:aside|on\s+hold))\b", re.IGNORECASE),
    re.compile(r"\bon\s+hold\b", re.IGNORECASE),
    re.compile(r"\bpunt\s+(?:on|this)\b", re.IGNORECASE),
    re.compile(r"\bdefer\s+(?:this|indefinitely)\b", re.IGNORECASE),
    # Concessive framing
    re.compile(r"\b(?:there\s+(?:is|s)|there'?s)\s+no\s+way\b", re.IGNORECASE),
    re.compile(r"\bno\s+way\s+to\b", re.IGNORECASE),
    re.compile(r"\b(?:we\s+have\s+to|must)\s+accept\b", re.IGNORECASE),
    re.compile(r"\b(?:this\s+is|I\s+realize\s+(?:this\s+approach\s+is)?)\s+problematic\b", re.IGNORECASE),
]


def _contains_give_up_language(text: str) -> bool:
    """Return True if *text* contains resignation or give-up language."""
    return any(pattern.search(text) for pattern in _GIVE_UP_PATTERNS)


def _extract_text_and_thinking(message: Message) -> str:
    """Concatenate all text and thinking parts from a message."""
    parts: list[str] = []
    for part in message.content:
        if isinstance(part, TextPart):
            parts.append(part.text)
        elif isinstance(part, ThinkPart):
            parts.append(part.think)
    return " ".join(parts)


class ResilienceReminderProvider(DynamicInjectionProvider):
    """Injects a resilience reminder when the assistant emits give-up language."""

    def __init__(self, enabled: bool = True, cooldown_steps: int = 5) -> None:
        self._enabled = enabled
        self._cooldown_steps = cooldown_steps
        self._last_injected_step: int | None = None
        self._last_injected_assistant_index: int = -1

    async def get_injections(
        self,
        history: Sequence[Message],
        soul: KimiSoul,
    ) -> list[DynamicInjection]:
        if not self._enabled or soul.is_subagent:
            return []

        # Locate the most recent assistant message and its index among assistant messages.
        assistant_index = -1
        last_assistant: Message | None = None
        for msg in history:
            if msg.role == "assistant":
                assistant_index += 1
                last_assistant = msg

        if last_assistant is None or assistant_index <= self._last_injected_assistant_index:
            return []

        text = _extract_text_and_thinking(last_assistant)
        if not text or not _contains_give_up_language(text):
            return []

        step_no = soul._current_step_no
        if (
            self._last_injected_step is not None
            and step_no - self._last_injected_step <= self._cooldown_steps
        ):
            return []

        self._last_injected_step = step_no
        self._last_injected_assistant_index = assistant_index
        return [DynamicInjection(type=_RESILIENCE_REMINDER_TYPE, content=_RESILIENCE_REMINDER_TEMPLATE)]

    async def on_context_compacted(self) -> None:
        """Reset throttling so the reminder can fire again after compaction."""
        self._last_injected_step = None
        self._last_injected_assistant_index = -1

    async def on_afk_changed(self, enabled: bool) -> None:
        """Reset throttling when afk mode changes."""
        _ = enabled
        self._last_injected_step = None
        self._last_injected_assistant_index = -1
