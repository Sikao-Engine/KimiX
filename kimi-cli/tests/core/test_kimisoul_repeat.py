"""Soul-level coverage for the repeat / cycle loop detector.

The toolset only force-stops a turn on an *exact* tool-call cycle: the same
tool with the same canonical arguments, at most 5 calls long, repeated more
than 100 times.  The modules below drive KimiSoul end to end to prove that

  * a bounded exact cycle still ends on a plain-text answer for the user
    (recovery prompts + synthesized fallback, never an empty tool-only stop);
  * the same tool with different arguments is never treated as a loop;
  * a non-empty thinking block between tool calls clears the counters, so the
    turn is never stopped for repeating.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Self

import pytest
from pydantic import BaseModel

import kimi_cli.soul.kimisoul as kimisoul_module
import kimi_cli.soul.toolset as toolset_module
from kimi_cli.llm import LLM
from kimi_cli.soul.agent import Agent, Runtime
from kimi_cli.soul.context import Context
from kimi_cli.soul.kimisoul import KimiSoul
from kimi_cli.soul.toolset import KimiToolset
from kosong.chat_provider import StreamedMessagePart, ThinkingEffort, TokenUsage
from kosong.message import Message, TextPart, ThinkPart, ToolCall
from kosong.tooling import CallableTool2, Tool, ToolOk, ToolReturnValue


class _Params(BaseModel):
    value: str = ""


class _DummyTool(CallableTool2[_Params]):
    name = "ToolA"
    description = "Dummy tool that always succeeds."
    params = _Params

    async def __call__(self, params: _Params) -> ToolReturnValue:
        return ToolOk(output="a")


class _DummyToolB(CallableTool2[_Params]):
    name = "ToolB"
    description = "Second dummy tool for interleaved-call tests."
    params = _Params

    async def __call__(self, params: _Params) -> ToolReturnValue:
        return ToolOk(output="b")


class _RepeatStream:
    def __init__(self, parts: list[StreamedMessagePart]) -> None:
        self._iter = self._to_stream(parts)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> StreamedMessagePart:
        return await self._iter.__anext__()

    async def _to_stream(
        self, parts: list[StreamedMessagePart]
    ) -> AsyncIterator[StreamedMessagePart]:
        for part in parts:
            yield part

    @property
    def id(self) -> str | None:
        return "repeat"

    @property
    def usage(self) -> TokenUsage | None:
        return None


class _RepeatChatProvider:
    """Yields one tool call per step.

    ``cycle`` is the sequence of (tool_name, arguments) pairs replayed round
    robin; a one-element sequence reproduces a plain adjacent repeat, a longer
    one reproduces an interleaved cycle.  ``thinking`` prefixes every tool call
    with a non-empty ``ThinkPart`` (a "reasoned" step).  ``finish_after`` ends
    the replay with a plain text answer.
    """

    name = "repeat"

    def __init__(
        self,
        cycle: Sequence[tuple[str, str]] | None = None,
        *,
        finish_after: int | None = None,
        thinking: str | None = None,
    ) -> None:
        self._n = 0
        self._cycle: Sequence[tuple[str, str]] = cycle or [("ToolA", '{"value":"x"}')]
        self._finish_after = finish_after
        self._thinking = thinking
        self._histories: list[list[Message]] = []

    @property
    def model_name(self) -> str:
        return "repeat"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
    ) -> _RepeatStream:
        self._histories.append(list(history))
        if self._finish_after is not None and self._n >= self._finish_after:
            return _RepeatStream([TextPart(text="finished")])
        name, arguments = self._cycle[self._n % len(self._cycle)]
        self._n += 1
        parts: list[StreamedMessagePart] = []
        if self._thinking:
            parts.append(ThinkPart(think=self._thinking))
        parts.append(
            ToolCall(
                id=f"tc-{self._n}",
                function=ToolCall.FunctionBody(name=name, arguments=arguments),
            )
        )
        return _RepeatStream(parts)

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


def _runtime_with_llm(runtime: Runtime, llm: LLM) -> Runtime:
    return Runtime(
        config=runtime.config,
        llm=llm,
        session=runtime.session,
        builtin_args=runtime.builtin_args,
        denwa_renji=runtime.denwa_renji,
        approval=runtime.approval,
        labor_market=runtime.labor_market,
        environment=runtime.environment,
        notifications=runtime.notifications,
        background_tasks=runtime.background_tasks,
        skills=runtime.skills,
        oauth=runtime.oauth,
        additional_dirs=runtime.additional_dirs,
        skills_dirs=runtime.skills_dirs,
        role=runtime.role,
    )


def _make_soul(runtime: Runtime, llm: LLM, toolset: KimiToolset, tmp_path: Path) -> KimiSoul:
    agent = Agent(
        name="Repeat Test Agent",
        system_prompt="Test system prompt.",
        toolset=toolset,
        runtime=_runtime_with_llm(runtime, llm),
    )
    return KimiSoul(agent, context=Context(file_backend=tmp_path / "history.jsonl"))


def _make_llm(provider: _RepeatChatProvider) -> LLM:
    return LLM(
        chat_provider=provider,
        max_context_size=100_000,
        capabilities=set(),
    )


def _recovery_messages(soul: KimiSoul) -> list[str]:
    return [
        message.extract_text(" ")
        for message in soul.context.history
        if "[loop-recovery]" in message.extract_text(" ")
    ]


@pytest.mark.asyncio
async def test_turn_force_stops_on_adjacent_identical_calls(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exact adjacent cycle must NOT end the turn silently.

    The toolset's loop detector fires; the soul then runs bounded recovery
    prompts and finally returns a synthesized plain-text answer that references
    the user requirement, never an empty tool-only stop.
    """
    monkeypatch.setattr(toolset_module, "_STREAK_FORCE_STOP", 2)

    toolset = KimiToolset()
    toolset.add(_DummyTool())
    soul = _make_soul(runtime, _make_llm(_RepeatChatProvider()), toolset, tmp_path)

    monkeypatch.setattr(kimisoul_module, "wire_send", lambda _msg: None)

    outcome = await soul._turn(Message(role="user", content="go"))

    # The turn must end on a plain-text answer, never with an empty stop.
    assert outcome.stop_reason == "no_tool_calls"
    assert outcome.final_message is not None
    assert outcome.final_message.content
    text = outcome.final_message.extract_text(" ")
    assert "Original request: go" in text
    # A A A stops on the 3rd call, then the bounded recovery rounds run.
    assert outcome.step_count >= 3


@pytest.mark.asyncio
async def test_turn_allows_same_tool_with_different_arguments(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 2: different arguments are distinct work, never a repeat."""
    toolset = KimiToolset()
    toolset.add(_DummyTool())
    call_count = 60
    provider = _RepeatChatProvider(
        [("ToolA", f'{{"value":"{i}"}}') for i in range(call_count)],
        finish_after=call_count,
    )
    soul = _make_soul(runtime, _make_llm(provider), toolset, tmp_path)
    wire_messages: list[object] = []
    monkeypatch.setattr(kimisoul_module, "wire_send", wire_messages.append)

    outcome = await soul._turn(Message(role="user", content="inspect every item"))

    assert outcome.stop_reason == "no_tool_calls"
    assert outcome.final_message is not None
    assert outcome.final_message.extract_text(" ") == "finished"
    assert len(toolset._turn_call_history) == call_count
    assert not any(
        "Recovering from repeated tool calls" in getattr(message, "text", "")
        for message in wire_messages
    )
    assert _recovery_messages(soul) == []


@pytest.mark.asyncio
async def test_turn_force_stops_on_interleaved_cycle(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``A(x) -> B(x) -> A(x) -> B(x) ...`` must NOT end silently.

    The turn is converted into a recovery prompt plus a final plain-text answer
    for the user requirement.
    """
    monkeypatch.setattr(toolset_module, "_CYCLE_FORCE_STOP", 2)

    toolset = KimiToolset()
    toolset.add(_DummyTool())
    toolset.add(_DummyToolB())
    args = '{"value":"x"}'
    provider = _RepeatChatProvider([("ToolA", args), ("ToolB", args)])
    soul = _make_soul(runtime, _make_llm(provider), toolset, tmp_path)

    monkeypatch.setattr(kimisoul_module, "wire_send", lambda _msg: None)

    outcome = await soul._turn(Message(role="user", content="go"))

    assert outcome.stop_reason == "no_tool_calls"
    assert outcome.final_message is not None
    assert outcome.final_message.content
    text = outcome.final_message.extract_text(" ")
    assert "Original request: go" in text
    # ToolA's 2nd occurrence (call 3) reaches the lowered cycle stop.
    assert outcome.step_count >= 3


@pytest.mark.asyncio
async def test_loop_recovery_prompt_reaches_llm_and_requirement_kept(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the loop detector fires, recovery prompts must reach the LLM.

    Regression for the session-truncation symptom: the toolset used to
    force-stop the turn with no final text.  Now the soul injects plain-user
    recovery prompts that restate the top-level user requirement, and if the
    model keeps looping the turn ends on a synthesized text fallback
    referencing it.
    """
    monkeypatch.setattr(toolset_module, "_CYCLE_FORCE_STOP", 2)

    toolset = KimiToolset()
    toolset.add(_DummyTool())
    toolset.add(_DummyToolB())
    args = '{"value":"x"}'
    provider = _RepeatChatProvider([("ToolA", args), ("ToolB", args)])
    soul = _make_soul(runtime, _make_llm(provider), toolset, tmp_path)

    monkeypatch.setattr(kimisoul_module, "wire_send", lambda _msg: None)

    outcome = await soul._turn(Message(role="user", content="deploy the service"))

    # Never ends without a top-level answer.
    assert outcome.stop_reason == "no_tool_calls"
    assert outcome.final_message is not None
    final_text = outcome.final_message.extract_text(" ")
    assert "deploy the service" in final_text

    # The recovery prompts must actually be visible to later LLM steps
    # (not stripped as system reminders).
    recovery_calls = [
        hist
        for hist in provider._histories
        if any(
            m.role == "user" and "[loop-recovery]" in m.extract_text(" ")
            for m in hist
        )
    ]
    assert recovery_calls, "No LLM call ever saw a loop-recovery prompt"
    recovery_texts = [
        m.extract_text(" ")
        for hist in recovery_calls
        for m in hist
        if m.role == "user" and "[loop-recovery]" in m.extract_text(" ")
    ]
    assert any("deploy the service" in t for t in recovery_texts)


@pytest.mark.asyncio
async def test_turn_with_thinking_between_calls_is_never_a_loop(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 1: a thinking block between tool calls clears the counter.

    Every step below repeats the identical call and is well past the (lowered)
    force-stop threshold, but each one carries a non-empty ``ThinkPart``, so the
    soul resets the loop detectors after the step and the turn runs to its
    natural end.
    """
    monkeypatch.setattr(toolset_module, "_STREAK_REMINDER_AT", 2)
    monkeypatch.setattr(toolset_module, "_STREAK_REMINDER_2_AT", 3)
    monkeypatch.setattr(toolset_module, "_STREAK_REMINDER_3_AT", 4)
    monkeypatch.setattr(toolset_module, "_STREAK_FORCE_STOP", 3)
    monkeypatch.setattr(toolset_module, "_CYCLE_REMINDER_AT", 2)
    monkeypatch.setattr(toolset_module, "_CYCLE_FORCE_STOP", 3)

    toolset = KimiToolset()
    toolset.add(_DummyTool())
    provider = _RepeatChatProvider(thinking="let me check what changed", finish_after=8)
    soul = _make_soul(runtime, _make_llm(provider), toolset, tmp_path)
    wire_messages: list[object] = []
    monkeypatch.setattr(kimisoul_module, "wire_send", wire_messages.append)

    outcome = await soul._turn(Message(role="user", content="poll the build"))

    assert outcome.stop_reason == "no_tool_calls"
    assert outcome.final_message is not None
    assert outcome.final_message.extract_text(" ") == "finished"
    assert not any(
        "Recovering from repeated tool calls" in getattr(message, "text", "")
        for message in wire_messages
    )
    assert _recovery_messages(soul) == []


@pytest.mark.asyncio
async def test_turn_force_stops_on_different_args_overuse(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The different-args ladder is a turn-ending stop, not just a warning."""
    monkeypatch.setattr(toolset_module, "_DIFF_ARGS_FORCE_STOP", 4)

    toolset = KimiToolset()
    toolset.add(_DummyTool())
    # every step uses a new argument, so only the per-tool call count can stop it
    provider = _RepeatChatProvider([("ToolA", f'{{"value":"{i}"}}') for i in range(8)])
    soul = _make_soul(runtime, _make_llm(provider), toolset, tmp_path)

    monkeypatch.setattr(kimisoul_module, "wire_send", lambda _msg: None)

    outcome = await soul._turn(Message(role="user", content="fan out the work"))

    assert outcome.stop_reason == "no_tool_calls"
    assert outcome.final_message is not None
    assert "Original request: fan out the work" in outcome.final_message.extract_text(" ")
    # the recovery prompts restate the requirement after the forced stop
    assert _recovery_messages(soul)
    assert outcome.step_count >= 4
