from __future__ import annotations

from pathlib import Path

import pytest
from kosong.message import Message
from kosong.tooling.empty import EmptyToolset

import kimi_cli.soul.kimisoul as kimisoul_module
import kimi_cli.soul.slash as soul_slash
from kimi_cli.soul.agent import Agent, Runtime
from kimi_cli.soul.context import Context
from kimi_cli.soul.kimisoul import KimiSoul
from kimi_cli.wire.types import TextPart

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
    monkeypatch.setattr(kimisoul_module, "wire_send", lambda _msg: None)
    monkeypatch.setattr(soul_slash, "wire_send", lambda _msg: None)
    await soul.context.append_message(Message(role="user", content="hi"))
    assert len(soul.context.history) > 0

    await soul.run("/clear")

    assert soul.context.n_checkpoints == 0
    assert runtime.session.state.custom_title is None


@pytest.mark.asyncio
async def test_unknown_slash_command_reports_without_turn(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    soul = _make_soul(runtime, tmp_path)
    sent: list = []
    monkeypatch.setattr(kimisoul_module, "wire_send", sent.append)

    await soul.run("/no-such-command")

    texts = [m.text for m in sent if isinstance(m, TextPart)]
    assert any('Unknown slash command "/no-such-command"' in t for t in texts)
    assert soul.context.n_checkpoints == 0
    assert runtime.session.state.custom_title is None


@pytest.mark.asyncio
async def test_slash_run_does_not_auto_generate_session_title(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    soul = _make_soul(runtime, tmp_path)
    monkeypatch.setattr(kimisoul_module, "wire_send", lambda _msg: None)
    monkeypatch.setattr(soul_slash, "wire_send", lambda _msg: None)

    await soul.run("/clear")

    assert runtime.session.state.custom_title is None
