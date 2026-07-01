"""Tests for KimiToolset hide/unhide and deduplication functionality."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

from kosong.tooling import CallableTool2, ToolOk, ToolReturnValue
from kosong.tooling.error import ToolNotFoundError as KosongToolNotFoundError
from pydantic import BaseModel

from kimi_cli.soul.toolset import KimiToolset
from kimi_cli.wire.types import ToolCall, ToolResult


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


def _make_toolset() -> KimiToolset:
    ts = KimiToolset()
    ts.add(DummyToolA())
    ts.add(DummyToolB())
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

    assert tr_1.return_value.output == "a"
    assert tr_2.return_value.output == "a"
    assert ts.end_step() == [("ToolA", '{"a":1,"b":2}'), ("ToolA", '{"a":1,"b":2}')]


async def test_cross_step_duplicate_does_not_append_reminder_below_three_consecutive():
    """The second consecutive identical call is tracked but not reminded yet."""
    ts = _make_toolset()
    args = json.dumps({"value": "x"})
    ts.begin_step([("ToolA", args)])

    tool_call = ToolCall(
        id="tc-dedup-reminder",
        function=ToolCall.FunctionBody(
            name="ToolA",
            arguments=args,
        ),
    )

    result = ts.handle(tool_call)
    assert isinstance(result, asyncio.Task)
    tr = await result
    output = tr.return_value.output
    assert isinstance(output, str)
    assert output == "a"
    assert ts.dedup_triggered is True
    assert ts.end_step() == [("ToolA", '{"value":"x"}')]


async def test_cross_step_duplicate_appends_reminder_at_three_consecutive():
    """The first reminder is sparse and appears only at the third consecutive call."""
    ts = _make_toolset()
    args = json.dumps({"value": "x"})
    previous_calls: list[tuple[str, str]] = []

    for i in range(2):
        ts.begin_step(previous_calls, step_no=i + 1)
        result = ts.handle(
            ToolCall(
                id=f"tc-repeat-prior-{i}",
                function=ToolCall.FunctionBody(name="ToolA", arguments=args),
            )
        )
        assert isinstance(result, asyncio.Task)
        tr = await result
        assert "system-reminder" not in tr.return_value.output
        previous_calls = ts.end_step()

    ts.begin_step(previous_calls, step_no=3)
    result = ts.handle(
        ToolCall(
            id="tc-repeat-third",
            function=ToolCall.FunctionBody(name="ToolA", arguments=args),
        )
    )
    assert isinstance(result, asyncio.Task)
    tr = await result
    output = tr.return_value.output
    assert isinstance(output, str)
    assert "You just repeated the same tool call with identical parameters" in output
    assert "repeated_times" not in output


async def test_cross_step_duplicate_uses_sparse_stronger_reminders():
    """The stronger reminder appears at the fifth repeat and includes canonical args."""
    ts = _make_toolset()
    args = '{"b": 2, "a": 1}'
    previous_calls: list[tuple[str, str]] = []
    last_output = ""

    for i in range(5):
        ts.begin_step(previous_calls, step_no=i + 1)
        result = ts.handle(
            ToolCall(
                id=f"tc-repeat-{i}",
                function=ToolCall.FunctionBody(name="ToolA", arguments=args),
            )
        )
        assert isinstance(result, asyncio.Task)
        tr = await result
        last_output = tr.return_value.output
        previous_calls = ts.end_step()

    assert isinstance(last_output, str)
    assert "You have called this tool with identical parameters" in last_output
    assert "5 times without progress" in last_output
    assert "- tool: ToolA" in last_output
    assert '- arguments: {"a":1,"b":2}' in last_output


async def test_non_duplicate_allowed():
    """A tool call with different arguments should be allowed even if the tool name matches."""
    ts = _make_toolset()
    ts.begin_step([("ToolA", json.dumps({"value": "x"}))])

    args = json.dumps({"value": "y"})
    tool_call = ToolCall(
        id="tc-ok-1",
        function=ToolCall.FunctionBody(
            name="ToolA",
            arguments=args,
        ),
    )

    result = ts.handle(tool_call)
    assert isinstance(result, asyncio.Task)
    tr = await result
    assert tr.return_value.output == "a"
    assert ts.dedup_triggered is False
    assert ts.end_step() == [("ToolA", '{"value":"y"}')]


def test_begin_end_step():
    """begin_step and end_step should correctly manage deduplication state."""
    ts = _make_toolset()

    ts.begin_step([("ToolA", "{}")])
    assert ts._previous_step_calls == [("ToolA", "{}")]
    assert ts._current_step_calls == []
    assert ts._current_step_tasks == {}
    assert ts.dedup_triggered is False

    ts._current_step_calls.append(("ToolB", "{}"))
    assert ts.end_step() == [("ToolB", "{}")]

    # After end_step, internal lists are not cleared by end_step itself;
    # the caller (KimiSoul) is expected to call begin_step again for the next step.
    # But dedup_triggered should still reflect the last step's state.
    assert ts.dedup_triggered is False


async def test_begin_step_resets_cancelled_tasks():
    """begin_step() must clear _current_step_tasks so a retry does not await a cancelled task."""
    ts = _make_toolset()

    ts.begin_step([], step_no=1, turn_id="t1")
    args = json.dumps({"value": "x"})
    tc1 = ToolCall(
        id="c1",
        function=ToolCall.FunctionBody(
            name="ToolA",
            arguments=args,
        ),
    )
    result1 = ts.handle(tc1)
    assert isinstance(result1, asyncio.Task)
    result1.cancel()

    # Simulate retry: begin_step again for the same step
    ts.begin_step([], step_no=1, turn_id="t1")
    tc2 = ToolCall(
        id="c2",
        function=ToolCall.FunctionBody(
            name="ToolA",
            arguments=args,
        ),
    )
    result2 = ts.handle(tc2)
    assert isinstance(result2, asyncio.Task)
    assert result2 is not result1

    # The new task should complete successfully (not raise CancelledError)
    tr = await result2
    assert tr.return_value.output == "a"


async def test_cross_step_dedup_not_triggered_after_back_to_the_future():
    """When _last_tool_calls is emptied (back_to_the_future), the same call must not be treated as a cross-step duplicate."""
    ts = _make_toolset()

    # Step 1: execute a tool
    args = json.dumps({"value": "x"})
    ts.begin_step([], step_no=1, turn_id="t1")
    tc1 = ToolCall(
        id="c1",
        function=ToolCall.FunctionBody(
            name="ToolA",
            arguments=args,
        ),
    )
    result1 = ts.handle(tc1)
    assert isinstance(result1, asyncio.Task)
    await result1
    last_calls = ts.end_step()
    assert last_calls == [("ToolA", '{"value":"x"}')]

    # Simulate back_to_the_future: caller clears last_calls
    last_calls = []

    # Step 2: same call with empty last_calls should execute normally
    ts.begin_step(last_calls, step_no=2, turn_id="t1")
    tc2 = ToolCall(
        id="c2",
        function=ToolCall.FunctionBody(
            name="ToolA",
            arguments=args,
        ),
    )
    result2 = ts.handle(tc2)
    assert isinstance(result2, asyncio.Task)
    tr = await result2

    # Should NOT have the cross-step reminder appended
    assert tr.return_value.output == "a"
    assert ts.dedup_triggered is False



# --- Dynamic tool output byte budget ---


class _MockLLM:
    def __init__(self, max_context_size: int) -> None:
        self.max_context_size = max_context_size


class _MockRuntime:
    def __init__(self, max_context_size: int) -> None:
        self.llm = _MockLLM(max_context_size)


class _EchoTool(CallableTool2[DummyParams]):
    name: str = "EchoTool"
    description: str = "Echoes the input value"
    params: type[DummyParams] = DummyParams

    async def __call__(self, params: DummyParams) -> ToolReturnValue:
        return ToolOk(output=params.value)


def test_max_output_bytes_fallback_without_runtime():
    """Without a runtime the byte budget falls back to the original 128 KiB."""
    ts = KimiToolset()
    assert ts._get_max_output_bytes() == 128 << 10


def test_max_output_bytes_with_total_context_budget():
    """Empty context: the remaining-context term dominates for typical models."""
    ts = KimiToolset(runtime=_MockRuntime(32_768))
    # total_budget = 32768 * 4 = 131072
    # remaining_budget = int(32768 * 4 * 0.9) = 117964
    assert ts._get_max_output_bytes() == 117_964


def test_max_output_bytes_with_partial_context():
    """Partially filled context shrinks the budget via the remaining-context term."""
    ts = KimiToolset(
        runtime=_MockRuntime(131_072),
        context_token_provider=lambda: 65_536,
    )
    # total_budget = 131072 * 4 = 524288
    # remaining_budget = int((131072 - 65536) * 4 * 0.9) = 235929
    assert ts._get_max_output_bytes() == 235_929


def test_max_output_bytes_near_full_context():
    """Near-full context drives the budget toward zero."""
    ts = KimiToolset(
        runtime=_MockRuntime(131_072),
        context_token_provider=lambda: 130_000,
    )
    # remaining_budget = int((131072 - 130000) * 4 * 0.9) = 3859
    assert ts._get_max_output_bytes() == 3_859


def test_max_output_bytes_absolute_ceiling():
    """Very large contexts are capped by the absolute 1 MiB ceiling."""
    ts = KimiToolset(runtime=_MockRuntime(1_048_576))
    assert ts._get_max_output_bytes() == 1 << 20


def test_set_context_token_provider_overrides_provider():
    """The setter can replace the callback used by _get_max_output_bytes."""
    ts = KimiToolset(
        runtime=_MockRuntime(131_072),
        context_token_provider=lambda: 65_536,
    )
    assert ts._get_max_output_bytes() == 235_929

    ts.set_context_token_provider(lambda: 130_000)
    assert ts._get_max_output_bytes() == 3_859


async def test_oversized_string_output_is_spilled():
    """A string tool output above the dynamic limit is exported to a temp file."""
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
    output = tr.return_value.output
    assert isinstance(output, str)
    assert output.startswith("Output too large (200000 bytes), exported to")

    path = output.split("exported to `")[-1].rstrip("`")
    assert Path(path).exists()
    assert Path(path).read_text(encoding="utf-8") == large_output


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
