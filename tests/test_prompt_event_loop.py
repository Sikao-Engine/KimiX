"""Regressions for the per-conversation event loop in ``kimix.utils.prompt``.

``prompt()`` (the synchronous entry point used by the REPL, ``/continue``,
``/plan`` and the manual-CoT loop) used to wrap *every* conversation in its own
``asyncio.run()``: a fresh loop was created and **closed** again when the
conversation ended, while the ``Session``/``KimiSoul``/toolset kept living and
was reused by the next conversation.

Anything the session created during the previous conversation therefore stayed
bound to a closed loop, so the second conversation raised::

    RuntimeError: Event loop is closed

with ``src/kimix/utils/prompt.py`` as the outermost frame of the traceback
(``prompt()`` calling ``asyncio.run``).  These tests pin down the invariant that
must hold instead: all conversations of a process share one live event loop.
"""

from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass
from typing import Any

import pytest
from kimi_cli.wire.types import TextPart

prompt_mod = importlib.import_module("kimix.utils.prompt")


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
@dataclass
class FakeStatus:
    context_usage: float = 0.0
    context_tokens: int = 0


class FakeSession:
    """Minimal session whose ``prompt`` runs caller-supplied in-loop code."""

    def __init__(self, on_prompt: Any = None) -> None:
        self.status = FakeStatus()
        self.custom_data: dict[str, Any] = {}
        self.custom_config: dict[str, Any] = {}
        self._cancel_event: asyncio.Event | None = None
        self._cli = None
        self._closed = False
        self._on_prompt = on_prompt
        self.loops: list[asyncio.AbstractEventLoop] = []
        self.prompt_calls = 0

    async def prompt(self, prompt_str: str, *, merge_wire_messages: bool = False) -> Any:
        loop = asyncio.get_running_loop()
        self.loops.append(loop)
        self.prompt_calls += 1
        if self._on_prompt is not None:
            self._on_prompt(self, loop)
        yield TextPart(text="ok")

    def cancel(self) -> None:
        pass


async def _no_sleep(*_args: Any, **_kwargs: Any) -> None:
    return None


@pytest.fixture()
def printed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Silence the prompt's stream output and record everything it prints."""
    messages: list[str] = []

    def _capture(text: str, *args: Any, **kwargs: Any) -> None:
        messages.append(text)

    monkeypatch.setattr(prompt_mod.base._stream, "colorful_print_word", _capture)
    monkeypatch.setattr(prompt_mod.base._stream, "print_word", lambda *a, **k: None)
    monkeypatch.setattr(prompt_mod, "_print_usage", lambda *a, **k: None)
    # Never sleep between retry attempts.
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    return messages


def _run(prompt_str: str, session: FakeSession) -> None:
    prompt_mod.prompt(prompt_str, session=session, ensure_todo_finished=False, info_print=False)


# --------------------------------------------------------------------------- #
# The bug: a session-scoped asyncio object created in conversation 1 is used by
# conversation 2 (exactly how the production code keeps pending waiters — e.g.
# ``BackgroundTaskManager.completion_event``, approval futures — on the session).
# --------------------------------------------------------------------------- #
def test_second_conversation_can_complete_a_loop_bound_object(printed: list[str]) -> None:
    """A waiter created during conversation 1 must still be resolvable later."""
    pending: dict[str, asyncio.Future[str]] = {}

    def on_prompt(session: FakeSession, loop: asyncio.AbstractEventLoop) -> None:
        future = pending.get("future")
        if future is None:
            # Conversation 1: the session (re)creates a waiter on its loop.
            future = loop.create_future()
            future.add_done_callback(lambda _f: None)
            pending["future"] = future
        elif not future.done():
            # Conversation 2: resolving the waiter schedules its callbacks on
            # the loop it was created on.
            future.set_result("done")

    session = FakeSession(on_prompt)

    _run("first", session)
    _run("second", session)

    assert pending["future"].result() == "done"
    # Before the fix the loop-bound future could not be resolved: the retry loop
    # logged ``Event loop is closed`` and re-ran the whole turn.
    assert not any("Event loop is closed" in message for message in printed), printed
    assert session.prompt_calls == 2


def test_conversations_share_one_live_event_loop(printed: list[str]) -> None:
    """Every conversation must run on the same, still-open event loop."""
    session = FakeSession()

    _run("first", session)
    _run("second", session)

    first, second = session.loops
    assert first is second, "each prompt created and closed its own event loop"
    assert not first.is_closed(), "the event loop was closed between conversations"


def test_prompt_plan_drives_the_planner_on_one_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """``prompt_plan`` loops over planner conversations: same invariant."""
    loops: list[asyncio.AbstractEventLoop] = []

    async def fake_prompt_plan_async(requirement: str, plan_file: Any = "plan.md") -> None:
        loops.append(asyncio.get_running_loop())

    monkeypatch.setattr(prompt_mod, "prompt_plan_async", fake_prompt_plan_async)

    prompt_mod.prompt_plan("do the thing", "plan.md")
    prompt_mod.prompt_plan("do the thing", "plan.md")

    assert loops[0] is loops[1]
    assert not loops[0].is_closed()
