"""Planner regression: no post-answer turn, and no request ending on a model turn.

Reproduces the field failure reported while using ``prompt_plan_async`` with an
OpenAI-compatible Gemini endpoint:

    Revision failed: Error code: 400 - {
      "error": {"code": 400,
                "message": "Requests ending with a model turn are not supported.",
                "status": "INVALID_ARGUMENT"}
    }

It happened *after* ``write_plan`` had already saved the plan: the soul-layer
verification gate nudged the planner ("you modified code but ran no
verification") as a ``<system-reminder>``, ``strip_system_reminders`` deleted it
on the next step, and the follow-up request therefore ended on the assistant
turn — which Gemini rejects. The planner also must not get an extra turn after
its done answer at all.

This test drives a real planner session (TodoMaker manifest, read-only lock, the
same creation path as ``prompt_plan_async``) with a scripted chat provider, so
the assertion is on the *requests* the soul actually sends.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Self

import kimi_cli.tools.note as note
import pytest
from kaos.path import KaosPath
from kimi_cli.llm import LLM
from kimi_cli.soul.message import is_system_reminder_message
from kosong.chat_provider import StreamedMessagePart, ThinkingEffort, TokenUsage
from kosong.message import Message, TextPart, ToolCall
from kosong.tooling import Tool

from kimix.utils.prompt import lock_planner_session_read_only
from kimix.utils.session import _create_session_async, close_session_async
from kimix.utils.system_prompt import SystemPromptType

PROVIDER = {
    "type": "openai_legacy",
    "model": "planner-test-model",
    "url": "http://planner.invalid",
    "api_key": "planner-test-key",
    "max_context_size": 128000,
}
REQUIREMENT = (
    "read the following requirement carefully and generate a comprehensive plan. "
    "save the complete plan to a file using the write_plan tool. "
    "Requirement:\nAdd a greet() helper."
)


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
        return "planner-test"

    @property
    def usage(self) -> TokenUsage | None:
        return None


class _PlanThenAnswerProvider:
    """Writes the plan on step 1, answers on step 2 — then must stop."""

    name = "plan-then-answer"

    def __init__(self) -> None:
        self.histories: list[list[Message]] = []

    def model_name(self) -> str:
        return "plan-then-answer"

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
                        id="tc-plan",
                        function=ToolCall.FunctionBody(
                            name="write_plan",
                            arguments='{"content": "# Plan\\n\\n1. add greet()\\n"}',
                        ),
                    )
                ]
            )
        return _Stream([TextPart(text="Plan saved.")])

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


@pytest.mark.asyncio
async def test_planner_has_no_extra_turn_and_never_ends_a_request_on_assistant(
    tmp_path: Path,
) -> None:
    plan_file = tmp_path / "plan.md"
    provider = _PlanThenAnswerProvider()
    session = None
    note._enable_plan = True
    try:
        session = await _create_session_async(
            agent_type=SystemPromptType.TodoMaker,
            agent_file="agent_planner.json",
            provider_dict=dict(PROVIDER),
            chat_provider=provider,
            work_dir=KaosPath(str(tmp_path)),
        )
        session.get_custom_data()["plan_writing_path"] = plan_file
        lock_planner_session_read_only(session)
        # The planner session owns a read-only runtime (no notifications, no
        # verification duty) and a fake LLM with the provider under test.
        session._cli.soul.runtime.llm = LLM(
            chat_provider=provider, max_context_size=128000, capabilities=set()
        )

        async for _message in session.prompt(REQUIREMENT):
            pass
        stored_history = list(session._cli.soul.context.history)
    finally:
        note._enable_plan = False
        if session is not None:
            await close_session_async(session)

    # The plan really was written by the tool call.
    assert plan_file.is_file()
    assert "greet()" in plan_file.read_text(encoding="utf-8")

    # Exactly two LLM requests: the plan tool call and the done answer. A third
    # one means the planner was nudged into an extra turn after answering.
    assert len(provider.histories) == 2, (
        f"planner was asked for {len(provider.histories)} turns: "
        f"{[[m.role for m in h] for h in provider.histories]}"
    )
    # Every request must end on a user/tool turn — never on the model turn.
    assert "assistant" not in [history[-1].role for history in provider.histories]
    # No verification-gate nudge leaked into the planner's context.
    assert not any(
        "[verification-gate]" in message.extract_text(" ") or is_system_reminder_message(message)
        for message in stored_history
    )
