from __future__ import annotations

import regex as re
from collections.abc import Sequence
from typing import TYPE_CHECKING

from kosong.message import Message

from kimi_cli.soul.dynamic_injection import DynamicInjection, DynamicInjectionProvider
from kimi_cli.tools.todo import Params as TodoParams
from kimi_cli.tools.todo import TodoList

if TYPE_CHECKING:
    from kimi_cli.soul.kimisoul import KimiSoul

_DONE_REMINDER_TYPE = "done_reminder"

_DONE_REMINDER_TEMPLATE = (
    "You indicated something is finished. Before concluding, call `TodoList` "
    "to verify no pending tasks remain, then continue until all todos are done.\n"
    'Original user prompt:\n{user_prompt}'
    
)

# Single-word completion markers matched with word boundaries.
_SINGLE_WORD_KEYWORDS = (
    "summary|done|finished|completed|complete|resolved|fixed|closed|verified|approved|"
    "shipped|delivered|merged|deployed|released|published|finalized|finalised|"
    "ready|set|concluded|accomplished|outstanding|addressed|handled|cleared|settled"
)

_COMPLETION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(rf"\b(?:{_SINGLE_WORD_KEYWORDS})\b", re.IGNORECASE),
    # Common multi-word phrases with flexible whitespace/punctuation.
    re.compile(r"\ball\s+done\b", re.IGNORECASE),
    re.compile(r"\ball\s+set\b", re.IGNORECASE),
    re.compile(r"\bwrapped\s+up\b", re.IGNORECASE),
    re.compile(r"\btaken\s+care\s+of\b", re.IGNORECASE),
    re.compile(r"\bgood\s+to\s+go\b", re.IGNORECASE),
    re.compile(r"\bready\s+to\s+go\b", re.IGNORECASE),
    re.compile(r"\b(?:completed|finished)\s+successfully\b", re.IGNORECASE),
    re.compile(r"\bsuccessfully\s+(?:completed|finished)\b", re.IGNORECASE),
    re.compile(r"\b(?:task|tasks|work|implementation)\s+complete\b", re.IGNORECASE),
    re.compile(r"\b(?:is|are|now)\s+complete\b", re.IGNORECASE),
    re.compile(r"\b(?:is|are|has\s+been|have\s+been)\s+done\b", re.IGNORECASE),
    re.compile(r"\bno\s+(?:further|more)\s+(?:action|steps?)\b", re.IGNORECASE),
    re.compile(r"\bnothing\s+left\b", re.IGNORECASE),
    re.compile(r"\bit\s+works\b", re.IGNORECASE),
    re.compile(r"\bworking\s+as\s+expected\b", re.IGNORECASE),
    re.compile(r"\b(?:completed|finished|fixed|resolved)\s+(?:the|all)\b", re.IGNORECASE),
    re.compile(r"\bdone\s+(?:with|all)\b", re.IGNORECASE),
    re.compile(r"\bfinalized\s+the\b", re.IGNORECASE),
    re.compile(r"\bfinalised\s+the\b", re.IGNORECASE),
    re.compile(r"\bresolved\s+all\b", re.IGNORECASE),
    re.compile(r"\bfixed\s+all\b", re.IGNORECASE),
    re.compile(r"\bcompleted\s+all\b", re.IGNORECASE),
    re.compile(r"\bfinished\s+all\b", re.IGNORECASE),
]

# Regex to detect a non-done todo status in TodoList read output.
_PENDING_TODO_RE = re.compile(r"-\s+\[(?!done\b)[^\]]+\]")

_MAX_PROMPT_LEN = 4096


def _truncate_prompt(text: str, max_len: int = _MAX_PROMPT_LEN) -> str:
    """Truncate *text* to *max_len* chars, appending ``...`` if truncated."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def _find_last_user_prompt(history: Sequence[Message], soul: KimiSoul) -> str:
    """Return the text of the most recent real user message before the last
    assistant message, skipping system-reminder messages.

    Falls back to ``soul._current_turn_user_text`` if no user message found.
    """
    from kimi_cli.soul.message import is_system_reminder_message

    last_assistant_idx = -1
    for idx, msg in enumerate(history):
        if msg.role == "assistant":
            last_assistant_idx = idx

    # No assistant message at all - no user prompt to find
    if last_assistant_idx == -1:
        return getattr(soul, "_current_turn_user_text", "") or ""

    # Walk backwards from last_assistant_idx to find the most recent
    # real user message (skip system-reminder injections).
    for idx in range(last_assistant_idx - 1, -1, -1):
        msg = history[idx]
        if msg.role == "user" and not is_system_reminder_message(msg):
            text = msg.extract_text(" ").strip()
            if text:
                return text
            break  # found a user message but it was empty

    # Fallback to turn-starting user text
    return getattr(soul, "_current_turn_user_text", "") or ""


def _has_pending_todos(todo_output: str) -> bool:
    """Return True if TodoList read output contains any pending/in-progress item."""
    return bool(_PENDING_TODO_RE.search(todo_output))


def _contains_completion_keyword(text: str) -> bool:
    """Return True if the text contains any completion keyword/phrase."""
    return any(pattern.search(text) for pattern in _COMPLETION_PATTERNS)


def _todolist_tool_available(soul: KimiSoul) -> bool:
    """Return True when the agent's toolset exposes a non-hidden TodoList tool."""
    toolset = soul._agent.toolset
    try:
        tools = toolset.tools
    except Exception:
        return False
    return any(getattr(tool, "name", None) == "TodoList" for tool in tools)


class DoneReminderProvider(DynamicInjectionProvider):
    """Injects a reminder to verify pending todos when completion language is detected."""

    def __init__(self, enabled: bool = True, cooldown_steps: int = 1) -> None:
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

        text = last_assistant.extract_text(" ")
        if not text or not _contains_completion_keyword(text):
            return []

        if not _todolist_tool_available(soul):
            return []

        todo_tool = TodoList(soul._runtime)
        result = await todo_tool(TodoParams())
        output = result.output if isinstance(result.output, str) else ""
        if not _has_pending_todos(output):
            return []

        step_no = soul._current_step_no
        if (
            self._last_injected_step is not None
            and step_no - self._last_injected_step <= self._cooldown_steps
        ):
            return []

        user_prompt = _find_last_user_prompt(history, soul)
        truncated = _truncate_prompt(user_prompt)
        content = _DONE_REMINDER_TEMPLATE.format(user_prompt=truncated)

        self._last_injected_step = step_no
        self._last_injected_assistant_index = assistant_index
        return [DynamicInjection(type=_DONE_REMINDER_TYPE, content=content)]

    async def on_context_compacted(self) -> None:
        """Reset throttling so the reminder can fire again after compaction."""
        self._last_injected_step = None
        self._last_injected_assistant_index = -1

    async def on_afk_changed(self, enabled: bool) -> None:
        """Reset throttling when afk mode changes."""
        _ = enabled
        self._last_injected_step = None
        self._last_injected_assistant_index = -1
