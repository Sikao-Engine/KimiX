"""Tests for the entry-point plumbing owned by ``run_soul``.

These behaviors used to live inside ``KimiSoul.run`` and were moved to the
``run_soul`` funnel so the core turn logic stays focused:

- the empty-input guard,
- the approval-source lifecycle,
- session auto-titling after the first real turn.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest
from kosong.message import Message
from kosong.tooling.empty import EmptyToolset

from kimi_cli.approval_runtime import (
    ApprovalSource,
    get_current_approval_source_or_none,
    reset_current_approval_source,
    set_current_approval_source,
)
from kimi_cli.soul import run_soul
from kimi_cli.soul.agent import Agent, Runtime
from kimi_cli.soul.context import Context
from kimi_cli.soul.kimisoul import KimiSoul
from kimi_cli.utils.aioqueue import QueueShutDown
from kimi_cli.wire import Wire
from kimi_cli.wire.types import TextPart, TurnBegin, TurnEnd


def _make_soul(runtime: Runtime, tmp_path: Path) -> KimiSoul:
    agent = Agent(
        name="Entry Agent",
        system_prompt="Test system prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    return KimiSoul(agent, context=Context(file_backend=tmp_path / "history.jsonl"))


def _collecting_ui_loop(sent: list) -> Callable[[Wire], Coroutine[Any, Any, None]]:
    async def _ui_loop_fn(wire: Wire) -> None:
        ui = wire.ui_side(merge=False)
        with contextlib.suppress(QueueShutDown):
            while True:
                sent.append(await ui.receive())

    return _ui_loop_fn


def _fake_turn_run(
    recorded: list, wire_events: bool = True
) -> Callable[[KimiSoul, str | list], Coroutine[Any, Any, None]]:
    """A stand-in for ``KimiSoul.run`` that records input and (optionally)
    emits the TurnBegin/TurnEnd pair a real turn would produce."""

    async def _run(self: KimiSoul, user_input: str | list) -> None:
        recorded.append(user_input)
        if wire_events:
            from kimi_cli.soul import wire_send

            wire_send(TurnBegin(user_input=user_input))
            wire_send(TurnEnd())

    return _run


@pytest.mark.asyncio
async def test_empty_input_is_ignored(runtime: Runtime, tmp_path: Path) -> None:
    soul = _make_soul(runtime, tmp_path)
    ui_called = False

    async def _ui_loop_fn(wire: Wire) -> None:
        nonlocal ui_called
        ui_called = True

    for blank in ("", "   ", [], [TextPart(text="  ")]):
        await run_soul(soul, blank, _ui_loop_fn, asyncio.Event(), runtime=runtime)

    assert ui_called is False
    assert len(soul.context.history) == 0


@pytest.mark.asyncio
async def test_first_real_turn_sets_session_title(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    soul = _make_soul(runtime, tmp_path)
    recorded: list = []
    monkeypatch.setattr(KimiSoul, "run", _fake_turn_run(recorded))

    await run_soul(
        soul,
        "fix the parser bug",
        _collecting_ui_loop([]),
        asyncio.Event(),
        runtime=runtime,
    )

    assert recorded == ["fix the parser bug"]
    assert runtime.session.state.custom_title == "fix the parser bug"


@pytest.mark.asyncio
async def test_title_is_not_overwritten(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime.session.state.custom_title = "keep me"
    soul = _make_soul(runtime, tmp_path)
    monkeypatch.setattr(KimiSoul, "run", _fake_turn_run([]))

    await run_soul(
        soul,
        "another request",
        _collecting_ui_loop([]),
        asyncio.Event(),
        runtime=runtime,
    )

    assert runtime.session.state.custom_title == "keep me"


@pytest.mark.asyncio
async def test_foreground_turn_cancels_pending_approvals_on_cleanup(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    soul = _make_soul(runtime, tmp_path)

    async def _run_with_pending_approval(self: KimiSoul, user_input: str | list) -> None:
        source = get_current_approval_source_or_none()
        assert source is not None
        assert source.kind == "foreground_turn"
        assert self.runtime.approval_runtime is not None
        self.runtime.approval_runtime.create_request(
            sender="test",
            action="Shell",
            description="run a command",
            tool_call_id="tc-1",
            display=[],
            source=source,
            request_id="req-pending",
        )
        # Leave the request unresolved — run_soul must cancel it on cleanup.

    monkeypatch.setattr(KimiSoul, "run", _run_with_pending_approval)

    await run_soul(
        soul,
        "do something approved",
        _collecting_ui_loop([]),
        asyncio.Event(),
        runtime=runtime,
    )

    assert runtime.approval_runtime is not None
    record = runtime.approval_runtime.get_request("req-pending")
    assert record is not None
    assert record.status == "cancelled"


@pytest.mark.asyncio
async def test_outer_approval_source_is_preserved(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subagent/background runners install their own approval source before
    calling run_soul; run_soul must not override or cancel it."""
    soul = _make_soul(runtime, tmp_path)
    outer_source = ApprovalSource(
        kind="foreground_turn", id="outer-id", agent_id="a1", subagent_type="coder"
    )
    seen_sources: list = []

    async def _run(self: KimiSoul, user_input: str | list) -> None:
        seen_sources.append(get_current_approval_source_or_none())
        assert self.runtime.approval_runtime is not None
        self.runtime.approval_runtime.create_request(
            sender="test",
            action="Shell",
            description="run a command",
            tool_call_id="tc-2",
            display=[],
            # The request is attributed to the outer (subagent) source.
            source=get_current_approval_source_or_none(),  # type: ignore[arg-type]
            request_id="req-outer",
        )

    monkeypatch.setattr(KimiSoul, "run", _run)

    token = set_current_approval_source(outer_source)
    try:
        await run_soul(
            soul,
            "subagent task",
            _collecting_ui_loop([]),
            asyncio.Event(),
            runtime=runtime,
        )
    finally:
        reset_current_approval_source(token)

    assert seen_sources == [outer_source]
    # The outer source's requests are the outer layer's responsibility;
    # run_soul must not cancel them.
    assert runtime.approval_runtime is not None
    record = runtime.approval_runtime.get_request("req-outer")
    assert record is not None
    assert record.status == "pending"


@pytest.mark.asyncio
async def test_kimisoul_run_is_pure_turn_execution(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KimiSoul.run no longer dispatches slash commands or titles the session
    — a leading ``/`` is ordinary user text for the core turn (dispatch and
    titling are run_soul's job)."""
    soul = _make_soul(runtime, tmp_path)
    recorded: list = []

    async def _fake_turn(self: KimiSoul, user_message: Message) -> None:
        recorded.append(user_message)

    monkeypatch.setattr(KimiSoul, "_turn", _fake_turn)
    sent: list = []
    monkeypatch.setattr("kimi_cli.soul.kimisoul.wire_send", sent.append)

    await soul.run("/clear")

    assert len(recorded) == 1
    assert recorded[0].role == "user"
    assert TurnBegin(user_input="/clear") in sent
    assert TurnEnd() in sent
    # The command table was not consulted: the context is untouched and no
    # title was generated.
    assert len(soul.context.history) == 0
    assert runtime.session.state.custom_title is None
