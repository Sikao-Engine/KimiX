from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import orjson
from kaos.path import KaosPath
from kimi_agent_sdk import Session

from kimi_cli.metadata import load_metadata

_default_session: Session | None = None
_default_role: Any = None

_should_print_usage = threading.local()
_should_print_usage.value = True

_cli_sessions: dict[str, dict[str, Any]] = {}
"""In-memory session cache: session_id -> {title, updated_at, context_usage, context_tokens}.

Populated at CLI startup via a lightweight filesystem scan (not CliSession.list)
and kept up-to-date whenever sessions are created, resumed, or loaded.
context_usage (float 0.0-1.0 or -1.0 if unknown) and context_tokens (int or 0)
are filled in when the session is loaded.
"""


def _add_cli_session(
    session_id: str,
    title: str,
    updated_at: float,
    context_usage: float = -1.0,
    context_tokens: int = 0,
) -> None:
    """Insert/update an entry in _cli_sessions.

    Args:
        session_id: The session ID.
        title: The session title.
        updated_at: Unix timestamp of last update.
        context_usage: Context usage ratio (0.0-1.0), or -1.0 if unknown.
        context_tokens: Number of tokens currently in the context.
    """
    _cli_sessions[session_id] = {
        "title": title,
        "updated_at": updated_at,
        "context_usage": context_usage,
        "context_tokens": context_tokens,
    }


def _remove_cli_session(session_id: str) -> None:
    """Remove an entry from _cli_sessions."""
    _cli_sessions.pop(session_id, None)


def _refresh_cli_sessions(work_dir: KaosPath) -> None:
    """Lightweight sync scan of sessions directory under work_dir.

    Lists subdirectories in the sessions directory, reads state.json for
    custom_title (fallback "Untitled"), and stats context.db / context.jsonl
    for updated_at. Does NOT call CliSession.list() — pure filesystem + JSON.
    """
    metadata = load_metadata()
    work_dir_meta = metadata.get_work_dir_meta(work_dir.canonical())
    if work_dir_meta is None:
        _cli_sessions.clear()
        return

    sessions_dir = work_dir_meta.sessions_dir
    _cli_sessions.clear()

    for path in sessions_dir.iterdir():
        if not path.is_dir():
            continue
        session_id = path.name

        # Read state.json for title
        title = "Untitled"
        state_file = path / "state.json"
        if state_file.exists():
            try:
                data = orjson.loads(state_file.read_bytes())
                custom_title = data.get("custom_title")
                if custom_title:
                    title = custom_title
            except Exception:
                pass

        # Get updated_at from context.db or context.jsonl mtime
        updated_at = 0.0
        db_file = path / "context.db"
        jsonl_file = path / "context.jsonl"
        if db_file.exists():
            updated_at = db_file.stat().st_mtime
        elif jsonl_file.exists():
            updated_at = jsonl_file.stat().st_mtime

        _add_cli_session(session_id, title, updated_at)
