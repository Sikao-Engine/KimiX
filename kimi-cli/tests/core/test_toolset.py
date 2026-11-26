"""Tests for KimiToolset hide/unhide and deduplication functionality."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import Any, override

from kosong.tooling import CallableTool2, ToolError, ToolOk, ToolReturnValue
from kosong.tooling.error import ToolNotFoundError as KosongToolNotFoundError
from pydantic import BaseModel

import kimi_cli.soul.toolset as toolset_module

from kimi_cli.soul.toolset import (
    _CYCLE_FORCE_STOP,
    _CYCLE_REMINDER_AT,
    _DIFF_ARGS_FORCE_STOP,
    _DIFF_ARGS_REMINDER_2_AT,
    _DIFF_ARGS_REMINDER_3_AT,
    _DIFF_ARGS_REMINDER_AT,
    _MAX_CYCLE_LENGTH,
    _OUTPUT_DUMP_MAX_FILES,
    _OUTPUT_DUMP_SUBDIR,
    _PLATFORM_REDIRECTS_NORM,
    _STREAK_FORCE_STOP,
    _STREAK_REMINDER_2_AT,
    _STREAK_REMINDER_3_AT,
    _STREAK_REMINDER_AT,
    _TURN_TOTAL_REMINDER_START,
    KimiToolset,
    _adjacent_streak,
    _build_platform_redirects,
    _collect_candidates,
    _cycle_occurrences,
    _has_reasoning_parts,
    _is_action_tool,
    _output_to_dump_text,
    _parse_stringified_arguments,
    _repair_argument_format,
    _repair_todo_arguments,
    _unwrap_nested_arguments,
    _write_output_dump,
)
from kimi_cli.tools import RETIRED_TODO_TOOL_NAMES
from kimi_cli.wire.types import (
    ContentPart,
    ImageURLPart,
    TextPart,
    ThinkPart,
    ToolCall,
    ToolResult,
)


class DummyParams(BaseModel):
    value: str = ""


class DummyToolA(CallableTool2[DummyParams]):
    name: str = "ToolA"
    description: str = "Tool A"
    params: type[DummyParams] = DummyParams

    async def __call__(self, params: DummyParams) -> ToolReturnValue:
        return ToolOk(output="a")


class DummyToolB(CallableTool2[DummyParams]):
    name: str = "ToolB"
    description: str = "Tool B"
    params: type[DummyParams] = DummyParams

    async def __call__(self, params: DummyParams) -> ToolReturnValue:
        return ToolOk(output="b")


class DummyToolC(CallableTool2[DummyParams]):
    name: str = "LongNamedTool"
    description: str = "A tool with a longer name for typo tests"
    params: type[DummyParams] = DummyParams

    async def __call__(self, params: DummyParams) -> ToolReturnValue:
        return ToolOk(output="long")


class DummyBrowserUseTool(CallableTool2[DummyParams]):
    """Stands in for the real ``browser_use`` tool: identical calls are clicks."""

    name: str = "browser_use"
    description: str = "Drive a browser"
    params: type[DummyParams] = DummyParams

    async def __call__(self, params: DummyParams) -> ToolReturnValue:
        return ToolOk(output="clicked")


def _make_toolset() -> KimiToolset:
    ts = KimiToolset()
    ts.add(DummyToolA())
    ts.add(DummyToolB())
    return ts


def _make_extended_toolset() -> KimiToolset:
    ts = KimiToolset()
    ts.add(DummyToolA())
    ts.add(DummyToolB())
    ts.add(DummyToolC())
    return ts


def _tool_names(ts: KimiToolset) -> set[str]:
    return {t.name for t in ts.tools}


# --- hide() ---


def test_hide_removes_from_tools_property():
    ts = _make_toolset()
    assert _tool_names(ts) == {"ToolA", "ToolB"}

    ts.hide("ToolA")
    assert _tool_names(ts) == {"ToolB"}


def test_hide_returns_true_for_existing_tool():
    ts = _make_toolset()
    assert ts.hide("ToolA") is True


def test_hide_returns_false_for_nonexistent_tool():
    ts = _make_toolset()
    assert ts.hide("NoSuchTool") is False


def test_hide_is_idempotent():
    ts = _make_toolset()
    ts.hide("ToolA")
    ts.hide("ToolA")
    assert "ToolA" not in _tool_names(ts)

    # Single unhide restores after multiple hides
    ts.unhide("ToolA")
    assert "ToolA" in _tool_names(ts)


def test_hide_multiple_tools():
    ts = _make_toolset()
    ts.hide("ToolA")
    ts.hide("ToolB")
    assert ts.tools == []


# --- unhide() ---


def test_unhide_restores_tool():
    ts = _make_toolset()
    ts.hide("ToolA")
    assert "ToolA" not in _tool_names(ts)

    ts.unhide("ToolA")
    assert "ToolA" in _tool_names(ts)


def test_unhide_nonexistent_is_noop():
    ts = _make_toolset()
    ts.unhide("NoSuchTool")
    assert _tool_names(ts) == {"ToolA", "ToolB"}


def test_unhide_without_prior_hide_is_noop():
    ts = _make_toolset()
    ts.unhide("ToolA")
    assert _tool_names(ts) == {"ToolA", "ToolB"}


# --- find() is unaffected ---


def test_hidden_tool_still_findable_by_name():
    ts = _make_toolset()
    ts.hide("ToolA")
    assert ts.find("ToolA") is not None


def test_hidden_tool_still_findable_by_type():
    ts = _make_toolset()
    ts.hide("ToolA")
    assert ts.find(DummyToolA) is not None


# --- handle() is unaffected ---


async def test_hidden_tool_still_handled():
    """handle() should dispatch to hidden tools instead of returning ToolNotFoundError."""
    ts = _make_toolset()
    ts.hide("ToolA")

    tool_call = ToolCall(
        id="tc-1",
        function=ToolCall.FunctionBody(
            name="ToolA",
            arguments=json.dumps({"value": "test"}),
        ),
    )
    result = ts.handle(tool_call)
    # For async tools, handle() returns an asyncio.Future.
    # A ToolNotFoundError would be returned as a sync ToolResult directly.
    if isinstance(result, ToolResult):
        assert not isinstance(result.return_value, KosongToolNotFoundError)
    else:
        assert isinstance(result, asyncio.Future)
        result.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await result


async def test_nonexistent_tool_returns_not_found():
    """handle() should return ToolNotFoundError for tools not in _tool_dict at all."""
    ts = _make_toolset()

    tool_call = ToolCall(
        id="tc-2",
        function=ToolCall.FunctionBody(
            name="NoSuchTool",
            arguments="{}",
        ),
    )
    result = ts.handle(tool_call)
    assert isinstance(result, ToolResult)
    assert isinstance(result.return_value, KosongToolNotFoundError)
    assert "not found" in result.return_value.message


async def test_nonexistent_tool_with_fuzzy_suggestion():
    """A non-existent tool name close to a registered one should include a hint."""
    ts = _make_toolset()
    tool_call = ToolCall(
        id="tc-fuzzy",
        function=ToolCall.FunctionBody(
            name="TollX",  # similarity ~0.6 to ToolA/ToolB — below auto-correct but above suggestion cutoff
            arguments="{}",
        ),
    )
    result = ts.handle(tool_call)
    assert isinstance(result, ToolResult)
    assert isinstance(result.return_value, KosongToolNotFoundError)
    assert "did you mean" in result.return_value.message
    assert "ToolA" in result.return_value.message or "ToolB" in result.return_value.message


async def test_nonexistent_tool_auto_corrects_case_insensitive():
    """A case-insensitive match should auto-correct to the real tool."""
    ts = _make_toolset()
    tool_call = ToolCall(
        id="tc-ci",
        function=ToolCall.FunctionBody(
            name="toola",  # lowercase version of "ToolA"
            arguments="{}",
        ),
    )
    result = ts.handle(tool_call)
    assert isinstance(result, asyncio.Task)
    tr = await result
    output = tr.return_value.output
    assert isinstance(output, str)
    assert output.startswith("a")
    assert "<system-warning>" in output


async def test_nonexistent_tool_auto_correct_appends_warning():
    """When auto-correcting, a system-warning should be appended to the output."""
    ts = _make_toolset()
    tool_call = ToolCall(
        id="tc-warn",
        function=ToolCall.FunctionBody(
            name="toola",  # case-insensitive match to "ToolA"
            arguments="{}",
        ),
    )
    result = ts.handle(tool_call)
    assert isinstance(result, asyncio.Task)
    tr = await result
    output = tr.return_value.output
    assert isinstance(output, str)
    assert "<system-warning>" in output
    assert "toola" in output.lower() or "ToolA" in output
    assert "Auto-corrected" in output


async def test_nonexistent_tool_auto_corrects_close_typo():
    """A tool name with a small typo (high similarity) should auto-correct."""
    ts = _make_extended_toolset()
    # "LongNamedTol" missing the last 'o' vs "LongNamedTool" (ratio ~0.96)
    tool_call = ToolCall(
        id="tc-typo",
        function=ToolCall.FunctionBody(
            name="LongNamedTol",
            arguments="{}",
        ),
    )
    result = ts.handle(tool_call)
    assert isinstance(result, asyncio.Task)
    tr = await result
    output = tr.return_value.output
    assert isinstance(output, str)
    assert output.startswith("long")
    assert "<system-warning>" in output


async def test_nonexistent_tool_auto_correct_does_not_affect_distant_names():
    """A distant name still returns ToolNotFoundError."""
    ts = _make_extended_toolset()
    tool_call = ToolCall(
        id="tc-distant",
        function=ToolCall.FunctionBody(
            name="Xyzzy",  # no similarity to any registered tool, below 0.75 cutoff
            arguments="{}",
        ),
    )
    result = ts.handle(tool_call)
    assert isinstance(result, ToolResult)
    assert isinstance(result.return_value, KosongToolNotFoundError)


# --- snake_case / kebab-case / SCREAMING_SNAKE_CASE auto-correction ---


async def _assert_misformatted_autocorrects_to_long(sent: str) -> None:
    """A mis-formatted spelling of ``LongNamedTool`` auto-corrects and runs it."""
    ts = _make_extended_toolset()
    tool_call = ToolCall(
        id=f"tc-{sent}",
        function=ToolCall.FunctionBody(
            name=sent,
            arguments="{}",
        ),
    )
    result = ts.handle(tool_call)
    assert isinstance(result, asyncio.Task)
    tr = await result
    output = tr.return_value.output
    assert isinstance(output, str)
    assert output.startswith("long")
    assert "<system-warning>" in output
    assert "Auto-corrected" in output


async def test_nonexistent_tool_auto_corrects_snake_case():
    """snake_case spelling of a multi-word tool auto-corrects to CamelCase."""
    await _assert_misformatted_autocorrects_to_long("long_named_tool")


async def test_nonexistent_tool_auto_corrects_kebab_case():
    """kebab-case spelling of a multi-word tool auto-corrects to CamelCase."""
    await _assert_misformatted_autocorrects_to_long("long-named-tool")


async def test_nonexistent_tool_auto_corrects_screaming_snake_case():
    """SCREAMING_SNAKE_CASE spelling of a multi-word tool auto-corrects."""
    await _assert_misformatted_autocorrects_to_long("LONG_NAMED_TOOL")


async def test_nonexistent_tool_auto_corrects_mixed_separators():
    """Mixed CamelCase + separator spelling of a multi-word tool auto-corrects."""
    await _assert_misformatted_autocorrects_to_long("Long_Named_Tool")


async def test_nonexistent_tool_auto_corrects_snake_case_with_typo():
    """A snake_case spelling that also contains a typo still auto-corrects."""
    await _assert_misformatted_autocorrects_to_long("long_named_tol")


async def test_snake_case_non_tool_returns_not_found():
    """A snake_case name not close to any tool still yields ToolNotFoundError."""
    ts = _make_extended_toolset()
    tool_call = ToolCall(
        id="tc-unrelated",
        function=ToolCall.FunctionBody(
            name="totally_unrelated",
            arguments="{}",
        ),
    )
    result = ts.handle(tool_call)
    assert isinstance(result, ToolResult)
    assert isinstance(result.return_value, KosongToolNotFoundError)


# --- AskAgent name-variant auto-correction (fuzzy matching) ---


class DummyAskAgent(CallableTool2[DummyParams]):
    name: str = "AskAgent"
    description: str = "Send a message to another agent."
    params: type[DummyParams] = DummyParams

    async def __call__(self, params: DummyParams) -> ToolReturnValue:
        return ToolOk(output="asked")


def _make_ask_agent_toolset() -> KimiToolset:
    ts = KimiToolset()
    ts.add(DummyAskAgent())
    return ts


async def _assert_ask_agent_variant_autocorrects(sent: str) -> None:
    """A slightly-wrong spelling of ``AskAgent`` auto-corrects and runs it."""
    ts = _make_ask_agent_toolset()
    tool_call = ToolCall(
        id=f"tc-ask-{sent}",
        function=ToolCall.FunctionBody(
            name=sent,
            arguments="{}",
        ),
    )
    result = ts.handle(tool_call)
    assert isinstance(result, asyncio.Task)
    tr = await result
    output = tr.return_value.output
    assert isinstance(output, str)
    assert output.startswith("asked")
    assert "<system-warning>" in output
    assert "AskAgent" in output


async def test_ask_agent_kebab_case_autocorrects():
    """``ask-agent`` auto-corrects to ``AskAgent``."""
    await _assert_ask_agent_variant_autocorrects("ask-agent")


async def test_ask_agent_snake_case_autocorrects():
    """``ask_agent`` auto-corrects to ``AskAgent``."""
    await _assert_ask_agent_variant_autocorrects("ask_agent")


async def test_ask_agent_space_separated_autocorrects():
    """``ask agent`` (space-separated) auto-corrects to ``AskAgent``."""
    await _assert_ask_agent_variant_autocorrects("ask agent")


# --- hide/unhide cycle ---


def test_hide_unhide_cycle():
    """Multiple hide/unhide cycles should work correctly."""
    ts = _make_toolset()

    ts.hide("ToolA")
    assert "ToolA" not in _tool_names(ts)

    ts.unhide("ToolA")
    assert "ToolA" in _tool_names(ts)

    ts.hide("ToolA")
    assert "ToolA" not in _tool_names(ts)

    ts.unhide("ToolA")
    assert "ToolA" in _tool_names(ts)


# --- deduplication ---


async def test_same_step_dedup():
    """Duplicate tool calls within the same step should share the original result."""
    ts = _make_toolset()
    ts.begin_step([])

    args = json.dumps({"value": "x"})
    tool_call_1 = ToolCall(
        id="tc-dedup-1",
        function=ToolCall.FunctionBody(
            name="ToolA",
            arguments=args,
        ),
    )
    tool_call_2 = ToolCall(
        id="tc-dedup-2",
        function=ToolCall.FunctionBody(
            name="ToolA",
            arguments=args,
        ),
    )

    result_1 = ts.handle(tool_call_1)
    assert isinstance(result_1, asyncio.Task)

    result_2 = ts.handle(tool_call_2)
    assert isinstance(result_2, asyncio.Task)

    # Both should eventually return the same output but with different tool_call_id
    tr_1 = await result_1
    tr_2 = await result_2

    assert tr_1.return_value.output == "a"
    assert tr_2.return_value.output == "a"
    assert tr_1.tool_call_id == "tc-dedup-1"
    assert tr_2.tool_call_id == "tc-dedup-2"

    assert ts.end_step() == [("ToolA", '{"value":"x"}'), ("ToolA", '{"value":"x"}')]


async def test_same_step_dedup_canonicalizes_argument_key_order():
    """Equivalent JSON objects with different key order should share the original result."""
    ts = _make_toolset()
    ts.begin_step([])

    tool_call_1 = ToolCall(
        id="tc-canonical-1",
        function=ToolCall.FunctionBody(
            name="ToolA",
            arguments='{"a": 1, "b": 2}',
        ),
    )
    tool_call_2 = ToolCall(
        id="tc-canonical-2",
        function=ToolCall.FunctionBody(
            name="ToolA",
            arguments='{"b": 2, "a": 1}',
        ),
    )

    result_1 = ts.handle(tool_call_1)
    result_2 = ts.handle(tool_call_2)
    assert isinstance(result_1, asyncio.Task)
    assert isinstance(result_2, asyncio.Task)
    tr_1 = await result_1
    tr_2 = await result_2
    # `a`/`b` are not ToolA params, so both results carry the unsupported-args
    # warning; the same-step duplicate copies the already-decorated original.
    for tr in (tr_1, tr_2):
        output = tr.return_value.output
        assert isinstance(output, str)
        assert output.startswith("a")
        assert "<system-warning>" in output
    assert tr_1.tool_call_id == "tc-canonical-1"
    assert tr_2.tool_call_id == "tc-canonical-2"
    assert ts.end_step() == [("ToolA", '{"a":1,"b":2}'), ("ToolA", '{"a":1,"b":2}')]


# ══════════════════════════════════════════════════════════════════════════════
# Repeat / cycle detection
#
# Three independent ladders, all keyed on the exact
# ``(tool name, canonical arguments)`` pair wherever a repeat is implied:
#   1. adjacent streak — remind 5 / 10 / 15, stop 20;
#   2. different args  — remind 35 / 45 / 55, stop 70 (per tool);
#   3. per-key cycle   — remind 3, stop 6 (same call reappearing inside a
#      cycle of at most 5 calls; a 6+ call rotation keeps the counter at zero).
# A non-empty thinking block between two tool calls clears every counter (see
# the reasoning-reset section further down).
# ══════════════════════════════════════════════════════════════════════════════


def _call_args(value: str) -> str:
    return json.dumps({"value": value})


def _canonical_key(tool_name: str, value: str) -> tuple[str, str]:
    """The key ``handle()`` records: (tool name, canonical arguments)."""
    return tool_name, json.dumps({"value": value}, separators=(",", ":"))


def _make_call(tool_name: str, arguments: str, call_id: str = "c") -> ToolCall:
    return ToolCall(
        id=call_id,
        function=ToolCall.FunctionBody(name=tool_name, arguments=arguments),
    )


async def _one_step(
    ts: KimiToolset,
    previous: list[tuple[str, str]],
    tool_name: str,
    arguments: str,
    *,
    step: int = 1,
    turn_id: str = "T1",
) -> tuple[str, list[tuple[str, str]]]:
    """Run one step with one tool call; return (output text, next previous_calls)."""
    ts.begin_step(previous, step_no=step, turn_id=turn_id)
    result = ts.handle(_make_call(tool_name, arguments, f"c{step}"))
    assert isinstance(result, asyncio.Task)
    tr = await result
    return str(tr.return_value.output), ts.end_step()


async def _run_cycle(
    ts: KimiToolset,
    seq: list[tuple[str, str]],
    *,
    max_steps: int = 90,
    turn_id: str = "T1",
) -> tuple[int | None, dict[int, str]]:
    """Replay *seq* one call per step (as KimiSoul drives the toolset).

    Returns (stop_step, outputs) where ``stop_step`` is the 1-based step that
    tripped a force-stop (``None`` when the sequence never stopped) and
    ``outputs`` maps step number to that step's tool output text.
    """
    previous: list[tuple[str, str]] = []
    outputs: dict[int, str] = {}
    for step in range(1, max_steps + 1):
        tool_name, arguments = seq[(step - 1) % len(seq)]
        output, previous = await _one_step(
            ts, previous, tool_name, arguments, step=step, turn_id=turn_id
        )
        outputs[step] = output
        if ts.force_stop_turn:
            return step, outputs
    return None, outputs


def test_repeat_detection_thresholds():
    """The three ladders and their thresholds are the configured ones."""
    assert (_STREAK_REMINDER_AT, _STREAK_REMINDER_2_AT, _STREAK_REMINDER_3_AT) == (5, 10, 15)
    assert _STREAK_FORCE_STOP == 20
    assert (_DIFF_ARGS_REMINDER_AT, _DIFF_ARGS_REMINDER_2_AT, _DIFF_ARGS_REMINDER_3_AT) == (
        35,
        45,
        55,
    )
    assert _DIFF_ARGS_FORCE_STOP == 70
    assert _CYCLE_REMINDER_AT == 3
    assert _CYCLE_FORCE_STOP == 6
    assert _MAX_CYCLE_LENGTH == 5

    # reminders must fire strictly before the matching force stop
    assert _STREAK_REMINDER_3_AT < _STREAK_FORCE_STOP
    assert _DIFF_ARGS_REMINDER_3_AT < _DIFF_ARGS_FORCE_STOP
    assert _CYCLE_REMINDER_AT < _CYCLE_FORCE_STOP
    # the per-key window covers a full 5-call cycle but never a 6-call one
    assert _MAX_CYCLE_LENGTH == 5


def test_adjacent_streak_helper():
    """The streak helper counts identical trailing calls, capped at the stop."""
    a = ("tool", "a")
    b = ("tool", "b")

    assert _adjacent_streak([]) == 0
    assert _adjacent_streak([a]) == 1
    assert _adjacent_streak([a, b]) == 1
    assert _adjacent_streak([a, a, b]) == 1
    assert _adjacent_streak([b, a, a, a]) == 3
    assert _adjacent_streak([a] * 500) == _STREAK_FORCE_STOP + 1


def test_cycle_occurrences_helper():
    """Per-key occurrences only count inside a cycle of at most 5 calls."""
    a, b, c, d, e, f = (("tool", value) for value in "abcdef")

    assert _cycle_occurrences([a, b, a], a) == 2
    assert _cycle_occurrences([a, b, a, b], b) == 2
    assert _cycle_occurrences([a, b, c, d, e, a], a) == 2
    # a 6-call rotation is not a cycle: every counter stays at one
    assert _cycle_occurrences([a, b, c, d, e, f, a], a) == 1
    # the count is capped just past the force-stop threshold
    assert _cycle_occurrences([a, b] * 20, a) == _CYCLE_FORCE_STOP + 1


async def test_third_identical_call_is_not_detected():
    """Repeating one exact call a few times is normal work, not a loop."""
    ts = _make_toolset()
    args = _call_args("x")
    previous: list[tuple[str, str]] = []
    outputs: list[str] = []
    for step in range(1, 4):
        output, previous = await _one_step(ts, previous, "ToolA", args, step=step)
        outputs.append(output)

    assert ts.force_stop_turn is False
    assert not any("system-reminder" in text for text in outputs)
    assert ts._turn_call_history == [_canonical_key("ToolA", "x")] * 3


async def test_adjacent_streak_reminds_at_5_10_15_and_stops_at_20():
    """A(x) repeated back to back: graded reminders, then a force-stop."""
    ts = _make_toolset()
    stop_step, outputs = await _run_cycle(ts, [("ToolA", _call_args("x"))], max_steps=25)

    assert stop_step == _STREAK_FORCE_STOP
    # silence before the first threshold
    for step in range(1, _STREAK_REMINDER_AT):
        assert "system-reminder" not in outputs[step]
    # the first reminder is generic
    assert "Stop repeating the same tool call with identical parameters" in outputs[5]
    # the second names the tool, the count and the canonical arguments
    assert "Repeated identical call" in outputs[10]
    assert "- tool: ToolA" in outputs[10]
    assert "repeated_times: 10" in outputs[10]
    assert '- arguments: {"value":"x"}' in outputs[10]
    # the third is the last warning before the stop
    assert "Dead-end loop" in outputs[15]
    assert "The turn is stopped at 20 repetitions" in outputs[15]
    # the stopping call carries the stop text
    assert "Dead-end loop detected" in outputs[20]
    # between the thresholds the ladder is silent
    for step in (6, 7, 11, 16, 19):
        assert "system-reminder" not in outputs[step]
    assert ts.force_stop_reason == "adjacent-repeat"
    assert ts.force_stop_key == _canonical_key("ToolA", "x")


async def test_same_tool_with_different_args_is_not_a_repeat_below_35():
    """Rule 2: distinct arguments are distinct work below the diff-args ladder."""
    ts = _make_toolset()
    ts.begin_step([], step_no=1, turn_id="turn-diff-args")

    outputs: list[str] = []
    for i in range(_DIFF_ARGS_REMINDER_AT - 1):
        result = ts.handle(_make_call("ToolA", _call_args(str(i)), f"d{i}"))
        assert isinstance(result, asyncio.Task)
        tr = await result
        outputs.append(str(tr.return_value.output))

    assert ts.force_stop_turn is False
    assert not any("system-reminder" in text for text in outputs)
    assert len(ts._turn_call_history) == _DIFF_ARGS_REMINDER_AT - 1
    assert len(set(ts._turn_call_history)) == _DIFF_ARGS_REMINDER_AT - 1


async def test_different_args_reminds_at_35_45_55_and_stops_at_70():
    """One tool called with 70 distinct arguments force-stops the turn."""
    ts = _make_toolset()
    stop_step, outputs = await _run_cycle(
        ts,
        [("ToolA", _call_args(str(i))) for i in range(_DIFF_ARGS_FORCE_STOP)],
        max_steps=_DIFF_ARGS_FORCE_STOP,
    )

    assert stop_step == _DIFF_ARGS_FORCE_STOP
    assert "system-reminder" not in outputs[34]
    assert "Same tool called repeatedly with different args" in outputs[35]
    assert "called 45 times with different args" in outputs[45]
    assert "called 55 times. Stop now." in outputs[55]
    assert "Stop all tool calls and summarize what is left" in outputs[70]
    for step in (36, 40, 46, 56, 69):
        assert "system-reminder" not in outputs[step]
    assert ts.force_stop_reason == "different-args-overuse"


async def test_per_key_cycle_reminds_at_3_and_stops_at_6():
    """A(x) -> B(y) -> A(x) -> ... is a per-key cycle, not an adjacent run."""
    ts = _make_toolset()
    seq = [("ToolA", _call_args("x")), ("ToolB", _call_args("y"))]
    stop_step, outputs = await _run_cycle(ts, seq, max_steps=20)

    # ToolA runs on calls 1/3/5/7/9/11: its 3rd repetition is the reminder,
    # its 6th repetition force-stops the turn.
    assert stop_step == 11
    assert "system-reminder" not in outputs[4]
    assert "'ToolA' has already run 3 times this turn" in outputs[5]
    assert "the turn is stopped at 6 repetitions" in outputs[5]
    # ToolB reaches its own 3rd occurrence on the next call, then silence until
    # ToolA's 6th occurrence force-stops the turn
    assert "'ToolB' has already run 3 times this turn" in outputs[6]
    for step in range(7, 11):
        assert "system-reminder" not in outputs[step]
    assert "Dead-end loop detected" in outputs[11]
    assert ts.force_stop_reason == "cycle-repeat"
    assert ts.force_stop_key == _canonical_key("ToolA", "x")


async def test_six_call_rotation_never_counts():
    """Rule 3: 6+ calls in the rotation keep every per-key counter at zero."""
    ts = _make_extended_toolset()
    seq = [
        ("ToolA", _call_args("a")),
        ("ToolB", _call_args("b")),
        ("LongNamedTool", _call_args("c")),
        ("ToolA", _call_args("d")),
        ("ToolB", _call_args("e")),
        ("LongNamedTool", _call_args("f")),
    ]

    stop_step, outputs = await _run_cycle(ts, seq, max_steps=60)

    assert stop_step is None
    assert ts.force_stop_turn is False
    # 10 full rotations produced no loop traffic at all (each tool is called
    # 20 times with distinct arguments, i.e. below the 35-call ladder)
    assert not any("system-reminder" in text for text in outputs.values())


async def test_five_call_cycle_is_still_detected():
    """A 5-call cycle is the widest rotation the per-key ladder understands."""
    ts = _make_extended_toolset()
    seq = [
        ("ToolA", _call_args("a")),
        ("ToolB", _call_args("b")),
        ("LongNamedTool", _call_args("c")),
        ("ToolA", _call_args("d")),
        ("ToolB", _call_args("e")),
    ]

    stop_step, outputs = await _run_cycle(ts, seq, max_steps=30)

    assert stop_step == 26  # the first key's 6th occurrence
    assert "system-reminder" not in outputs[10]
    assert "has already run 3 times this turn" in outputs[11]
    assert ts.force_stop_reason == "cycle-repeat"


async def test_cycle_ladder_skips_adjacent_repeats():
    """A back-to-back run is owned by the streak ladder, not the cycle one."""
    ts = _make_toolset()
    _, outputs = await _run_cycle(ts, [("ToolA", _call_args("x"))], max_steps=16)

    assert not any("has already run" in text for text in outputs.values())
    # the streak ladder still speaks at its own thresholds
    assert "Stop repeating the same tool call" in outputs[_STREAK_REMINDER_AT]
    assert "Repeated identical call" in outputs[_STREAK_REMINDER_2_AT]


async def test_action_tools_are_not_punished_by_the_cycle_ladder():
    """Identical ``browser_use`` calls are real clicks, not a repeated step."""
    ts = KimiToolset()
    ts.add(DummyBrowserUseTool())
    ts.add(DummyToolB())
    click = _call_args("same-selector")
    previous: list[tuple[str, str]] = []
    outputs: list[str] = []
    step = 0
    for i in range(5):
        for tool_name, arguments in (("browser_use", click), ("ToolB", _call_args(f"b{i}"))):
            step += 1
            output, previous = await _one_step(
                ts, previous, tool_name, arguments, step=step, turn_id="turn-action"
            )
            outputs.append(output)

    assert ts.force_stop_turn is False
    assert ts._turn_call_history.count(_canonical_key("browser_use", "same-selector")) == 5
    assert not any("has already run" in text for text in outputs)


def test_action_tool_detection_matches_the_tool_and_its_dotted_spelling():
    assert _is_action_tool("browser_use")
    assert _is_action_tool("computer_use")
    assert _is_action_tool("kimi_cli.tools.browser_use:browser_use")
    assert not _is_action_tool("ToolA")
    assert not _is_action_tool("read")


async def test_same_step_duplicate_is_not_counted():
    """A duplicate inside one step copies the result and stays unpunished."""
    ts = _make_toolset()
    args = _call_args("x")
    ts.begin_step([], turn_id="turn-same-step")

    first = ts.handle(_make_call("ToolA", args, "a1"))
    assert isinstance(first, asyncio.Task)
    tr1 = await first
    assert tr1.return_value.output == "a"

    second = ts.handle(_make_call("ToolA", args, "a2"))
    assert isinstance(second, asyncio.Task)
    tr2 = await second
    assert "system-reminder" not in str(tr2.return_value.output)
    assert ts._turn_call_history == [_canonical_key("ToolA", "x")]
    assert ts._tool_call_counts["ToolA"] == 1
    assert ts.force_stop_turn is False


async def test_loop_counts_reset_on_new_turn():
    """A repeated call across two turns is legitimate work, not a loop."""
    ts = _make_toolset()
    args = _call_args("x")
    previous: list[tuple[str, str]] = []
    for step in range(1, 4):
        _, previous = await _one_step(ts, previous, "ToolA", args, step=step)
    assert len(ts._turn_call_history) == 3

    # turn 2 starts with an empty previous-calls window
    output, _ = await _one_step(ts, [], "ToolA", args, step=1, turn_id="turn-2")

    assert output == "a"
    assert ts._turn_call_history == [_canonical_key("ToolA", "x")]
    assert ts._tool_call_counts == {"ToolA": 1}
    assert ts.force_stop_turn is False


async def test_loop_counts_reset_on_retried_step():
    """Re-running a step (empty previous calls) clears the call history."""
    ts = _make_toolset()
    args = _call_args("x")
    await _one_step(ts, [], "ToolA", args, step=1, turn_id="t")
    assert ts._turn_call_history == [_canonical_key("ToolA", "x")]

    ts.begin_step([], step_no=1, turn_id="t")
    assert ts._turn_call_history == []
    assert ts._tool_call_counts == {}

    output, _ = await _one_step(ts, [], "ToolA", args, step=1, turn_id="t")
    assert output == "a"
    assert ts._turn_call_history == [_canonical_key("ToolA", "x")]


async def test_turn_total_calls_soft_reminder():
    """A very long turn receives a soft reminder instead of being force-stopped."""
    ts = _make_extended_toolset()
    ts.begin_step([], turn_id="turn-total")

    names = ("ToolA", "ToolB", "LongNamedTool")
    last_output = ""
    for i in range(_TURN_TOTAL_REMINDER_START):
        # rotate over three tools so no per-tool ladder (70 calls) can fire,
        # and use distinct arguments so no repeat is ever implied
        result = ts.handle(_make_call(names[i % len(names)], _call_args(str(i)), f"t{i}"))
        assert isinstance(result, asyncio.Task)
        tr = await result
        last_output = str(tr.return_value.output)

    assert ts.force_stop_turn is False
    assert ts._turn_total_calls == _TURN_TOTAL_REMINDER_START
    assert max(ts._tool_call_counts.values()) < _DIFF_ARGS_FORCE_STOP
    assert f"This turn has made {_TURN_TOTAL_REMINDER_START} tool calls" in last_output


async def test_cross_step_duplicate_is_tracked_without_punishment():
    """Repeating an earlier call sets the dedup flag but appends no reminder."""
    ts = _make_toolset()
    args = _call_args("x")
    ts.begin_step([("ToolA", args)], step_no=2, turn_id="T1")

    result = ts.handle(_make_call("ToolA", args, "dup"))
    assert isinstance(result, asyncio.Task)
    tr = await result

    assert str(tr.return_value.output) == "a"
    assert ts.dedup_triggered is True
    assert ts.force_stop_turn is False


async def test_non_duplicate_allowed():
    """A tool call with different arguments is not a duplicate."""
    ts = _make_toolset()
    ts.begin_step([("ToolA", _call_args("x"))])

    result = ts.handle(_make_call("ToolA", _call_args("y")))
    assert isinstance(result, asyncio.Task)
    tr = await result
    assert tr.return_value.output == "a"
    assert ts.dedup_triggered is False
    assert ts.end_step() == [("ToolA", '{"value":"y"}')]


def test_begin_end_step():
    """begin_step and end_step manage the per-step deduplication window."""
    ts = _make_toolset()

    ts.begin_step([("ToolA", "{}")])
    assert ts._previous_step_calls == [("ToolA", "{}")]
    assert ts._current_step_calls == []
    assert ts._current_step_tasks == {}
    assert ts.dedup_triggered is False

    ts._current_step_calls.append(("ToolB", "{}"))
    assert ts.end_step() == [("ToolB", "{}")]
    assert ts.dedup_triggered is False


async def test_begin_step_resets_cancelled_tasks():
    """begin_step() must clear _current_step_tasks so a retry does not await a cancelled task."""
    ts = _make_toolset()

    ts.begin_step([], step_no=1, turn_id="t1")
    args = _call_args("x")
    result1 = ts.handle(_make_call("ToolA", args, "c1"))
    assert isinstance(result1, asyncio.Task)
    result1.cancel()

    # Simulate retry: begin_step again for the same step
    ts.begin_step([], step_no=1, turn_id="t1")
    result2 = ts.handle(_make_call("ToolA", args, "c2"))
    assert isinstance(result2, asyncio.Task)
    assert result2 is not result1

    # The new task should complete successfully (not raise CancelledError)
    tr = await result2
    assert tr.return_value.output == "a"


async def test_cross_step_dedup_not_triggered_after_back_to_the_future():
    """An emptied previous-calls window (back_to_the_future) is a fresh start."""
    ts = _make_toolset()
    args = _call_args("x")

    ts.begin_step([], step_no=1, turn_id="t1")
    first = ts.handle(_make_call("ToolA", args, "c1"))
    assert isinstance(first, asyncio.Task)
    await first
    assert ts.end_step() == [("ToolA", '{"value":"x"}')]

    # Simulate back_to_the_future: caller clears last_calls
    ts.begin_step([], step_no=2, turn_id="t1")
    second = ts.handle(_make_call("ToolA", args, "c2"))
    assert isinstance(second, asyncio.Task)
    tr = await second

    assert tr.return_value.output == "a"
    assert ts.dedup_triggered is False
    assert ts._turn_call_history == [_canonical_key("ToolA", "x")]


# --- Dynamic tool output byte budget ---


class _MockLLM:
    def __init__(self, max_context_size: int) -> None:
        self.max_context_size = max_context_size


class _MockRuntime:
    def __init__(self, max_context_size: int, session: object | None = None) -> None:
        self.llm = _MockLLM(max_context_size)
        self.read_only = False
        # a session is always present (the toolset reads `session.work_dir` for
        # hook events); the default one has no `dir`, so output dumps are skipped
        self.session = session if session is not None else _SessionWithoutDir()


def _mock_runtime(max_context_size: int, session: object | None = None) -> Any:
    """Build a `_MockRuntime`, typed loosely so it fits `KimiToolset(runtime=...)`."""
    return _MockRuntime(max_context_size, session=session)


class _EchoTool(CallableTool2[DummyParams]):
    name: str = "EchoTool"
    description: str = "Echoes the input value"
    params: type[DummyParams] = DummyParams

    async def __call__(self, params: DummyParams) -> ToolReturnValue:
        return ToolOk(output=params.value)


class _BashEchoTool(_EchoTool):
    name: str = "bash"

    async def __call__(self, params: DummyParams) -> ToolReturnValue:
        return ToolOk(output=params.value, message=params.value)


class _BashPartEchoTool(_BashEchoTool):
    async def __call__(self, params: DummyParams) -> ToolReturnValue:
        return ToolOk(output=[TextPart(text=params.value)], message=params.value)


class _BashRuntimeErrorTool(_EchoTool):
    name: str = "bash"

    async def __call__(self, params: DummyParams) -> ToolReturnValue:
        raise RuntimeError(params.value)


def test_max_output_bytes_fallback_without_runtime():
    """Without a runtime the byte budget falls back to the original 128 KiB."""
    ts = KimiToolset()
    assert ts._get_max_output_bytes() == 128 << 10


def test_max_output_bytes_with_total_context_budget():
    """Empty context: the total-context term dominates for typical models."""
    ts = KimiToolset(runtime=_MockRuntime(32_768))
    # total_budget = int(32768 * 4 * 0.5) = 65536
    # remaining_budget = int(32768 * 4 * 0.9) = 117964
    # default cap = 131072
    assert ts._get_max_output_bytes() == 65_536


def test_max_output_bytes_with_partial_context():
    """Partially filled context shrinks the budget via the remaining-context term."""
    ts = KimiToolset(
        runtime=_MockRuntime(1_048_576),
        context_token_provider=lambda: 1_020_000,
    )
    # reserve = safety margin = max(4096, 2% of 1048576) = 20972
    # remaining_budget = (1048576 - 1020000 - 20972) * 4 = 30416
    assert ts._get_max_output_bytes() == 30_416


def test_max_output_bytes_near_full_context():
    """Near-full context drives the budget toward zero."""
    ts = KimiToolset(
        runtime=_MockRuntime(131_072),
        context_token_provider=lambda: 130_000,
    )
    # remaining after the 4096-token margin is negative: nothing may be emitted
    assert ts._get_max_output_bytes() == 0


def test_max_output_bytes_absolute_ceiling():
    """Very large contexts are capped by the default 128 KiB ceiling."""
    ts = KimiToolset(runtime=_MockRuntime(1_048_576))
    assert ts._get_max_output_bytes() == 128 << 10


def test_set_context_token_provider_overrides_provider():
    """The setter can replace the callback used by _get_max_output_bytes."""
    ts = KimiToolset(
        runtime=_MockRuntime(1_048_576),
        context_token_provider=lambda: 1_020_000,
    )
    assert ts._get_max_output_bytes() == 30_416

    ts.set_context_token_provider(lambda: 1_046_528)
    # remaining after the margin is negative: no room for tool output at all
    assert ts._get_max_output_bytes() == 0


def test_estimate_tool_output_token_budget_matches_byte_budget():
    """The token-budget helper mirrors the existing byte-budget logic."""
    ts = KimiToolset(runtime=_MockRuntime(131_072))

    # Empty context: capped by the default 128 KiB ceiling.
    # total_budget_bytes = int(131072 * 4 * 0.5) = 262144 -> capped at 131072 -> tokens = 32768
    assert ts.estimate_tool_output_token_budget(131_072, 0) == 32_768

    # Partial context: still capped by the default 128 KiB ceiling.
    # remaining_budget_bytes = int((131072 - 65536) * 4 * 0.9) = 235929 -> capped -> tokens = 32768
    assert ts.estimate_tool_output_token_budget(131_072, 65_536) == 32_768

    # Near-full context: nothing fits after the safety margin, so the tool must
    # not produce output at all (the old 0.9 * remaining formula still emitted
    # 964 tokens here, which pushed the next request past the window).
    assert ts.estimate_tool_output_token_budget(131_072, 130_000) == 0

    # Very large context: default ceiling applies.
    assert ts.estimate_tool_output_token_budget(1_048_576, 0) == 32_768


async def test_oversized_string_output_is_truncated():
    """A string tool output above the dynamic limit is truncated and returned as an error."""
    ts = KimiToolset()  # fallback 128 KiB budget
    ts.add(_EchoTool())

    # Use a non-repeating pattern so sanitize_for_tokenizer does not collapse it.
    large_output = "".join(chr(65 + i % 26) for i in range(200_000))
    tool_call = ToolCall(
        id="tc-large",
        function=ToolCall.FunctionBody(
            name="EchoTool",
            arguments=json.dumps({"value": large_output}),
        ),
    )
    result = ts.handle(tool_call)
    assert isinstance(result, asyncio.Task)
    tr = await result
    assert isinstance(tr.return_value, ToolError)
    output = tr.return_value.output
    assert isinstance(output, str)
    assert len(output.encode("utf-8")) == ts._get_max_output_bytes()
    assert "exceeded the maximum allowed size" in tr.return_value.message
    assert "truncated" in tr.return_value.message.lower()
    # The truncated output is a prefix of the original content.
    assert large_output.startswith(output)


async def test_small_string_output_is_returned_normally():
    """A string tool output below the dynamic limit is returned unchanged."""
    ts = KimiToolset()
    ts.add(_EchoTool())

    small_output = "hello"
    tool_call = ToolCall(
        id="tc-small",
        function=ToolCall.FunctionBody(
            name="EchoTool",
            arguments=json.dumps({"value": small_output}),
        ),
    )
    result = ts.handle(tool_call)
    assert isinstance(result, asyncio.Task)
    tr = await result
    assert tr.return_value.output == small_output


async def test_process_output_and_message_receive_unicode_prefilter():
    ts = KimiToolset()
    ts.add(_BashEchoTool())
    hostile = "before😀👨\u200d👩\u200d👧\ue0b0\u0378\ud800\ufdd0after"
    preserved = "before😀👨\u200d👩\u200d👧\ue0b0\u0378after"

    tr = await _call_echo(ts, "bash", hostile, "tc-bash-prefilter")

    assert tr.return_value.output == preserved
    assert tr.return_value.message == preserved
    tr.return_value.output.encode("utf-8")
    tr.return_value.message.encode("utf-8")


async def test_process_content_parts_receive_unicode_prefilter():
    ts = KimiToolset()
    ts.add(_BashPartEchoTool())

    tr = await _call_echo(ts, "bash", "before😀\u0378after", "tc-bash-part-prefilter")

    assert isinstance(tr.return_value.output, list)
    assert len(tr.return_value.output) == 1
    part = tr.return_value.output[0]
    assert isinstance(part, TextPart)
    assert part.text == "before😀\u0378after"


async def test_non_process_output_does_not_receive_process_prefilter():
    ts = KimiToolset()
    ts.add(_EchoTool())
    output = "before😀\u0378after"

    tr = await _call_echo(ts, "EchoTool", output, "tc-echo-no-prefilter")

    assert tr.return_value.output == output


async def test_process_runtime_error_message_receives_unicode_prefilter():
    ts = KimiToolset()
    ts.add(_BashRuntimeErrorTool())

    tr = await _call_echo(ts, "bash", "failure😀\u0378", "tc-bash-error-prefilter")

    assert tr.return_value.is_error
    assert tr.return_value.message == "Error running tool: failure😀\u0378"


class _PartEchoTool(CallableTool2[DummyParams]):
    name: str = "PartEchoTool"
    description: str = "Echoes the input value as a TextPart list"
    params: type[DummyParams] = DummyParams

    async def __call__(self, params: DummyParams) -> ToolReturnValue:
        return ToolOk(output=[TextPart(text=params.value)])


async def test_oversized_content_part_output_is_truncated():
    """A ContentPart tool output above the dynamic limit is truncated and returned as an error."""
    ts = KimiToolset()  # fallback 128 KiB budget
    ts.add(_PartEchoTool())

    large_output = "".join(chr(65 + i % 26) for i in range(200_000))
    tool_call = ToolCall(
        id="tc-large-parts",
        function=ToolCall.FunctionBody(
            name="PartEchoTool",
            arguments=json.dumps({"value": large_output}),
        ),
    )
    result = ts.handle(tool_call)
    assert isinstance(result, asyncio.Task)
    tr = await result
    assert isinstance(tr.return_value, ToolError)
    output = tr.return_value.output
    assert isinstance(output, list)
    assert len(output) == 1
    assert isinstance(output[0], TextPart)
    assert len(output[0].text.encode("utf-8")) == ts._get_max_output_bytes()
    assert "exceeded the maximum allowed size" in tr.return_value.message
    assert large_output.startswith(output[0].text)


# ══════════════════════════════════════════════════════════════════════════════
# Oversized tool output: dump to the session directory instead of truncating
# ══════════════════════════════════════════════════════════════════════════════


class _MockSession:
    """Minimal stand-in for ``Session``, exposing ``dir`` and ``work_dir``."""

    def __init__(self, dir_path: Path) -> None:
        self._dir = dir_path
        self.work_dir = str(dir_path.parent)

    @property
    def dir(self) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        return self._dir


class _SessionWithoutDir:
    """A session-like object with no ``dir``: oversized output cannot be dumped."""

    work_dir = "."


def _non_compressible(size: int) -> str:
    """A non-repeating A-Z pattern that survives sanitize / micro-compression."""
    return "".join(chr(65 + i % 26) for i in range(size))


async def _call_echo(ts: KimiToolset, tool_name: str, value: str, call_id: str) -> ToolResult:
    tool_call = ToolCall(
        id=call_id,
        function=ToolCall.FunctionBody(
            name=tool_name,
            arguments=json.dumps({"value": value}),
        ),
    )
    result = ts.handle(tool_call)
    assert isinstance(result, asyncio.Task)
    return await result


async def test_oversized_string_output_is_dumped_to_session_dir(tmp_path):
    """With a session, oversized output is saved to a file, not returned truncated."""
    session_dir = tmp_path / "session-str"
    ts = KimiToolset(runtime=_mock_runtime(4096, session=_MockSession(session_dir)))
    ts.add(_EchoTool())
    max_bytes = ts._get_max_output_bytes()

    large_output = _non_compressible(200_000)
    tr = await _call_echo(ts, "EchoTool", large_output, "tc-dump-str")

    assert isinstance(tr.return_value, ToolError)
    # nothing is returned inline any more, only the pointer message
    assert tr.return_value.output == ""
    assert tr.return_value.brief == "Output too large"
    message = tr.return_value.message
    assert "exceeded the maximum allowed size" in message
    assert "the full output was saved to" in message
    assert "The result has been truncated" not in message

    dumps = list((session_dir / _OUTPUT_DUMP_SUBDIR).glob("*.txt"))
    assert len(dumps) == 1
    assert dumps[0].name in message
    # the dump is not cut at the inline budget: it keeps more than the budget
    dumped = dumps[0].read_text(encoding="utf-8")
    assert len(dumped.encode("utf-8")) > max_bytes
    assert len(dumped.encode("utf-8")) <= len(large_output.encode("utf-8"))


async def test_oversized_content_part_output_is_dumped_to_session_dir(tmp_path):
    """ContentPart outputs are flattened into the dump file, with no inline output."""
    session_dir = tmp_path / "session-parts"
    ts = KimiToolset(runtime=_mock_runtime(4096, session=_MockSession(session_dir)))
    ts.add(_PartEchoTool())
    max_bytes = ts._get_max_output_bytes()

    large_output = _non_compressible(200_000)
    tr = await _call_echo(ts, "PartEchoTool", large_output, "tc-dump-parts")

    assert isinstance(tr.return_value, ToolError)
    assert tr.return_value.output == ""
    assert "was saved to" in tr.return_value.message

    dumps = list((session_dir / _OUTPUT_DUMP_SUBDIR).glob("*.txt"))
    assert len(dumps) == 1
    assert len(dumps[0].read_text(encoding="utf-8").encode("utf-8")) > max_bytes


async def test_oversized_output_falls_back_to_truncation_without_session():
    """A runtime without a usable session dir keeps the old truncating behavior."""
    ts = KimiToolset(runtime=_mock_runtime(4096))  # session has no `dir`
    ts.add(_EchoTool())

    large_output = _non_compressible(200_000)
    tr = await _call_echo(ts, "EchoTool", large_output, "tc-no-session")

    assert isinstance(tr.return_value, ToolError)
    assert "The result has been truncated." in tr.return_value.message
    inline = tr.return_value.output
    assert isinstance(inline, str)
    assert len(inline.encode("utf-8")) == ts._get_max_output_bytes()


def test_output_to_dump_text_flattens_parts():
    """Dump text keeps text/think content and media URLs."""
    parts: list[ContentPart] = [
        TextPart(text="hello"),
        ThinkPart(think="why"),
        ImageURLPart(image_url=ImageURLPart.ImageURL(url="data:image/png;base64,AAA")),
    ]
    text = _output_to_dump_text(parts)
    assert "hello" in text
    assert "[think #1]\nwhy" in text
    assert "[image #2] data:image/png;base64,AAA" in text
    assert _output_to_dump_text("plain") == "plain"
    assert _output_to_dump_text(TextPart(text="single")) == "single"


def test_write_output_dump_is_lossless_and_unique(tmp_path):
    """A plain string dump is written verbatim; names never collide."""
    dump_dir = tmp_path / _OUTPUT_DUMP_SUBDIR
    dump_dir.mkdir()
    first = _write_output_dump(dump_dir, "line one", "Some Tool")
    second = _write_output_dump(dump_dir, "line two", "Some Tool")
    assert first is not None and second is not None
    assert first != second
    assert first.read_text(encoding="utf-8") == "line one"
    assert second.read_text(encoding="utf-8") == "line two"
    # the tool name is slugged into the file name
    assert "Some_Tool" in first.name


def test_write_output_dump_prunes_old_files(tmp_path):
    """Only the newest ``_OUTPUT_DUMP_MAX_FILES`` dumps are kept."""
    dump_dir = tmp_path / _OUTPUT_DUMP_SUBDIR
    dump_dir.mkdir()
    total = _OUTPUT_DUMP_MAX_FILES + 5
    last = None
    for i in range(total):
        last = _write_output_dump(dump_dir, f"payload-{i}", "EchoTool")
        assert last is not None
    files = list(dump_dir.glob("*.txt"))
    assert len(files) == _OUTPUT_DUMP_MAX_FILES
    assert last is not None
    assert last.read_text(encoding="utf-8") == f"payload-{total - 1}"


def test_write_output_dump_cuts_huge_content(tmp_path, monkeypatch):
    """Content above the dump ceiling is cut with an explicit marker."""
    import kimi_cli.soul.toolset as toolset_module

    monkeypatch.setattr(toolset_module, "_OUTPUT_DUMP_MAX_BYTES", 1000)
    dump_dir = tmp_path / _OUTPUT_DUMP_SUBDIR
    dump_dir.mkdir()
    path = _write_output_dump(dump_dir, "x" * 5000, "EchoTool")
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "[dump cut at 1000 bytes" in text
    assert text.startswith("x" * 1000)


async def test_dump_failure_falls_back_to_truncation(tmp_path, monkeypatch):
    """When the dump write fails, the inline truncation path is still used."""
    import kimi_cli.soul.toolset as toolset_module

    monkeypatch.setattr(toolset_module, "_write_output_dump", lambda *args, **kwargs: None)
    ts = KimiToolset(runtime=_mock_runtime(4096, session=_MockSession(tmp_path / "session-fail")))
    ts.add(_EchoTool())

    tr = await _call_echo(ts, "EchoTool", _non_compressible(200_000), "tc-dump-fail")
    assert isinstance(tr.return_value, ToolError)
    assert "The result has been truncated." in tr.return_value.message
    inline = tr.return_value.output
    assert isinstance(inline, str)
    assert len(inline.encode("utf-8")) == ts._get_max_output_bytes()


async def test_dump_message_mentions_cut_when_output_exceeds_ceiling(tmp_path, monkeypatch):
    """The pointer message says only a prefix was saved when the ceiling is hit."""
    import kimi_cli.soul.toolset as toolset_module

    monkeypatch.setattr(toolset_module, "_OUTPUT_DUMP_MAX_BYTES", 1000)
    ts = KimiToolset(runtime=_mock_runtime(4096, session=_MockSession(tmp_path / "session-cut")))
    ts.add(_EchoTool())

    tr = await _call_echo(ts, "EchoTool", _non_compressible(200_000), "tc-dump-cut")
    assert isinstance(tr.return_value, ToolError)
    assert "only the first 1000 bytes were saved to" in tr.return_value.message


# ══════════════════════════════════════════════════════════════════════════════
# Tool name redirect / candidate collection tests
# ══════════════════════════════════════════════════════════════════════════════


def test_platform_redirects_win32_includes_powershell():
    """On Windows, the platform redirect map includes bash→pwsh."""
    redirects = _build_platform_redirects()
    # The normalized key for "bash" should map to "pwsh"
    from kosong.tooling import normalize_tool_name

    bash_norm = normalize_tool_name("Bash")
    pwsh_norm = normalize_tool_name("Powershell")
    # On win32, bash → pwsh; on POSIX, Powershell → bash
    if sys.platform == "win32":
        assert redirects.get(bash_norm) == "pwsh"
    else:
        assert redirects.get(pwsh_norm) == "bash"


def test_platform_redirects_has_keys():
    """Platform redirects map should always have entries."""
    redirects = _build_platform_redirects()
    assert len(redirects) > 0
    # All values should be valid normalized tool name variants
    for norm_key, target in redirects.items():
        assert isinstance(norm_key, str)
        assert isinstance(target, str)
        assert norm_key.islower()


def test_platform_redirects_norm_is_cache():
    """_PLATFORM_REDIRECTS_NORM is the pre-computed platform redirect map."""
    assert isinstance(_PLATFORM_REDIRECTS_NORM, dict)
    # Should match a fresh build
    fresh = _build_platform_redirects()
    assert fresh == _PLATFORM_REDIRECTS_NORM


def test_collect_candidates_no_redirects():
    """Without redirects, _collect_candidates falls through to normalize+fuzzy."""
    valid = {"ToolA", "ToolB", "LongNamedTool"}
    candidates = _collect_candidates("ToolA", valid)
    assert "ToolA" in candidates
    # The exact match via normalize_tool_name should be first
    assert candidates[0] == "ToolA"


def test_collect_candidates_with_redirect():
    """With redirects, the redirect match takes priority."""
    from kosong.tooling import normalize_tool_name

    valid = {"ToolA", "ToolB"}
    redirects = {normalize_tool_name("Bash"): "ToolA"}
    # "Bash" is not in valid, but redirects point to "ToolA"
    candidates = _collect_candidates("Bash", valid, redirects=redirects)
    assert "ToolA" in candidates
    # ToolA should be first (from redirect, highest priority)
    assert candidates[0] == "ToolA"


def test_collect_candidates_redirect_not_in_valid():
    """Redirect to a name not in valid_names is skipped."""
    from kosong.tooling import normalize_tool_name

    valid = {"ToolA", "ToolB"}
    redirects = {normalize_tool_name("Bash"): "NonExistent"}
    candidates = _collect_candidates("Bash", valid, redirects=redirects)
    # Should not contain "NonExistent" since it's not in valid
    assert "NonExistent" not in candidates


def test_collect_candidates_deduplicates():
    """Candidates list should not contain duplicates."""
    valid = {"ToolA", "ToolB"}
    candidates = _collect_candidates("toola", valid)  # case-insensitive match to ToolA
    # Should have exactly one "ToolA"
    toola_count = candidates.count("ToolA")
    assert toola_count == 1, f"Expected exactly one 'ToolA', got {toola_count}"


# ══════════════════════════════════════════════════════════════════════════════
# Handle with redirect map (integration)
# ══════════════════════════════════════════════════════════════════════════════


async def test_handle_redirect_auto_corrects_hallucinated_name():
    """A hallucinated tool name that matches the redirect map should auto-correct."""
    from kosong.tooling import normalize_tool_name

    ts = _make_toolset()
    # Add a redirect entry for testing: "AppendFile" → "ToolA"
    # We inject into the module-level platform redirects (it's used by handle())
    import kimi_cli.soul.toolset as ts_mod

    original_redirects = dict(ts_mod._PLATFORM_REDIRECTS_NORM)
    ts_mod._PLATFORM_REDIRECTS_NORM = dict(original_redirects)
    ts_mod._PLATFORM_REDIRECTS_NORM[normalize_tool_name("AppendFile")] = "ToolA"

    try:
        tool_call = ToolCall(
            id="tc-redirect",
            function=ToolCall.FunctionBody(
                name="AppendFile",
                arguments=json.dumps({"value": "test"}),
            ),
        )
        result = ts.handle(tool_call)
        assert isinstance(result, asyncio.Task)
        tr = await result
        output = tr.return_value.output
        assert isinstance(output, str)
        assert output.startswith("a")  # ToolA returns "a"
        assert "<system-warning>" in output
        assert "Auto-corrected" in output
    finally:
        ts_mod._PLATFORM_REDIRECTS_NORM = original_redirects


# ══════════════════════════════════════════════════════════════════════════════
# Integration test: TodoList invalid status fuzzy matching
# ══════════════════════════════════════════════════════════════════════════════


async def test_todo_tool_invalid_status_fuzzy_match():
    """A TodoList call with status="completed" should be fuzzy-matched to
    status="done" after value-level repair in kosong/tooling.

    Currently this is expected to return a ToolValidateError (broken behavior).
    After Phase 2's value-level fuzzy matching, this should return ToolOk.
    """
    from typing import Literal

    class TodoItem(BaseModel):
        title: str
        status: Literal["pending", "in_progress", "done"]

    class TodoParams(BaseModel):
        todos: list[TodoItem] | TodoItem | None = None
        mode: Literal["overwrite", "append", "force_overwrite"] = "append"

    class SimpleTodoTool(CallableTool2[TodoParams]):
        name: str = "TodoList"
        description: str = "Simple todo list tool"
        params: type[TodoParams] = TodoParams

        @override
        async def __call__(self, params: TodoParams) -> ToolReturnValue:
            return ToolOk(output="ok")

    ts = KimiToolset()
    ts.add(SimpleTodoTool())

    tool_call = ToolCall(
        id="tc-todo-status",
        function=ToolCall.FunctionBody(
            name="TodoList",
            arguments=json.dumps({
                "items": [{"title": "Test", "status": "completed"}],
                "mode": "append",
            }),
        ),
    )

    result = ts.handle(tool_call)
    assert isinstance(result, asyncio.Task)
    tr = await result
    ret = tr.return_value

    # Currently the tool succeeds (ToolOk), which is the desired behavior.
    # After Phase 2 adds fuzzy value matching, "completed" should be
    # explicitly repaired to "done" by the kosong/tooling layer.
    assert isinstance(ret, ToolOk), (
        f"Expected ToolOk for 'completed' status, got {ret}"
    )


async def test_handle_redirect_precedes_fuzzy():
    """Redirect map is checked before fuzzy matching."""
    from kosong.tooling import normalize_tool_name

    ts = _make_extended_toolset()
    import kimi_cli.soul.toolset as ts_mod

    original_redirects = dict(ts_mod._PLATFORM_REDIRECTS_NORM)
    ts_mod._PLATFORM_REDIRECTS_NORM = dict(original_redirects)
    # "LongNmedTool" has a typo - fuzzy would correct to "LongNamedTool"
    # But we redirect it to "ToolB" instead
    ts_mod._PLATFORM_REDIRECTS_NORM[normalize_tool_name("LongNmedTool")] = "ToolB"

    try:
        tool_call = ToolCall(
            id="tc-redirect-priority",
            function=ToolCall.FunctionBody(
                name="LongNmedTool",
                arguments=json.dumps({"value": "test"}),
            ),
        )
        result = ts.handle(tool_call)
        assert isinstance(result, asyncio.Task)
        tr = await result
        output = tr.return_value.output
        assert isinstance(output, str)
        assert output.startswith("b")  # ToolB returns "b", not "long"
        assert "<system-warning>" in output
    finally:
        ts_mod._PLATFORM_REDIRECTS_NORM = original_redirects


# ══════════════════════════════════════════════════════════════════════════════
# Argument-format hallucination repair tests
# ══════════════════════════════════════════════════════════════════════════════


def test_unwrap_nested_arguments_extracts_arguments_key():
    """{"arguments": {...}} should unwrap to the inner dict."""
    assert _unwrap_nested_arguments({"arguments": {"value": "x"}}) == {"value": "x"}


def test_unwrap_nested_arguments_extracts_args_key():
    """{"args": {...}} should unwrap to the inner dict."""
    assert _unwrap_nested_arguments({"args": {"value": "x"}}) == {"value": "x"}


def test_unwrap_nested_arguments_extracts_stringified_inner():
    """{"arguments": "{...}"} should unwrap to the stringified JSON object."""
    assert _unwrap_nested_arguments({"arguments": '{"value": "x"}'}) == '{"value": "x"}'


def test_unwrap_nested_arguments_extracts_list_inner():
    """{"arguments": [...]} should unwrap to the inner list."""
    assert _unwrap_nested_arguments({"arguments": [1, 2]}) == [1, 2]


def test_unwrap_nested_arguments_leaves_plain_dict_alone():
    """A plain argument dict should not be modified."""
    assert _unwrap_nested_arguments({"value": "x"}) == {"value": "x"}


def test_unwrap_nested_arguments_leaves_multi_key_dict_alone():
    """A dict with multiple keys should not be unwrapped even if one is 'arguments'."""
    assert _unwrap_nested_arguments({"arguments": {"value": "x"}, "extra": 1}) == {
        "arguments": {"value": "x"},
        "extra": 1,
    }


def test_unwrap_nested_arguments_passes_through_non_dict():
    """Non-dict values are returned unchanged."""
    assert _unwrap_nested_arguments("string") == "string"
    assert _unwrap_nested_arguments([1, 2]) == [1, 2]
    assert _unwrap_nested_arguments(None) is None


def test_parse_stringified_arguments_parses_json_object():
    """A string containing a JSON object is parsed into a dict."""
    assert _parse_stringified_arguments('{"value": "x"}') == {"value": "x"}


def test_parse_stringified_arguments_parses_json_array():
    """A string containing a JSON array is parsed into a list."""
    assert _parse_stringified_arguments('["a", "b"]') == ["a", "b"]


def test_parse_stringified_arguments_parses_with_relaxed_json():
    """Relaxed JSON parsing allows single quotes and trailing commas."""
    assert _parse_stringified_arguments("{'value': 'x',}") == {"value": "x"}


def test_parse_stringified_arguments_leaves_plain_string_alone():
    """A non-JSON string is returned unchanged."""
    assert _parse_stringified_arguments("plain text") == "plain text"


def test_parse_stringified_arguments_leaves_dict_alone():
    """A dict input is returned unchanged."""
    assert _parse_stringified_arguments({"value": "x"}) == {"value": "x"}



def test_repair_argument_format_unwraps_then_parses():
    """A stringified object wrapped in {'arguments': ...} is fully repaired."""
    assert _repair_argument_format({'arguments': '{"value": "x"}'}) == {"value": "x"}


def test_repair_argument_format_parses_then_unwraps():
    """A stringified {'arguments': ...} object is parsed and then unwrapped."""
    assert _repair_argument_format('{"arguments": {"value": "x"}}') == {"value": "x"}


def test_repair_argument_format_noop_for_plain_dict():
    """A plain dict is not changed by repair."""
    assert _repair_argument_format({"value": "x"}) == {"value": "x"}


# --- _repair_todo_arguments: fuzzy todo argument repair -----------------
# One tool owns the argument shape now; the retired names still route here so a
# session recorded before the merge repairs the same way.

def test_repair_todo_wraps_bare_string_items():
    """Bare-string items become `{title}` items; no status is injected."""
    assert _repair_todo_arguments("todo_list", {"todos": ["Buy milk", "Walk dog"]}) == {
        "todos": [{"title": "Buy milk"}, {"title": "Walk dog"}]
    }
    assert _repair_todo_arguments("todo_list", {"todos": "Single"}) == {
        "todos": [{"title": "Single"}]
    }


def test_repair_todo_promotes_batch_synonyms():
    """edits/changes/operations/updates all land on `todos`."""
    assert _repair_todo_arguments("todo_list", {"edits": [{"title": "A"}, {"content": "B"}]}) == {
        "todos": [{"title": "A"}, {"content": "B"}]
    }
    assert _repair_todo_arguments("todo_list", {"operations": [{"title": "C"}]}) == {
        "todos": [{"title": "C"}]
    }
    assert _repair_todo_arguments("todo_list", {"changes": "Just one"}) == {
        "todos": [{"title": "Just one"}]
    }


def test_repair_todo_passes_wellformed_arguments_through():
    """A call that already matches the schema is never rewritten."""
    args = {"todos": [{"title": "K", "parent": "P"}], "mode": "merge"}
    assert _repair_todo_arguments("todo_list", dict(args)) == args


def test_repair_todo_accepts_retired_tool_names():
    """A session recorded before the merge repaired the same way."""
    for retired in RETIRED_TODO_TOOL_NAMES:
        assert _repair_todo_arguments(retired, {"todos": ["X"]}) == {
            "todos": [{"title": "X"}]
        }
    # any other tool is untouched
    assert _repair_todo_arguments("bash", {"command": "ls"}) == {"command": "ls"}


def test_repair_todo_arguments_keeps_valid_calls_unchanged():
    """Well-formed todo calls and non-todo tools are not modified."""
    valid_write = {"todos": [{"content": "A", "status": "done"}], "mode": "append"}
    assert (
        _repair_todo_arguments(RETIRED_TODO_TOOL_NAMES[0], dict(valid_write))
        == valid_write
    )

    valid_update = {"title": "A", "status": "in_progress", "force": True}
    assert (
        _repair_todo_arguments(RETIRED_TODO_TOOL_NAMES[1], dict(valid_update))
        == valid_update
    )

    other = {"value": "x", "task": "ignored"}
    assert _repair_todo_arguments("bash", dict(other)) == other


async def test_handle_repairs_nested_arguments():
    """handle() repairs arguments double-wrapped in {'arguments': ...}."""
    ts = KimiToolset()
    ts.add(_EchoTool())

    tool_call = ToolCall(
        id="tc-nested-args",
        function=ToolCall.FunctionBody(
            name="EchoTool",
            arguments=json.dumps({"arguments": {"value": "nested"}}),
        ),
    )
    result = ts.handle(tool_call)
    assert isinstance(result, asyncio.Task)
    tr = await result
    assert tr.return_value.output == "nested"


async def test_handle_repairs_stringified_arguments():
    """handle() repairs arguments that are a stringified JSON object."""
    ts = KimiToolset()
    ts.add(_EchoTool())

    # arguments field is a JSON-encoded string containing the real args object
    tool_call = ToolCall(
        id="tc-stringified-args",
        function=ToolCall.FunctionBody(
            name="EchoTool",
            arguments=json.dumps('{"value": "stringified"}'),
        ),
    )
    result = ts.handle(tool_call)
    assert isinstance(result, asyncio.Task)
    tr = await result
    assert tr.return_value.output == "stringified"


async def test_handle_repairs_stringified_double_wrapped_arguments():
    """handle() repairs arguments that are a stringified {'arguments': ...} wrapper."""
    ts = KimiToolset()
    ts.add(_EchoTool())

    # arguments field is a JSON-encoded string containing {"arguments": {"value": ...}}
    tool_call = ToolCall(
        id="tc-stringified-nested",
        function=ToolCall.FunctionBody(
            name="EchoTool",
            arguments=json.dumps('{"arguments": {"value": "deeply_nested"}}'),
        ),
    )
    result = ts.handle(tool_call)
    assert isinstance(result, asyncio.Task)
    tr = await result
    assert tr.return_value.output == "deeply_nested"


async def test_handle_repairs_relaxedly_stringified_arguments():
    """handle() repairs single-quoted/trailing-comma stringified arguments."""
    ts = KimiToolset()
    ts.add(_EchoTool())

    # arguments field is a JSON-encoded string containing relaxed JSON
    tool_call = ToolCall(
        id="tc-relaxed-stringified",
        function=ToolCall.FunctionBody(
            name="EchoTool",
            arguments=json.dumps("{'value': 'relaxed',}"),
        ),
    )
    result = ts.handle(tool_call)
    assert isinstance(result, asyncio.Task)
    tr = await result
    assert tr.return_value.output == "relaxed"


# --- Reasoning-gated loop detection reset --------------------------------
# Rule 1: a non-empty ThinkPart (thinking block) between two tool calls is
# progress.  KimiSoul calls ``KimiToolset.reset_loop_detectors()`` for such a
# step, which clears the ordered call history and every counter, so no repeat is
# ever detected across the thinking block.


async def _inflate_repeat_history(
    ts: KimiToolset, count: int = 3
) -> list[tuple[str, str]]:
    """Record *count* identical calls (below every nudge threshold)."""
    args = _call_args("x")
    previous: list[tuple[str, str]] = []
    for step in range(1, count + 1):
        output, previous = await _one_step(ts, previous, "ToolA", args, step=step)
        assert "system-reminder" not in output
    return previous


async def test_reset_loop_detectors_clears_every_counter():
    """The public reset clears the call history, every counter and the trip."""
    ts = _make_toolset()
    await _inflate_repeat_history(ts)
    assert ts._turn_call_history  # the counter is inflated

    ts.reset_loop_detectors()

    assert ts._turn_call_history == []
    assert ts._tool_call_counts == {}
    assert ts._seen_call_keys == set()
    assert ts._turn_total_calls == 0
    assert ts.force_stop_turn is False
    assert ts.force_stop_reason is None
    assert ts.force_stop_key is None


async def test_reset_loop_detectors_allows_legitimate_rerun_after_reasoning():
    """After a reasoning reset the same call is fresh work, not a repeat."""
    ts = _make_toolset()
    previous = await _inflate_repeat_history(ts)
    ts.reset_loop_detectors()

    # the per-step window is unchanged (non-empty), so only the reasoning reset
    # can have cleared the call history
    output, _ = await _one_step(ts, previous, "ToolA", _call_args("x"), step=4)

    assert output == "a"
    assert ts.force_stop_turn is False
    assert ts._turn_call_history == [_canonical_key("ToolA", "x")]
    assert ts._tool_call_counts == {"ToolA": 1}


async def test_thinking_between_calls_never_detects_a_loop(monkeypatch):
    """Rule 1: every reasoned step clears the counters, so nothing is detected."""
    monkeypatch.setattr(toolset_module, "_STREAK_REMINDER_AT", 2)
    monkeypatch.setattr(toolset_module, "_STREAK_REMINDER_2_AT", 3)
    monkeypatch.setattr(toolset_module, "_STREAK_REMINDER_3_AT", 4)
    monkeypatch.setattr(toolset_module, "_STREAK_FORCE_STOP", 5)
    monkeypatch.setattr(toolset_module, "_CYCLE_REMINDER_AT", 2)
    monkeypatch.setattr(toolset_module, "_CYCLE_FORCE_STOP", 3)

    ts = _make_toolset()
    args = _call_args("x")
    previous: list[tuple[str, str]] = []
    for step in range(1, 13):
        output, previous = await _one_step(ts, previous, "ToolA", args, step=step)
        assert "system-reminder" not in output
        assert ts.force_stop_turn is False
        ts.reset_loop_detectors()  # a non-empty ThinkPart between two tool calls

    # Without the reasoning resets this sequence would have nudged at call 2 and
    # force-stopped at call 5 (see the streak ladder tests above).
    assert ts._turn_call_history == []
    assert ts._tool_call_counts == {}


async def test_reset_loop_detectors_clears_force_stop_trip(monkeypatch):
    """A reasoned step clears a mid-stream force-stop trip so it cannot end the turn."""
    monkeypatch.setattr(toolset_module, "_CYCLE_FORCE_STOP", 3)

    ts = _make_toolset()
    seq = [("ToolA", _call_args("x")), ("ToolB", _call_args("y"))]
    stop_step, _ = await _run_cycle(ts, seq)
    assert stop_step == 5  # ToolA's 3rd occurrence
    assert ts.force_stop_turn is True
    assert ts.force_stop_reason == "cycle-repeat"
    assert ts.force_stop_key is not None

    ts.reset_loop_detectors()
    assert ts.force_stop_turn is False
    assert ts.force_stop_reason is None
    assert ts.force_stop_key is None
    assert ts._turn_total_calls == 0


async def test_reasoned_step_delays_the_force_stop(monkeypatch):
    """A reasoning block between occurrences restarts the streak count."""
    monkeypatch.setattr(toolset_module, "_STREAK_FORCE_STOP", 3)

    ts = _make_toolset()
    args = _call_args("x")
    previous: list[tuple[str, str]] = []

    # A(x) twice, then a reasoning step clears the counter before it grows
    for step in (1, 2):
        _, previous = await _one_step(ts, previous, "ToolA", args, step=step)
    assert _adjacent_streak(ts._turn_call_history) == 2
    assert ts.force_stop_turn is False
    ts.reset_loop_detectors()

    # 3 more calls after the reset: the 3rd is the stop
    stop_step = None
    for step in (3, 4, 5):
        _, previous = await _one_step(ts, previous, "ToolA", args, step=step)
        if ts.force_stop_turn:
            stop_step = step
            break

    assert stop_step == 5
    assert ts.force_stop_reason == "adjacent-repeat"


def test_has_reasoning_parts_helper():
    """The toolset reasoning-part helper only accepts non-empty ThinkParts."""
    assert _has_reasoning_parts([ThinkPart(think="let me think")]) is True
    assert _has_reasoning_parts([ThinkPart(think="")]) is False
    assert _has_reasoning_parts([TextPart(text="answer")]) is False
    assert _has_reasoning_parts([TextPart(text="x"), ThinkPart(think="why")]) is True
    assert _has_reasoning_parts([]) is False


def test_has_reasoning_parts_accepts_generic_content_parts():
    """The helper is typed on ContentPart and accepts any part subclass at runtime."""
    parts: list[ContentPart] = [ThinkPart(think="reason")]
    assert _has_reasoning_parts(parts) is True


# ══════════════════════════════════════════════════════════════════════════════
# Argument-coercion error handling inside KimiToolset.handle()
# (kimi_cli/soul/toolset.py, the exception handler around line 1669)
#
# Regression tests for two defects in that branch:
#   A. `ToolValidateError` was referenced but never imported, so the coercion
#      branch raised `NameError` *inside* the tool task instead of returning a
#      ToolValidateError.  The `NameError` escaped `handle()`'s task and was
#      re-raised by `StepResult.tool_results()` (only CancelledError is
#      converted there), which aborted the whole agent turn.
#   B. A `TypeError`/`ValueError` whose message does *not* look like an argument
#      problem hit a bare `raise`.  Python does not re-dispatch an exception
#      raised inside a handler to the sibling `except Exception` clause, so such
#      a tool error also escaped as a failed task instead of becoming a
#      `ToolRuntimeError` (the contract `kosong.tooling.Toolset` documents:
#      report failures as values, never as exceptions).
# ══════════════════════════════════════════════════════════════════════════════


class _CoercionParams(BaseModel):
    value: str = ""


class _DictCoercionTool(CallableTool2[_CoercionParams]):
    """Raises the real-world ValueError you get from a bare ``dict(...)`` call.

    This is the exact failure the coercion branch was written to catch: the LLM
    passes a plain string where the tool expects key/value pairs, so the tool's
    ``dict(params.value)`` fails with "dictionary update sequence element #0 has
    length 1; 2 is required".
    """

    name: str = "DictCoercion"
    description: str = "Tool that coerces its argument with dict()"
    params: type[_CoercionParams] = _CoercionParams

    @override
    async def __call__(self, params: _CoercionParams) -> ToolReturnValue:
        return ToolOk(output=str(dict(params.value)))


class _ArityCoercionTool(CallableTool2[_CoercionParams]):
    """Raises a TypeError that mentions ``argument`` (wrong-arity failure)."""

    name: str = "ArityCoercion"
    description: str = "Tool that fails with an argument-count TypeError"
    params: type[_CoercionParams] = _CoercionParams

    @override
    async def __call__(self, params: _CoercionParams) -> ToolReturnValue:
        raise TypeError("_helper() takes 1 positional argument but 2 were given")


class _UnrelatedValueErrorTool(CallableTool2[_CoercionParams]):
    """Raises a ValueError that has nothing to do with tool arguments."""

    name: str = "UnrelatedValueError"
    description: str = "Tool that fails with an unrelated ValueError"
    params: type[_CoercionParams] = _CoercionParams

    @override
    async def __call__(self, params: _CoercionParams) -> ToolReturnValue:
        raise ValueError("invalid literal for int() with base 10: 'x'")


def _make_coercion_toolset(tool: CallableTool2[_CoercionParams]) -> KimiToolset:
    ts = KimiToolset()
    ts.add(tool)  # type: ignore[arg-type]
    return ts


def _coercion_call(tool_name: str, value: str) -> ToolCall:
    return ToolCall(
        id=f"tc-{tool_name}",
        function=ToolCall.FunctionBody(name=tool_name, arguments=_call_args(value)),
    )


async def test_handle_dict_coercion_error_returns_tool_validate_error():
    """``dict("ab")`` inside a tool must come back as a ToolValidateError.

    Before the fix this raised ``NameError: name 'ToolValidateError' is not
    defined`` from the task returned by ``handle()``.
    """
    from kosong.tooling.error import ToolValidateError

    ts = _make_coercion_toolset(_DictCoercionTool())
    result = ts.handle(_coercion_call("DictCoercion", "ab"))
    assert isinstance(result, asyncio.Task)

    tr = await result  # must not raise NameError
    assert tr.tool_call_id == "tc-DictCoercion"
    assert isinstance(tr.return_value, ToolValidateError), (
        f"expected ToolValidateError, got {type(tr.return_value).__name__}: "
        f"{tr.return_value.message}"
    )
    assert "Error validating JSON arguments" in tr.return_value.message
    assert "Invalid arguments for tool `DictCoercion`" in tr.return_value.message
    # The real cause stays visible to the model so it can reformat its call.
    assert "dictionary update sequence" in tr.return_value.message
    assert tr.return_value.is_error


async def test_handle_argument_type_error_returns_tool_validate_error():
    """A TypeError whose message mentions `argument` is also an argument error."""
    from kosong.tooling.error import ToolValidateError

    ts = _make_coercion_toolset(_ArityCoercionTool())
    result = ts.handle(_coercion_call("ArityCoercion", "x"))
    assert isinstance(result, asyncio.Task)

    tr = await result
    assert isinstance(tr.return_value, ToolValidateError), (
        f"expected ToolValidateError, got {type(tr.return_value).__name__}: "
        f"{tr.return_value.message}"
    )
    assert "positional argument" in tr.return_value.message


async def test_handle_unrelated_value_error_returns_tool_runtime_error():
    """A non-argument ValueError must degrade to ToolRuntimeError, not escape.

    The whole agent turn must survive a tool that raises an ordinary ValueError:
    `handle()`'s task failing would re-raise from
    `StepResult.tool_results()` and interrupt the step.
    """
    from kosong.tooling.error import ToolRuntimeError

    ts = _make_coercion_toolset(_UnrelatedValueErrorTool())
    result = ts.handle(_coercion_call("UnrelatedValueError", "x"))
    assert isinstance(result, asyncio.Task)

    tr = await result
    assert isinstance(tr.return_value, ToolRuntimeError), (
        f"expected ToolRuntimeError, got {type(tr.return_value).__name__}: "
        f"{tr.return_value.message}"
    )
    assert "invalid literal for int()" in tr.return_value.message
    assert tr.return_value.is_error


async def test_handle_coercion_error_does_not_fail_the_task():
    """The tool task must settle with a value, never with an exception.

    Pins the Toolset contract that `StepResult.tool_results()` relies on:
    every tool call resolves to a `ToolResult`, so one broken call cannot abort
    the turn.
    """
    ts = KimiToolset()
    ts.add(_DictCoercionTool())  # type: ignore[arg-type]
    ts.add(_ArityCoercionTool())  # type: ignore[arg-type]
    ts.add(_UnrelatedValueErrorTool())  # type: ignore[arg-type]

    tasks = [
        ts.handle(_coercion_call("DictCoercion", "ab")),
        ts.handle(_coercion_call("ArityCoercion", "x")),
        ts.handle(_coercion_call("UnrelatedValueError", "x")),
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert [type(r).__name__ for r in results] == ["ToolResult"] * 3, (
        f"tool tasks must return ToolResult, got {results}"
    )
