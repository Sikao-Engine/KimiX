from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from kosong import StepResult
from kosong.chat_provider import TokenUsage
from kosong.message import ContentPart, Message, ToolCall
from kosong.tooling import CallableTool2, ToolOk, ToolReturnValue
from kosong.tooling.empty import EmptyToolset
from pydantic import BaseModel

import kimi_cli.soul.kimisoul as kimisoul_module
from kimi_cli.llm import LLM, ModelCapability
from kimi_cli.soul import LLMNotSupported, run_soul
from kimi_cli.soul.agent import Agent, Runtime
from kimi_cli.soul.approval import Approval
from kimi_cli.soul.context import Context
from kimi_cli.soul.kimisoul import KimiSoul
from kimi_cli.soul.message import is_system_reminder_message, system_reminder
from kimi_cli.soul.toolset import KimiToolset
from kimi_cli.utils.aioqueue import QueueShutDown
from kimi_cli.wire import Wire
from kimi_cli.wire.types import ImageURLPart, SteerInput, StepBegin, TextPart, TurnBegin, TurnEnd


@pytest.fixture
def approval() -> Approval:
    """Override global yolo=True fixture; steer tests don't need yolo."""
    return Approval(yolo=False)


def _make_soul(runtime: Runtime, tmp_path: Path) -> KimiSoul:
    agent = Agent(
        name="Steer Test Agent",
        system_prompt="Test prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    return KimiSoul(agent, context=Context(file_backend=tmp_path / "history.jsonl"))


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
    )


def _llm_with_capabilities(runtime: Runtime, capabilities: set[ModelCapability]) -> LLM:
    assert runtime.llm is not None
    return LLM(
        chat_provider=runtime.llm.chat_provider,
        max_context_size=runtime.llm.max_context_size,
        capabilities=capabilities,
        model_config=runtime.llm.model_config,
        provider_config=runtime.llm.provider_config,
    )


@pytest.mark.asyncio
async def test_inject_steer_appends_plain_user_message(runtime: Runtime, tmp_path: Path) -> None:
    soul = _make_soul(runtime, tmp_path)

    await soul._inject_steer("Stop after summarizing the diff.")

    assert len(soul.context.history) == 1
    message = soul.context.history[0]
    assert message.role == "user"
    assert message.content == [TextPart(text="Stop after summarizing the diff.")]


@pytest.mark.asyncio
async def test_inject_steer_preserves_content_parts(runtime: Runtime, tmp_path: Path) -> None:
    soul = _make_soul(runtime, tmp_path)

    parts: list[ContentPart] = [
        TextPart(text="Focus on tests."),
        TextPart(text="Explain failures."),
    ]
    await soul._inject_steer(parts)

    message = soul.context.history[0]
    assert message.content == parts


@pytest.mark.asyncio
async def test_inject_steer_rejects_unsupported_media_and_keeps_context_clean(
    runtime: Runtime,
    tmp_path: Path,
) -> None:
    soul = _make_soul(_runtime_with_llm(runtime, _llm_with_capabilities(runtime, set())), tmp_path)

    with pytest.raises(LLMNotSupported):
        await soul._inject_steer(
            [ImageURLPart(image_url=ImageURLPart.ImageURL(url="https://example.com/diagram.png"))]
        )

    assert soul.context.history == []


@pytest.mark.asyncio
async def test_consume_pending_steers_appends_history_before_emitting_wire_event(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    soul = _make_soul(runtime, tmp_path)
    sent: list[SteerInput] = []

    def fake_wire_send(msg) -> None:
        assert soul.context.history == [
            Message(role="user", content=[TextPart(text="Follow up now.")])
        ]
        assert isinstance(msg, SteerInput)
        sent.append(msg)

    monkeypatch.setattr(kimisoul_module, "wire_send", fake_wire_send)

    soul.steer("Follow up now.")

    assert await soul._consume_pending_steers() is True
    assert sent == [SteerInput(user_input="Follow up now.")]


@pytest.mark.asyncio
async def test_consume_pending_steers_does_not_emit_wire_event_for_unsupported_media(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    soul = _make_soul(_runtime_with_llm(runtime, _llm_with_capabilities(runtime, set())), tmp_path)
    sent: list[SteerInput] = []
    monkeypatch.setattr(kimisoul_module, "wire_send", lambda msg: sent.append(msg))

    soul.steer(
        [ImageURLPart(image_url=ImageURLPart.ImageURL(url="https://example.com/diagram.png"))]
    )

    with pytest.raises(LLMNotSupported):
        await soul._consume_pending_steers()

    assert sent == []
    assert soul.context.history == []


@pytest.mark.asyncio
async def test_consume_pending_steers_preserves_fifo_order_and_emits_matching_events(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    soul = _make_soul(runtime, tmp_path)
    sent: list[SteerInput] = []

    monkeypatch.setattr(kimisoul_module, "wire_send", lambda msg: sent.append(msg))

    soul.steer("first")
    soul.steer("second")

    assert await soul._consume_pending_steers() is True
    assert soul.context.history == [
        Message(role="user", content=[TextPart(text="first")]),
        Message(role="user", content=[TextPart(text="second")]),
    ]
    assert sent == [SteerInput(user_input="first"), SteerInput(user_input="second")]


@pytest.mark.asyncio
async def test_agent_loop_injects_steer_between_completed_steps(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    soul = _make_soul(runtime, tmp_path)
    sent: list[object] = []
    step_calls = 0

    async def fake_request(sender: str, action: str, description: str, display=None):
        await asyncio.Future()

    monkeypatch.setattr(soul._approval, "request", fake_request)
    monkeypatch.setattr(kimisoul_module, "wire_send", lambda msg: sent.append(msg))

    async def fake_step():
        nonlocal step_calls
        step_calls += 1
        if step_calls == 1:
            await soul.context.append_message(
                Message(role="assistant", content=[TextPart(text="tool call finished")])
            )
            await soul.context.append_message(
                Message(
                    role="tool",
                    content=[TextPart(text="tool output")],
                    tool_call_id="call-1",
                )
            )
            soul.steer("follow-up steer")
            return None

        assert soul.context.history == [
            Message(role="assistant", content=[TextPart(text="tool call finished")]),
            Message(
                role="tool",
                content=[TextPart(text="tool output")],
                tool_call_id="call-1",
            ),
            Message(role="user", content=[TextPart(text="follow-up steer")]),
        ]
        return kimisoul_module.StepOutcome(
            stop_reason="no_tool_calls",
            assistant_message=Message(role="assistant", content=[TextPart(text="done")]),
        )

    monkeypatch.setattr(soul, "_step", fake_step)

    outcome = await soul._agent_loop()

    assert outcome.stop_reason == "no_tool_calls"
    assert [msg for msg in sent if isinstance(msg, StepBegin)] == [StepBegin(n=1), StepBegin(n=2)]
    assert [msg for msg in sent if isinstance(msg, SteerInput)] == [
        SteerInput(user_input="follow-up steer")
    ]


@pytest.mark.asyncio
async def test_agent_loop_continues_after_tool_rejected_when_steer_is_injected(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    soul = _make_soul(runtime, tmp_path)
    sent: list[object] = []
    step_calls = 0

    async def fake_request(sender: str, action: str, description: str, display=None):
        await asyncio.Future()

    monkeypatch.setattr(soul._approval, "request", fake_request)
    monkeypatch.setattr(kimisoul_module, "wire_send", lambda msg: sent.append(msg))

    async def fake_step():
        nonlocal step_calls
        step_calls += 1
        if step_calls == 1:
            await soul.context.append_message(
                Message(role="assistant", content=[TextPart(text="plan blocked the tool call")])
            )
            await soul.context.append_message(
                Message(
                    role="tool",
                    content=[TextPart(text="<system>ERROR: rejected</system>")],
                    tool_call_id="call-1",
                )
            )
            soul.steer("switch to a read-only approach")
            return kimisoul_module.StepOutcome(
                stop_reason="tool_rejected",
                assistant_message=Message(
                    role="assistant",
                    content=[TextPart(text="plan blocked the tool call")],
                ),
            )

        assert soul.context.history == [
            Message(role="assistant", content=[TextPart(text="plan blocked the tool call")]),
            Message(
                role="tool",
                content=[TextPart(text="<system>ERROR: rejected</system>")],
                tool_call_id="call-1",
            ),
            Message(role="user", content=[TextPart(text="switch to a read-only approach")]),
        ]
        return kimisoul_module.StepOutcome(
            stop_reason="no_tool_calls",
            assistant_message=Message(role="assistant", content=[TextPart(text="done")]),
        )

    monkeypatch.setattr(soul, "_step", fake_step)

    outcome = await soul._agent_loop()

    assert outcome.stop_reason == "no_tool_calls"
    assert [msg for msg in sent if isinstance(msg, StepBegin)] == [StepBegin(n=1), StepBegin(n=2)]
    assert [msg for msg in sent if isinstance(msg, SteerInput)] == [
        SteerInput(user_input="switch to a read-only approach")
    ]


@pytest.mark.asyncio
async def test_step_keeps_steer_separate_from_system_reminder_in_model_history(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain steer followed by a system-reminder user message (the shape the
    verification gate produces) must stay two separate messages: history
    normalization must not merge the reminder into the steer message, so the
    cached prefix boundary stays stable across steps."""
    soul = _make_soul(runtime, tmp_path)
    captured_history: list[Message] = []

    await soul.context.append_message(
        [
            Message(role="user", content=[TextPart(text="Original question")]),
            Message(role="assistant", content=[TextPart(text="Original answer")]),
        ]
    )
    await soul._inject_steer("Follow user note")
    # Simulate a reminder producer (e.g. the verification gate) appending its
    # reminder right after the steer, within the same step boundary.
    await soul.context.append_message(
        Message(
            role="user",
            content=[system_reminder("Internal reminder")],
        )
    )
    # The reminder is fresh for this step: prevent the stale-reminder strip at
    # _step entry from removing it, so it reaches the model history.
    monkeypatch.setattr(kimisoul_module, "strip_system_reminders", lambda _history: 0)

    async def fake_kosong_step(chat_provider, system_prompt, toolset, history, **kwargs):
        captured_history[:] = list(history)
        return StepResult(
            id="step-1",
            message=Message(role="assistant", content=[TextPart(text="done")]),
            usage=None,
            tool_calls=[],
            _tool_result_futures={},
        )

    monkeypatch.setattr(kimisoul_module.kosong, "step", fake_kosong_step)
    monkeypatch.setattr(kimisoul_module, "wire_send", lambda _msg: None)
    outcome = await soul._step()
    assert outcome is not None
    assert soul.context.history[-3:] == [
        Message(role="user", content=[TextPart(text="Follow user note")]),
        Message(
            role="user",
            content=[TextPart(text="<system-reminder>\nInternal reminder\n</system-reminder>")],
        ),
        Message(role="assistant", content=[TextPart(text="done")]),
    ]
    # cache-01: system-reminder injections must NOT be merged into the real
    # user message — the model history keeps the steer and the reminder as
    # separate messages so the cached prefix boundary stays stable across steps.
    assert captured_history[-2].role == "user"
    assert captured_history[-2].content == [TextPart(text="Follow user note")]
    assert captured_history[-1].role == "user"
    assert captured_history[-1].content == [
        TextPart(text="<system-reminder>\nInternal reminder\n</system-reminder>")
    ]


class _RecordingPruner:
    """Stub pruner that records the kwargs of every prune() call."""

    def __init__(self) -> None:
        self.kwargs: dict | None = None

    def prune(self, history, **kwargs):
        self.kwargs = dict(kwargs)
        from kimi_cli.soul.context_pruning import PruningResult

        return PruningResult(
            messages=list(history),
            elided=[],
            freed_tokens=0,
            earliest_removed_index=None,
        )


@pytest.mark.asyncio
async def test_step_passes_current_turn_index_to_pruner(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cache-02 call-site test: ``_step`` must hand the pruner a non-None
    ``current_turn_index`` pointing at the current turn's first *real* user
    message (skipping injected system reminders)."""
    soul = _make_soul(runtime, tmp_path)

    await soul.context.append_message(
        [
            Message(role="user", content=[TextPart(text="u0")]),
            Message(role="assistant", content=[TextPart(text="a0")]),
            Message(role="user", content=[TextPart(text="u1")]),
            # Injected reminder after the real user message — must be skipped.
            Message(role="user", content=[system_reminder("reminder")]),
        ]
    )

    async def fake_kosong_step(chat_provider, system_prompt, toolset, history, **kwargs):
        return StepResult(
            id="step-1",
            message=Message(role="assistant", content=[TextPart(text="done")]),
            usage=None,
            tool_calls=[],
            _tool_result_futures={},
        )

    recording = _RecordingPruner()
    soul._pruner = recording  # type: ignore[assignment]
    monkeypatch.setattr(kimisoul_module.kosong, "step", fake_kosong_step)
    monkeypatch.setattr(kimisoul_module, "wire_send", lambda _msg: None)

    outcome = await soul._step()

    assert outcome is not None
    assert recording.kwargs is not None
    # History: [u0, a0, u1, reminder] → last real user message is at index 2.
    assert recording.kwargs.get("current_turn_index") == 2
    assert recording.kwargs.get("min_cache_prefix_depth") is not None


class _SequenceStreamedMessage:
    def __init__(self, parts: list[TextPart]) -> None:
        self._parts = list(parts)

    def __aiter__(self):
        return self

    async def __anext__(self) -> TextPart:
        if not self._parts:
            raise StopAsyncIteration
        return self._parts.pop(0)

    @property
    def id(self) -> str | None:
        return "sequence"

    @property
    def usage(self):
        return None


class _SequenceChatProvider:
    name = "sequence"

    def __init__(self, sequences: list[list[TextPart]]) -> None:
        self._sequences = [list(parts) for parts in sequences]
        self._calls = 0

    @property
    def model_name(self) -> str:
        return "sequence"

    @property
    def thinking_effort(self):
        return None

    async def generate(self, system_prompt, tools, history):
        index = min(self._calls, len(self._sequences) - 1)
        self._calls += 1
        return _SequenceStreamedMessage(self._sequences[index])

    def with_thinking(self, effort):
        return self


@pytest.mark.asyncio
async def test_run_soul_emits_steer_input_and_continues_same_turn(
    runtime: Runtime,
    tmp_path: Path,
) -> None:
    assert runtime.llm is not None
    llm = LLM(
        chat_provider=_SequenceChatProvider(
            [
                [TextPart(text="first answer")],
                [TextPart(text="second answer")],
            ]
        ),
        max_context_size=runtime.llm.max_context_size,
        capabilities=runtime.llm.capabilities,
    )
    agent = Agent(
        name="Steer Test Agent",
        system_prompt="Test prompt.",
        toolset=EmptyToolset(),
        runtime=_runtime_with_llm(runtime, llm),
    )
    soul = KimiSoul(agent, context=Context(file_backend=tmp_path / "history.jsonl"))

    seen: list[object] = []
    injected = False

    async def ui_loop(wire: Wire) -> None:
        nonlocal injected
        wire_ui = wire.ui_side(merge=True)
        while True:
            try:
                msg = await wire_ui.receive()
            except QueueShutDown:
                return
            seen.append(msg)
            if not injected and msg == TextPart(text="first answer"):
                soul.steer("follow-up steer")
                injected = True

    await run_soul(soul, "original question", ui_loop, asyncio.Event())

    assert soul.context.history == [
        Message(role="user", content=[TextPart(text="original question")]),
        Message(role="assistant", content=[TextPart(text="first answer")]),
        Message(role="user", content=[TextPart(text="follow-up steer")]),
        Message(role="assistant", content=[TextPart(text="second answer")]),
    ]
    assert [msg for msg in seen if isinstance(msg, TurnBegin)] == [
        TurnBegin(user_input="original question")
    ]
    assert [msg for msg in seen if isinstance(msg, SteerInput)] == [
        SteerInput(user_input="follow-up steer")
    ]
    assert [msg for msg in seen if isinstance(msg, StepBegin)] == [StepBegin(n=1), StepBegin(n=2)]
    assert isinstance(seen[-1], TurnEnd)


def test_is_system_reminder_message_detects_internal_reminder_message() -> None:
    assert is_system_reminder_message(
        Message(
            role="user",
            content=[TextPart(text="<system-reminder>\nStay on task.\n</system-reminder>")],
        )
    )


def test_is_system_reminder_message_rejects_regular_user_message() -> None:
    assert (
        is_system_reminder_message(Message(role="user", content=[TextPart(text="hello")])) is False
    )


# ---------------------------------------------------------------------------
# Regression: a steer interrupting a step mid context-growth must not split
# the assistant tool_call / tool-result pairing in the persisted history
# (strict backends 400 on "an assistant message with 'tool_calls' must be
# followed by tool messages responding to each 'tool_call_id'").
# ---------------------------------------------------------------------------


class _SteerRaceParams(BaseModel):
    value: str = ""


class _SteerRaceTool(CallableTool2[_SteerRaceParams]):
    name = "SteerTool"
    description = "Dummy tool; the steer is fired separately mid context-growth."
    params = _SteerRaceParams

    async def __call__(self, params: _SteerRaceParams) -> ToolReturnValue:
        return ToolOk(output="tool done")


class _SteerRaceStream:
    def __init__(self, parts: list, usage: TokenUsage | None = None) -> None:
        self._iter = self._to_stream(parts)
        self._usage = usage

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._iter.__anext__()

    async def _to_stream(self, parts: list):
        for part in parts:
            yield part

    @property
    def id(self) -> str:
        return "steer-race"

    @property
    def usage(self) -> TokenUsage | None:
        return self._usage


class _SteerRaceChatProvider:
    """Step 1 calls SteerTool (with usage, so _grow_context updates the token
    count); later steps answer with plain text."""

    name = "steer-race"

    def __init__(self) -> None:
        self._n = 0

    @property
    def model_name(self) -> str:
        return "steer-race"

    @property
    def thinking_effort(self):
        return None

    async def generate(self, system_prompt, tools, history):
        self._n += 1
        if self._n == 1:
            tool_call = ToolCall(
                id="tc-1",
                function=ToolCall.FunctionBody(name="SteerTool", arguments="{}"),
            )
            return _SteerRaceStream(
                [tool_call],
                usage=TokenUsage(
                    input_other=1, output=1, input_cache_read=0, input_cache_creation=0
                ),
            )
        return _SteerRaceStream([TextPart(text="done")])

    def with_thinking(self, effort):
        return self


@pytest.mark.asyncio
async def test_steer_interrupt_during_context_growth_keeps_tool_pairing_intact(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Steer wake cancels the step task while the shielded ``_grow_context``
    sits between the assistant-message append and the tool-result append.
    The step must wait for the shielded growth to finish before the
    cancellation path consumes steers, so the persisted history never becomes
    ``assistant(tool_calls) -> user -> tool``.
    """
    toolset = KimiToolset()
    toolset.add(_SteerRaceTool())
    llm = LLM(
        chat_provider=_SteerRaceChatProvider(),
        max_context_size=100_000,
        capabilities=set(),
    )
    agent = Agent(
        name="Steer Race Agent",
        system_prompt="Test prompt.",
        toolset=toolset,
        runtime=_runtime_with_llm(runtime, llm),
    )
    soul = KimiSoul(agent, context=Context(file_backend=tmp_path / "history.jsonl"))

    update_calls = 0
    original_update = soul._context.update_token_count

    async def _update_then_steer(token_count: int) -> None:
        nonlocal update_calls
        update_calls += 1
        await original_update(token_count)
        if update_calls == 2:
            # Second update happens inside the shielded _grow_context, after
            # the assistant message was appended but before the tool-result
            # messages.  Fire the steer wake now and hold the window open so
            # the step cancellation lands exactly here.
            soul._steer_queue.put_nowait("background subagent finished")
            soul._steer_wake_event.set()
            await asyncio.sleep(0.1)

    monkeypatch.setattr(soul._context, "update_token_count", _update_then_steer)
    monkeypatch.setattr(kimisoul_module, "wire_send", lambda _msg: None)

    await soul.run("go")

    history = soul.context.history
    assert [m.role for m in history] == ["user", "assistant", "tool", "user", "assistant"]
    assistant_message = history[1]
    assert assistant_message.tool_calls is not None
    assert [tc.id for tc in assistant_message.tool_calls] == ["tc-1"]
    assert history[2].tool_call_id == "tc-1"
    assert history[3].extract_text() == "background subagent finished"
