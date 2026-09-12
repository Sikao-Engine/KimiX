from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest
from kosong.message import Message
from kosong.tooling.empty import EmptyToolset

import kimi_cli.soul.slash as soul_slash
from kimi_cli.soul import run_soul
from kimi_cli.soul.agent import Agent, Runtime
from kimi_cli.soul.context import Context
from kimi_cli.soul.kimisoul import KimiSoul
from kimi_cli.utils.aioqueue import QueueShutDown
from kimi_cli.wire import Wire
from kimi_cli.wire.types import TextPart, TurnBegin, TurnEnd

CORE_COMMANDS = {
    "init",
    "compact",
    "prune",
    "clear",
    "yolo",
    "afk",
    "add-dir",
    "export",
    "refresh-env",
    "import",
}


def _make_soul(runtime: Runtime, tmp_path: Path) -> KimiSoul:
    agent = Agent(
        name="Test Agent",
        system_prompt="Test system prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    return KimiSoul(agent, context=Context(file_backend=tmp_path / "history.jsonl"))


def _collecting_ui_loop(sent: list) -> Callable[[Wire], Coroutine[Any, Any, None]]:
    """A ui_loop_fn that records every wire message until the wire shuts down."""

    async def _ui_loop_fn(wire: Wire) -> None:
        ui = wire.ui_side(merge=False)
        with contextlib.suppress(QueueShutDown):
            while True:
                sent.append(await ui.receive())

    return _ui_loop_fn


def test_available_slash_commands_lists_core_commands(
    runtime: Runtime, tmp_path: Path
) -> None:
    soul = _make_soul(runtime, tmp_path)

    command_names = {cmd.name for cmd in soul.available_slash_commands}
    assert CORE_COMMANDS <= command_names

    clear = next(cmd for cmd in soul.available_slash_commands if cmd.name == "clear")
    assert clear.aliases == ("reset",)
    assert "Clear the context" in clear.description


def test_find_command_resolves_aliases() -> None:
    assert soul_slash.find_command("clear") is soul_slash.COMMANDS["clear"]
    assert soul_slash.find_command("reset") is soul_slash.COMMANDS["clear"]
    assert soul_slash.find_command("no-such-command") is None


@pytest.mark.asyncio
async def test_clear_command_clears_context(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    soul = _make_soul(runtime, tmp_path)
    monkeypatch.setattr(soul_slash, "wire_send", lambda _msg: None)
    await soul.context.append_message(Message(role="user", content="hi"))
    assert len(soul.context.history) > 0

    command = soul_slash.find_command("clear")
    assert command is not None
    await command(soul, "")

    assert len(soul.context.history) == 0


@pytest.mark.asyncio
async def test_run_soul_dispatches_slash_command_with_turn_framing(
    runtime: Runtime, tmp_path: Path
) -> None:
    """A slash input is dispatched by run_soul (not sent to the LLM) and is
    framed by the same TurnBegin/TurnEnd pair a natural-language turn emits."""
    soul = _make_soul(runtime, tmp_path)
    await soul.context.append_message(Message(role="user", content="hi"))
    sent: list = []

    await run_soul(
        soul,
        "/clear",
        _collecting_ui_loop(sent),
        asyncio.Event(),
        runtime=runtime,
    )

    assert len(soul.context.history) == 0
    assert TurnBegin(user_input="/clear") in sent
    assert TurnEnd() in sent
    texts = [m.text for m in sent if isinstance(m, TextPart)]
    assert "The context has been cleared." in texts
    # Slash commands must not auto-generate a session title.
    assert runtime.session.state.custom_title is None


@pytest.mark.asyncio
async def test_unknown_slash_command_reports_without_turn(
    runtime: Runtime, tmp_path: Path
) -> None:
    soul = _make_soul(runtime, tmp_path)
    sent: list = []

    await run_soul(
        soul,
        "/no-such-command",
        _collecting_ui_loop(sent),
        asyncio.Event(),
        runtime=runtime,
    )

    texts = [m.text for m in sent if isinstance(m, TextPart)]
    assert any('Unknown slash command "/no-such-command"' in t for t in texts)
    assert runtime.session.state.custom_title is None
