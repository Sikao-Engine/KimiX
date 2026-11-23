"""Session fork utilities.

Provides turn enumeration, wire/context truncation, and session forking
for both the Web API and CLI slash commands (/undo, /fork).
"""

from __future__ import annotations

import mimetypes
import regex as re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import orjson

from kimi_cli.session_state import load_session_state, save_session_state

CHECKPOINT_USER_PATTERN = re.compile(r"^<system>CHECKPOINT \d+</system>$")


@dataclass(frozen=True, slots=True)
class TurnInfo:
    """Summary of a single turn for display in the selector."""

    index: int
    """0-based turn index."""
    user_text: str
    """First-line text of the user message."""


# ---------------------------------------------------------------------------
# Turn enumeration
# ---------------------------------------------------------------------------


def enumerate_turns(wire_path: Path) -> list[TurnInfo]:
    """Scan wire.jsonl and return a list of all turns with user message text.

    Each turn is identified by a ``TurnBegin`` record, whose ``user_input``
    field provides the user message text.
    """
    if not wire_path.exists():
        return []

    turns: list[TurnInfo] = []
    current_turn = -1

    with open(wire_path, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue

            try:
                record: dict[str, Any] = orjson.loads(stripped)
            except orjson.JSONDecodeError:
                continue

            if record.get("type") == "metadata":
                continue

            message: dict[str, Any] = record.get("message", {})
            msg_type: str | None = message.get("type")

            if msg_type == "TurnBegin":
                current_turn += 1
                user_input = message.get("payload", {}).get("user_input", "")
                text = _extract_user_text(user_input)
                turns.append(TurnInfo(index=current_turn, user_text=text))

    return turns


def _extract_user_text(user_input: str | list[Any]) -> str:
    """Extract plain text from a TurnBegin user_input (string or content parts)."""
    if isinstance(user_input, str):
        return user_input

    parts: list[str] = []
    for part in user_input:
        if isinstance(part, dict):
            part_dict = cast(dict[str, Any], part)
            text = part_dict.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif isinstance(part, str):
            parts.append(part)
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Wire / context truncation (extracted from web/api/sessions.py)
# ---------------------------------------------------------------------------


def truncate_wire_at_turn(wire_path: Path, turn_index: int) -> list[str]:
    """Read wire.jsonl and return all lines up to and including the given turn.

    Args:
        wire_path: Path to the wire.jsonl file
        turn_index: 0-based turn index. Returns turns 0..turn_index inclusive.

    Returns:
        List of raw JSON lines (including the metadata header)

    Raises:
        ValueError: If turn_index is out of range
    """
    if not wire_path.exists():
        raise ValueError("wire.jsonl not found")

    lines: list[str] = []
    current_turn = -1  # Will become 0 on first TurnBegin

    with open(wire_path, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue

            try:
                record: dict[str, Any] = orjson.loads(stripped)
            except orjson.JSONDecodeError:
                continue

            # Always keep metadata header
            if record.get("type") == "metadata":
                lines.append(stripped)
                continue

            message: dict[str, Any] = record.get("message", {})
            msg_type: str | None = message.get("type")

            if msg_type == "TurnBegin":
                current_turn += 1
                if current_turn > turn_index:
                    break

            if current_turn <= turn_index:
                lines.append(stripped)

            # Stop after the TurnEnd of the target turn
            if msg_type == "TurnEnd" and current_turn == turn_index:
                break

    if current_turn < turn_index:
        raise ValueError(f"turn_index {turn_index} out of range (max turn: {current_turn})")

    return lines


def _is_checkpoint_user_message(record: dict[str, Any]) -> bool:
    """Whether a context line is the synthetic user checkpoint marker."""
    if record.get("role") != "user":
        return False

    content = record.get("content")
    if isinstance(content, str):
        return CHECKPOINT_USER_PATTERN.fullmatch(content.strip()) is not None

    parts = cast(list[Any], content) if isinstance(content, list) else []
    if len(parts) == 1 and isinstance(parts[0], dict):
        first_part = cast(dict[str, Any], parts[0])
        text = first_part.get("text")
        if isinstance(text, str):
            return CHECKPOINT_USER_PATTERN.fullmatch(text.strip()) is not None

    return False


def _valid_context_records(context_path: Path) -> list[dict[str, Any]]:
    from kimi_cli.soul.jsonl_io import read_jsonl_records_for_copy

    return read_jsonl_records_for_copy(context_path)


def _active_context_records(context_path: Path, message_limit: int | None = None) -> list[dict[str, Any]]:
    """Canonicalize the replayed active view, optionally limiting messages.

    Usage/checkpoint order at a shared message boundary is reconstructed from
    each checkpoint's usage count, so later reverts retain the same pending and
    token state.  This is administrative migration/fork output, not a runtime
    rewrite of the source log.
    """
    from kimi_cli.soul.context import ContextReplayState, replay_context
    from kimi_cli.soul.context import message_record

    state: ContextReplayState = replay_context(context_path)
    cutoff = len(state.entries) if message_limit is None else min(message_limit, len(state.entries))
    checkpoints = [
        (checkpoint_id, checkpoint)
        for checkpoint_id, checkpoint in state.checkpoints.items()
        if checkpoint.message_count <= cutoff
    ]
    usages = [
        (index, usage)
        for index, usage in enumerate(state.usages)
        if usage.message_count <= cutoff
    ]
    # (message position, usage-order constraint, kind, tie-breaker, record)
    events: list[tuple[int, int, int, int, dict[str, Any]]] = []
    current_prompt = state.system_prompt
    if checkpoints:
        current_prompt = checkpoints[0][1].system_prompt
    records: list[dict[str, Any]] = []
    if current_prompt is not None:
        records.append({"role": "_system_prompt", "content": current_prompt})
    for checkpoint_id, checkpoint in checkpoints:
        if checkpoint.system_prompt != current_prompt:
            current_prompt = checkpoint.system_prompt
            if current_prompt is not None:
                events.append(
                    (
                        checkpoint.message_count,
                        checkpoint.usage_count,
                        -1,
                        checkpoint_id,
                        {"role": "_system_prompt", "content": current_prompt},
                    )
                )
        events.append(
            (
                checkpoint.message_count,
                checkpoint.usage_count,
                0,
                checkpoint_id,
                {
                    "role": "_checkpoint",
                    "id": checkpoint_id,
                    "message_count": checkpoint.message_count,
                },
            )
        )
    for index, usage in usages:
        events.append(
            (
                usage.message_count,
                index,
                1,
                index,
                {
                    "role": "_usage",
                    "token_count": usage.token_count,
                    "message_count": usage.message_count,
                },
            )
        )
    events.sort(key=lambda event: event[:4])
    event_index = 0
    for position, entry in enumerate(state.entries[:cutoff]):
        while event_index < len(events) and events[event_index][0] == position:
            records.append(events[event_index][4])
            event_index += 1
        records.append(message_record(entry.message, created_at=entry.created_at))
    while event_index < len(events):
        records.append(events[event_index][4])
        event_index += 1
    if state.system_prompt != current_prompt and state.system_prompt is not None:
        records.append({"role": "_system_prompt", "content": state.system_prompt})
    return records


def truncate_context_at_turn(context_path: Path, turn_index: int) -> list[str]:
    """Return dense active records through the requested real user turn.

    Resets/reverts are replayed before turn enumeration.  Synthetic checkpoint
    user messages do not count as turns.  As before, requesting beyond the
    available context turns returns the complete active view.
    """
    if not context_path.exists():
        return []
    from kimi_cli.soul.context import replay_context

    state = replay_context(context_path)
    current_turn = -1
    cutoff = len(state.entries)
    for index, entry in enumerate(state.entries):
        record = entry.message.model_dump(mode="json", exclude_none=True)
        if entry.message.role == "user" and not _is_checkpoint_user_message(record):
            current_turn += 1
            if current_turn > turn_index:
                cutoff = index
                break
    records = _active_context_records(context_path, message_limit=cutoff)
    return [orjson.dumps(record).decode("utf-8") for record in records]


# ---------------------------------------------------------------------------
# Full fork operation
# ---------------------------------------------------------------------------


async def fork_session(
    source_session_dir: Path,
    work_dir: Any,  # KaosPath
    turn_index: int | None = None,
    title_prefix: str = "Fork",
    source_title: str | None = None,
) -> str:
    """Fork a session, creating a new session with history up to the given turn.

    Args:
        source_session_dir: Path to the source session directory.
        work_dir: The KaosPath work directory.
        turn_index: 0-based turn index (inclusive). If None, copy all turns.
        title_prefix: Prefix for the forked session title.
        source_title: Title of the source session (used for the fork title).

    Returns:
        The new session ID.

    Raises:
        ValueError: If turn_index is out of range.
    """
    from kimi_cli.session import Session as KimiCLISession

    wire_path = source_session_dir / "wire.jsonl"
    context_path = source_session_dir / "context.jsonl"

    if turn_index is not None:
        truncated_wire_lines = truncate_wire_at_turn(wire_path, turn_index)
        truncated_context_lines = truncate_context_at_turn(context_path, turn_index)
    else:
        # Copy all content
        truncated_wire_lines = _read_all_lines(wire_path)
        truncated_context_lines = _read_all_content(context_path)

    new_session = await KimiCLISession.create(work_dir=work_dir)
    new_session_dir = new_session.dir

    # Copy referenced video files
    _copy_referenced_videos(source_session_dir, new_session_dir, truncated_wire_lines)

    # Write truncated wire.jsonl
    new_wire_path = new_session_dir / "wire.jsonl"
    with open(new_wire_path, "w", encoding="utf-8") as f:
        for line in truncated_wire_lines:
            f.write(line + "\n")

    new_context_path = new_session.context_file
    with new_context_path.open("wb") as handle:
        for line in truncated_context_lines:
            handle.write(orjson.dumps(orjson.loads(line), option=orjson.OPT_APPEND_NEWLINE))

    # A fork is not complete until the new active view can be restored.
    from kimi_cli.soul.context import Context

    validated = Context.from_file_backend(new_context_path)
    await validated.restore()
    await validated.close()

    # Set title
    if source_title is None:
        src_state = load_session_state(source_session_dir)
        source_title = src_state.custom_title or "Untitled"

    fork_title = f"{title_prefix}: {source_title}"

    new_state = load_session_state(new_session_dir)
    new_state.custom_title = fork_title
    new_state.title_generated = True
    new_state.wire_mtime = new_wire_path.stat().st_mtime
    save_session_state(new_state, new_session_dir)

    return new_session.id


def _read_all_lines(path: Path) -> list[str]:
    """Read all non-empty lines from a text file."""
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _read_all_content(path: Path) -> list[str]:
    """Copy every valid JSONL record densely; never read legacy DBs here."""
    if not path.exists():
        return []
    return [orjson.dumps(record).decode("utf-8") for record in _valid_context_records(path)]


def _copy_referenced_videos(
    source_dir: Path,
    new_session_dir: Path,
    wire_lines: list[str],
) -> None:
    """Copy video files referenced in the wire lines to the new session."""
    source_uploads = source_dir / "uploads"
    if not source_uploads.is_dir():
        return

    referenced_videos: set[str] = set()
    for line in wire_lines:
        for match in re.finditer(r"uploads/([^\"\\<>\s]+)", line):
            fname = match.group(1)
            mime, _ = mimetypes.guess_type(fname)
            if mime and mime.startswith("video/"):
                referenced_videos.add(fname)

    files_to_copy = [
        source_uploads / name for name in referenced_videos if (source_uploads / name).is_file()
    ]
    if files_to_copy:
        new_uploads = new_session_dir / "uploads"
        new_uploads.mkdir(parents=True, exist_ok=True)
        copied_names: list[str] = []
        for vf in files_to_copy:
            shutil.copy2(vf, new_uploads / vf.name)
            copied_names.append(vf.name)
        (new_uploads / ".sent").write_text(orjson.dumps(copied_names).decode(), encoding="utf-8")
