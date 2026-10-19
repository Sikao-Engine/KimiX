"""Tests for ``repair_tool_message_pairing`` / ``normalize_history``.

Interrupted steps (steer injection mid context-growth, session restarts) can
persist histories where an assistant message with ``tool_calls`` is not
immediately followed by its tool-result messages.  Strict backends reject
such requests with a 400.  The repair pass must reorder/synthesize/drop
messages so the history is always valid.
"""

from __future__ import annotations

from kosong.contrib.chat_provider.common import (
    normalize_history,
    repair_tool_message_pairing,
)
from kosong.message import Message, ToolCall


def _call(call_id: str, name: str = "tool") -> ToolCall:
    return ToolCall(
        id=call_id,
        function=ToolCall.FunctionBody(name=name, arguments="{}"),
    )


def _assistant(*call_ids: str, text: str = "") -> Message:
    return Message(
        role="assistant",
        content=text,
        tool_calls=[_call(cid, f"name_{cid}") for cid in call_ids] or None,
    )


def _tool(call_id: str, text: str = "result") -> Message:
    return Message(role="tool", content=text, tool_call_id=call_id)


def _user(text: str = "hi") -> Message:
    return Message(role="user", content=text)


def _roles(history) -> list[str]:
    return [m.role for m in history]


def _assert_valid_pairing(history) -> None:
    """Every assistant tool_call is immediately followed by its tool results."""
    index = 0
    while index < len(history):
        message = history[index]
        if message.role == "tool":
            raise AssertionError(f"orphan tool message at {index}")
        if message.role == "assistant" and message.tool_calls:
            expected = [tc.id for tc in message.tool_calls]
            for offset, call_id in enumerate(expected, start=1):
                follower = history[index + offset]
                assert follower.role == "tool", f"call {call_id} not followed by tool"
                assert follower.tool_call_id == call_id
            index += len(expected)
        index += 1


def test_well_formed_history_returned_unchanged() -> None:
    history = [
        _user(),
        _assistant("a1"),
        _tool("a1"),
        _assistant("a2", "a3"),
        _tool("a2"),
        _tool("a3"),
        _assistant(text="done"),
    ]
    result = repair_tool_message_pairing(history)
    assert result is history


def test_empty_and_tool_free_histories_unchanged() -> None:
    assert repair_tool_message_pairing([]) == []
    history = [_user(), _assistant(text="answer"), _user()]
    assert repair_tool_message_pairing(history) is history


def test_interleaved_user_message_is_moved_after_tool_result() -> None:
    """The exact corruption from a steer interrupting context growth:
    assistant(tool_calls) -> user -> tool."""
    history = [
        _user("run the checks"),
        _assistant("c1"),
        _user("Background subagent 'x' completed. Output: f.md"),
        _tool("c1", "Subagent finished"),
    ]
    repaired = repair_tool_message_pairing(history)
    assert _roles(repaired) == ["user", "assistant", "tool", "user"]
    _assert_valid_pairing(repaired)
    # The original message objects are reused, not mutated.
    assert repaired[2] is history[3]
    assert repaired[3] is history[2]


def test_missing_tool_result_gets_placeholder() -> None:
    history = [_user(), _assistant("c1")]
    repaired = repair_tool_message_pairing(history)
    assert _roles(repaired) == ["user", "assistant", "tool"]
    placeholder = repaired[2]
    assert placeholder.tool_call_id == "c1"
    assert "interrupted" in placeholder.extract_text()
    _assert_valid_pairing(repaired)


def test_orphan_tool_message_dropped() -> None:
    history = [_user(), _tool("ghost"), _assistant(text="ok")]
    repaired = repair_tool_message_pairing(history)
    assert _roles(repaired) == ["user", "assistant"]


def test_duplicate_tool_result_dropped() -> None:
    dup = _tool("c1", "second")
    history = [_user(), _assistant("c1"), _tool("c1", "first"), dup]
    repaired = repair_tool_message_pairing(history)
    assert _roles(repaired) == ["user", "assistant", "tool"]
    assert repaired[2].extract_text() == "first"


def test_multiple_calls_partially_answered() -> None:
    history = [
        _assistant("c1", "c2", "c3"),
        _tool("c1"),
        _user("steer"),
        _tool("c3"),
    ]
    repaired = repair_tool_message_pairing(history)
    assert _roles(repaired) == ["assistant", "tool", "tool", "tool", "user"]
    assert [m.tool_call_id for m in repaired[1:4]] == ["c1", "c2", "c3"]
    # c2's result was synthesized.
    assert "interrupted" in repaired[2].extract_text()
    _assert_valid_pairing(repaired)


def test_results_from_later_assistant_block_not_stolen() -> None:
    history = [
        _assistant("a1"),
        _assistant("b1"),
        _tool("b1"),
        _tool("a1"),
    ]
    repaired = repair_tool_message_pairing(history)
    assert _roles(repaired) == ["assistant", "tool", "assistant", "tool"]
    assert repaired[1].tool_call_id == "a1"
    assert repaired[3].tool_call_id == "b1"
    _assert_valid_pairing(repaired)


def test_repair_is_idempotent() -> None:
    history = [
        _user(),
        _assistant("c1"),
        _user("steer"),
        _tool("c1"),
        _assistant("c2"),
    ]
    once = repair_tool_message_pairing(history)
    twice = repair_tool_message_pairing(once)
    assert twice is once or [m.model_dump() for m in twice] == [
        m.model_dump() for m in once
    ]
    _assert_valid_pairing(twice)


def test_normalize_history_composes_repair_and_id_normalization() -> None:
    # ':' is rejected by strict backends and the pairing is broken.
    history = [
        _assistant("Read:9"),
        _user("steer"),
        _tool("Read:9"),
    ]
    normalized = normalize_history(history)
    _assert_valid_pairing(normalized)
    assert normalized[0].tool_calls is not None
    normalized_id = normalized[0].tool_calls[0].id
    assert ":" not in normalized_id
    assert normalized[1].tool_call_id == normalized_id
