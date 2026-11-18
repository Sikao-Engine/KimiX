"""Turn-level regression tests: a request must never end on a model turn.

Background: the verification gate used to append its nudge as a
``<system-reminder>``-only user message. ``_step`` starts with
``strip_system_reminders``, so the nudge was deleted before the LLM ever saw
it and the request history ended on the assistant turn. Strict providers
reject that outright — Gemini answers::

    Error code: 400 - "Requests ending with a model turn are not supported."

which is exactly what a plan-mode planner hit right after ``write_plan``
succeeded (the plan was written, the run still failed).

Covered here:
- the gate nudge survives as a real user message and reaches the model;
- a read-only (plan-mode) session gets no extra turn after its final answer;
- ``_repair_model_tail`` keeps *any* request valid if a continuation is lost.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Self

import pytest
from pydantic import BaseModel

import kimi_cli.soul.kimisoul as kimisoul_module
from kimi_cli.llm import LLM
from kimi_cli.soul.agent import Agent, Runtime
from kimi_cli.soul.context import Context
from kimi_cli.soul.kimisoul import KimiSoul
from kimi_cli.soul.message import is_system_reminder_message
from kimi_cli.soul.toolset import KimiToolset
from kosong.chat_provider import StreamedMessagePart, ThinkingEffort, TokenUsage
from kosong.message import Message, TextPart, ToolCall
from kosong.tooling import CallableTool2, Tool, ToolOk, ToolReturnValue


class _Params(BaseModel):
    value: str = ""


class _StubTool(CallableTool2[_Params]):
    """Named tool stub so tool classification sees the real tool name."""

    params = _Params
    description = "Stub tool for gate tests."

    def __init__(self, name: str) -> None:
        super().__init__(name=name, description="Stub tool for gate tests.")

    async def __call__(self, params: _Params) -> ToolReturnValue:
        return ToolOk(output=f"{self.name} ok")


class _Stream:
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
        return "gate-tail"

    @property
    def usage(self) -> TokenUsage | None:
        return None


class _ScriptedProvider:
    """Replays a fixed script: first an edit tool call, then plain text."""

    name = "scripted"

    def __init__(self) -> None:
        self.histories: list[list[Message]] = []

    def model_name(self) -> str:
        return "scripted"

    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
    ) -> _Stream:
        step = len(self.histories)
        self.histories.append(list(history))
        if step == 0:
            return _Stream(
                [
                    ToolCall(
                        id="tc-write",
                        function=ToolCall.FunctionBody(
                            name="write", arguments='{"path":"a.py","content":"x"}'
                        ),
                    )
                ]
            )
        return _Stream([TextPart(text=f"finished {step}")])

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


def _runtime_with_provider(runtime: Runtime, provider: _ScriptedProvider) -> Runtime:
    llm = LLM(chat_provider=provider, max_context_size=100_000, capabilities=set())
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
        read_only=runtime.read_only,
    )


def _make_soul(
    runtime: Runtime, provider: _ScriptedProvider, tool_names: Sequence[str], tmp_path: Path
) -> KimiSoul:
    toolset = KimiToolset()
    for name in tool_names:
        toolset.add(_StubTool(name))
    agent = Agent(
        name="Gate Tail Agent",
        system_prompt="Test system prompt.",
        toolset=toolset,
        runtime=_runtime_with_provider(runtime, provider),
    )
    return KimiSoul(agent, context=Context(file_backend=tmp_path / "history.jsonl"))


def _tail_roles(provider: _ScriptedProvider) -> list[str]:
    return [history[-1].role for history in provider.histories if history]


@pytest.mark.asyncio
async def test_gate_nudge_reaches_model_as_real_user_message(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The nudge must survive ``strip_system_reminders`` and keep the tail valid."""
    monkeypatch.setattr(kimisoul_module, "wire_send", lambda _msg: None)
    provider = _ScriptedProvider()
    # ``bash`` makes the toolset verification-capable, so the edit nudge fires.
    soul = _make_soul(runtime, provider, ["write", "bash"], tmp_path)

    outcome = await soul._turn(Message(role="user", content="implement it"))

    # request 1 (user) -> write call, request 2 (tool) -> text, request 3 (nudge)
    assert len(provider.histories) == 3
    assert outcome.stop_reason == "no_tool_calls"
    assert "assistant" not in _tail_roles(provider), (
        f"a request ended on a model turn: {_tail_roles(provider)}"
    )
    nudge = provider.histories[2][-1]
    assert nudge.role == "user"
    assert "[verification-gate]" in nudge.extract_text(" ")
    assert not is_system_reminder_message(nudge)
    # ...and it is persisted, so the stored history matches what was sent.
    assert "[verification-gate]" in soul.context.history[-2].extract_text(" ")


@pytest.mark.asyncio
async def test_read_only_session_gets_no_extra_turn_after_answer(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plan mode: the turn ends on the done answer (no post-answer nudge)."""
    monkeypatch.setattr(kimisoul_module, "wire_send", lambda _msg: None)
    runtime.read_only = True
    provider = _ScriptedProvider()
    soul = _make_soul(runtime, provider, ["write", "bash"], tmp_path)

    outcome = await soul._turn(Message(role="user", content="write the plan"))

    assert len(provider.histories) == 2, "read-only session was nudged into an extra turn"
    assert outcome.stop_reason == "no_tool_calls"
    assert outcome.step_count == 2
    assert "assistant" not in _tail_roles(provider)
    assert not any(
        "[verification-gate]" in message.extract_text(" ") for message in soul.context.history
    )


@pytest.mark.asyncio
async def test_repair_model_tail_appends_continuation(
    runtime: Runtime,
    tmp_path: Path,
) -> None:
    """A lost continuation is repaired instead of failing the provider request."""
    provider = _ScriptedProvider()
    soul = _make_soul(runtime, provider, ["write"], tmp_path)
    history = [
        Message(role="user", content=[TextPart(text="do it")]),
        Message(role="assistant", content=[TextPart(text="done")]),
    ]

    repaired = await soul._repair_model_tail(history)

    assert repaired[:-1] == history
    assert repaired[-1].role == "user"
    assert "[continuation]" in repaired[-1].extract_text(" ")
    assert soul.context.history[-1].role == "user"


@pytest.mark.asyncio
async def test_repair_model_tail_leaves_tool_call_tail_alone(
    runtime: Runtime,
    tmp_path: Path,
) -> None:
    """An assistant tool-call tail is completed by its tool results, not a nudge."""
    provider = _ScriptedProvider()
    soul = _make_soul(runtime, provider, ["write"], tmp_path)
    history = [
        Message(role="user", content=[TextPart(text="do it")]),
        Message(
            role="assistant",
            content=[],
            tool_calls=[
                ToolCall(
                    id="tc-1",
                    function=ToolCall.FunctionBody(
                        name="write", arguments='{"path":"a.py","content":"x"}'
                    ),
                )
            ],
        ),
    ]

    assert await soul._repair_model_tail(history) == history
    assert soul.context.history == []
