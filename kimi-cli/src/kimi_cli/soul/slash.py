"""Built-in soul slash commands.

Core session commands (context management, approval modes, workspace dirs,
export/import) implemented as plain functions over a static dispatch table.
``KimiSoul.run`` parses a leading ``/name [args]`` token and dispatches here;
wire/ACP clients discover the available commands via :func:`list_command_infos`.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import regex as re
from kaos.path import KaosPath
from kosong.message import Message

import kimi_cli.prompts as prompts
from kimi_cli import logger
from kimi_cli.soul import wire_send
from kimi_cli.soul.agent import load_agents_md
from kimi_cli.soul.compaction import ManualCompactionError
from kimi_cli.soul.context import Context
from kimi_cli.soul.message import system
from kimi_cli.utils.export import is_sensitive_file
from kimi_cli.utils.path import sanitize_cli_path, shorten_home
from kimi_cli.wire.types import StatusUpdate, TextPart

if TYPE_CHECKING:
    from kimi_cli.soul.kimisoul import KimiSoul

type SoulSlashCmdFunc = Callable[[KimiSoul, str], None | Awaitable[None]]
"""
A function that runs as a KimiSoul-level slash command.

Raises:
    Any exception that can be raised by `Soul.run`.
"""

_COMMAND_NAME_RE = re.compile(r"^\/([a-zA-Z0-9_-]+)")


@dataclass(frozen=True, slots=True)
class SlashCommandInfo:
    """Public description of a soul slash command (for wire/ACP clients)."""

    name: str
    description: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SlashCommandCall:
    name: str
    args: str
    raw_input: str


def parse_slash_command_call(user_input: str) -> SlashCommandCall | None:
    """
    Parse a slash command call from user input.

    Returns:
        SlashCommandCall if a slash command is found, else None. The `args` field contains
        the raw argument string after the command name.
    """
    user_input = user_input.strip()
    if not user_input or not user_input.startswith("/"):
        return None

    name_match = _COMMAND_NAME_RE.match(user_input)

    if not name_match:
        return None

    command_name = name_match.group(1)
    if len(user_input) > name_match.end() and not user_input[name_match.end()].isspace():
        return None
    raw_args = user_input[name_match.end() :].lstrip()
    return SlashCommandCall(name=command_name, args=raw_args, raw_input=user_input)


async def cmd_init(soul: KimiSoul, args: str):
    """Analyze the codebase and generate an `AGENTS.md` file"""
    from kimi_cli.soul.kimisoul import KimiSoul

    with tempfile.TemporaryDirectory() as temp_dir:
        tmp_context = Context(file_backend=Path(temp_dir) / "context.db")
        tmp_soul = KimiSoul(soul.agent, context=tmp_context)
        await tmp_soul.run(prompts.INIT)

    agents_md = await load_agents_md(soul.runtime.builtin_args.KIMI_WORK_DIR)
    system_message = system(
        "The user just ran `/init` slash command. "
        "The system has analyzed the codebase and generated an `AGENTS.md` file. "
        f"Latest AGENTS.md file content:\n{agents_md}"
    )
    await soul.context.append_message(Message(role="user", content=[system_message]))


async def cmd_compact(soul: KimiSoul, args: str):
    """Compact the context (optionally with a custom focus, e.g. /compact keep db discussions)"""
    if soul.context.n_checkpoints == 0:
        wire_send(TextPart(text="The context is empty."))
        return

    logger.info("Running `/compact`")
    instruction = args.strip()
    try:
        await soul.compact_context(manual=True, custom_instruction=instruction)
    except ManualCompactionError as e:
        # Phase 3 (§5.3): map classified manual-compaction failures to
        # user-facing messages instead of dumping a raw traceback.
        messages = {
            "busy": "Compaction already in progress.",
            "cancelled": "Compaction cancelled.",
            "changed": "History changed during compaction; try again.",
            "summary": "Summary was not smaller than the compacted content.",
            "commit": "Compaction did not commit cleanly.",
            "persistence": "Compaction did not persist cleanly.",
        }
        wire_send(TextPart(text=f"/compact failed: {messages.get(e.code, str(e))}"))
        return
    wire_send(TextPart(text="The context has been compacted."))
    snap = soul.status
    wire_send(
        StatusUpdate(
            context_usage=snap.context_usage,
            context_tokens=snap.context_tokens,
            max_context_tokens=snap.max_context_tokens,
        )
    )


async def cmd_prune(soul: KimiSoul, args: str):
    """Manually trigger context pruning (smart history removal)"""
    logger.info("Running `/prune`")

    if not soul.runtime.config.loop_control.context_pruning_enabled:
        wire_send(TextPart(text="Context pruning is disabled in config."))
        return

    llm = soul.runtime.llm
    max_context = llm.max_context_size if llm else 128_000
    model_name = llm.chat_provider.model_name if llm else None

    # Copy the history and run the pruner
    history = list(soul.context.history)
    result = soul.pruner.prune(
        history,
        current_step=soul.current_step_no,
        context_usage=soul.status.context_usage,
        max_context_size=max_context,
        model=model_name,
    )

    if result.earliest_removed_index is None:
        wire_send(TextPart(text="No prunable content found."))
        return

    wire_send(
        TextPart(
            text=f"Context pruned: freed {result.freed_tokens} tokens, "
            f"earliest change at index {result.earliest_removed_index}."
        )
    )
    snap = soul.status
    wire_send(
        StatusUpdate(
            context_usage=snap.context_usage,
            context_tokens=snap.context_tokens,
            max_context_tokens=snap.max_context_tokens,
        )
    )


async def cmd_clear(soul: KimiSoul, args: str):
    """Clear the context"""
    logger.info("Running `/clear`")
    await soul.context.clear()
    await soul.context.write_system_prompt(soul.agent.get_system_prompt())
    wire_send(TextPart(text="The context has been cleared."))
    snap = soul.status
    wire_send(
        StatusUpdate(
            context_usage=snap.context_usage,
            context_tokens=snap.context_tokens,
            max_context_tokens=snap.max_context_tokens,
        )
    )


async def cmd_yolo(soul: KimiSoul, args: str):
    """Toggle YOLO mode (auto-approve all actions)"""

    # Inspect only the yolo flag: afk is independent and is toggled by /afk.
    if soul.runtime.approval.is_yolo_flag():
        soul.runtime.approval.set_yolo(False)
        if soul.runtime.approval.is_afk():
            # Yolo off but afk still on -> tool calls remain auto-approved.
            # Don't mislead the user into thinking approvals just came back.
            wire_send(
                TextPart(
                    text=(
                        "Yolo disabled, but afk is still on — tool calls remain "
                        "auto-approved. Use /afk to turn off afk."
                    )
                )
            )
        else:
            wire_send(TextPart(text="You only die once! Actions will require approval."))
    else:
        soul.runtime.approval.set_yolo(True)
        wire_send(TextPart(text="You only live once! All actions will be auto-approved."))


async def cmd_afk(soul: KimiSoul, args: str):
    """Toggle afk mode (auto-dismiss AskUserQuestion, auto-approve tool calls)"""

    if soul.runtime.approval.is_afk():
        soul.runtime.approval.set_afk(False)
        await soul.notify_afk_changed(False)
        if soul.runtime.approval.is_yolo_flag():
            wire_send(
                TextPart(
                    text=("afk mode disabled. You are back at the terminal. Yolo is still on.")
                )
            )
        else:
            wire_send(TextPart(text="afk mode disabled. You are back at the terminal."))
    else:
        soul.runtime.approval.set_afk(True)
        await soul.notify_afk_changed(True)
        wire_send(
            TextPart(
                text=(
                    "afk mode enabled. AskUserQuestion will be auto-dismissed "
                    "and tool calls auto-approved."
                )
            )
        )


async def cmd_add_dir(soul: KimiSoul, args: str):
    """Add a directory to the workspace. Usage: /add-dir <path>. Run without args to list added dirs"""  # noqa: E501
    from kaos.path import KaosPath

    from kimi_cli.utils.path import is_within_directory, list_directory

    args = sanitize_cli_path(args)
    if not args:
        if not soul.runtime.additional_dirs:
            wire_send(TextPart(text="No additional directories. Usage: /add-dir <path>"))
        else:
            lines = ["Additional directories:"]
            for d in soul.runtime.additional_dirs:
                lines.append(f"  - {d}")
            wire_send(TextPart(text="\n".join(lines)))
        return

    path = KaosPath(args).expanduser().canonical()

    if not await path.exists():
        wire_send(TextPart(text=f"Directory does not exist: {path}"))
        return
    if not await path.is_dir():
        wire_send(TextPart(text=f"Not a directory: {path}"))
        return

    # Check if already added (exact match)
    if path in soul.runtime.additional_dirs:
        wire_send(TextPart(text=f"Directory already in workspace: {path}"))
        return

    # Check if it's within the work_dir (already accessible)
    work_dir = soul.runtime.builtin_args.KIMI_WORK_DIR
    if is_within_directory(path, work_dir):
        wire_send(TextPart(text=f"Directory is already within the working directory: {path}"))
        return

    # Check if it's within an already-added additional directory (redundant)
    for existing in soul.runtime.additional_dirs:
        if is_within_directory(path, existing):
            wire_send(
                TextPart(
                    text=f"Directory is already within an added directory `{existing}`: {path}"
                )
            )
            return

    # Validate readability before committing any state changes
    try:
        ls_output = await list_directory(path)
    except OSError as e:
        wire_send(TextPart(text=f"Cannot read directory: {path} ({e})"))
        return

    # Add the directory (only after readability is confirmed)
    soul.runtime.additional_dirs.append(path)

    # Persist to session state
    soul.runtime.session.state.additional_dirs.append(str(path))
    soul.runtime.session.save_state()

    # Inject a system message to inform the LLM about the new directory
    system_message = system(
        f"The user has added an additional directory to the workspace: `{path}`\n\n"
        f"Directory listing:\n```\n{ls_output}\n```\n\n"
        "You can now read, write, search, and glob files in this directory "
        "as if it were part of the working directory."
    )
    await soul.context.append_message(Message(role="user", content=[system_message]))

    wire_send(TextPart(text=f"Added directory to workspace: {path}"))
    logger.info("Added additional directory: {path}", path=path)


async def cmd_export(soul: KimiSoul, args: str):
    """Export current session context to a markdown file"""
    from kimi_cli.utils.export import perform_export

    session = soul.runtime.session
    result = await perform_export(
        history=list(soul.context.history),
        session_id=session.id,
        work_dir=str(session.work_dir),
        token_count=soul.context.token_count,
        args=args,
        default_dir=Path(str(session.work_dir)),
    )
    if isinstance(result, str):
        wire_send(TextPart(text=result))
        return
    output, count = result
    display = shorten_home(KaosPath(str(output)))
    wire_send(TextPart(text=f"Exported {count} messages to {display}"))
    wire_send(
        TextPart(
            text="  Note: The exported file may contain sensitive information. "
            "Please be cautious when sharing it externally."
        )
    )


async def cmd_refresh_env(soul: KimiSoul, args: str):
    """Refresh PATH/PATHEXT from the Windows registry (no restart required)"""
    import platform

    if platform.system() != "Windows":
        wire_send(TextPart(text="This command is only available on Windows."))
        return

    from kimi_cli.utils.environment import refresh_windows_env

    await asyncio.to_thread(refresh_windows_env)
    wire_send(TextPart(text="PATH and PATHEXT have been refreshed from the registry."))


async def cmd_import(soul: KimiSoul, args: str):
    """Import context from a file or session ID"""
    from kimi_cli.utils.export import perform_import

    target = sanitize_cli_path(args)
    if not target:
        wire_send(TextPart(text="Usage: /import <file_path or session_id>"))
        return

    session = soul.runtime.session
    raw_max_context_size = (
        soul.runtime.llm.max_context_size if soul.runtime.llm is not None else None
    )
    max_context_size = (
        raw_max_context_size
        if isinstance(raw_max_context_size, int) and raw_max_context_size > 0
        else None
    )
    result = await perform_import(
        target=target,
        current_session_id=session.id,
        work_dir=session.work_dir,
        context=soul.context,
        max_context_size=max_context_size,
    )
    if isinstance(result, str):
        wire_send(TextPart(text=result))
        return

    source_desc, content_len = result
    wire_send(TextPart(text=f"Imported context from {source_desc} ({content_len} chars)."))
    if source_desc.startswith("file") and is_sensitive_file(Path(target).name):
        wire_send(
            TextPart(
                text="Warning: This file may contain secrets (API keys, tokens, credentials). "
                "The content is now part of your session context."
            )
        )


COMMANDS: dict[str, SoulSlashCmdFunc] = {
    "init": cmd_init,
    "compact": cmd_compact,
    "prune": cmd_prune,
    "clear": cmd_clear,
    "yolo": cmd_yolo,
    "afk": cmd_afk,
    "add-dir": cmd_add_dir,
    "export": cmd_export,
    "refresh-env": cmd_refresh_env,
    "import": cmd_import,
}
"""Primary command name -> handler."""

ALIASES: dict[str, str] = {
    "reset": "clear",
}
"""Alias -> primary command name."""


def find_command(name: str) -> SoulSlashCmdFunc | None:
    """Resolve a (possibly aliased) command name to its handler."""
    return COMMANDS.get(ALIASES.get(name, name))


def list_command_infos() -> list[SlashCommandInfo]:
    """Public descriptions of all soul slash commands (for wire/ACP clients)."""
    alias_map: dict[str, list[str]] = {}
    for alias, canonical in ALIASES.items():
        alias_map.setdefault(canonical, []).append(alias)
    return [
        SlashCommandInfo(
            name=name,
            description=(func.__doc__ or "").strip(),
            aliases=tuple(alias_map.get(name, ())),
        )
        for name, func in COMMANDS.items()
    ]
