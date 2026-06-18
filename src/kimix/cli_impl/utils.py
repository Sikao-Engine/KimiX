from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from kimix.base import Color, colorful_text

_READLINE_MOD: Any = None
_READLINE_ATTEMPTED: bool = False


def _get_slash_commands() -> set[str]:
    """Lazily get slash command names to avoid circular import at module level."""
    from .commands import _command_map_keys
    return _command_map_keys


def _ensure_readline() -> Any:
    """Lazily import a readline implementation."""
    global _READLINE_MOD, _READLINE_ATTEMPTED
    if _READLINE_ATTEMPTED:
        return _READLINE_MOD
    _READLINE_ATTEMPTED = True
    for candidate in ("readline", "pyreadline3", "pyreadline"):
        try:
            mod = __import__(candidate)
        except ImportError:
            continue
        # The module must expose the standard readline API used below.
        required = ("set_completer", "set_completer_delims", "parse_and_bind", "get_line_buffer")
        if all(hasattr(mod, name) for name in required):
            _READLINE_MOD = mod
            return _READLINE_MOD
    return None


def _get_command_arg_types() -> dict[str, str]:
    """Lazily load command argument completion categories."""
    from .commands import _command_arg_types
    return _command_arg_types


def _complete_paths(prefix: str, *, directories_only: bool = False) -> list[str]:
    """Return path completion candidates for prefix."""
    expanded = os.path.expanduser(prefix) if prefix.startswith("~") else prefix
    p = Path(expanded)
    if not p.is_absolute():
        p = Path.cwd() / p
    base = p if p.exists() and p.is_dir() else p.parent
    name_prefix = "" if p.exists() and p.is_dir() else p.name
    try:
        entries = sorted(base.iterdir())
    except (PermissionError, OSError):
        return []
    candidates: list[str] = []
    for entry in entries:
        if directories_only and not entry.is_dir():
            continue
        if entry.name.startswith(name_prefix):
            suffix = os.sep if entry.is_dir() else ""
            candidate = str(base / (entry.name + suffix))
            candidates.append(candidate)
    return candidates


def _get_completion_candidates(line: str) -> list[str]:
    """Pure logic: given the current line, return candidate completions."""
    line = line.lstrip("\ufeff").strip()
    if not line.startswith("/"):
        return []
    body = line[1:]
    if not body:
        return [f"/{cmd}" for cmd in sorted(_get_slash_commands())]
    if ":" in body:
        cmd_name, arg_prefix = body.split(":", 1)
        if cmd_name not in _get_slash_commands():
            return []
        arg_type = _get_command_arg_types().get(cmd_name)
        if arg_type == "dir":
            return [
                f"/{cmd_name}:{p}"
                for p in _complete_paths(arg_prefix, directories_only=True)
            ]
        if arg_type == "file":
            return [f"/{cmd_name}:{p}" for p in _complete_paths(arg_prefix)]
        if arg_type == "bool_on_off":
            return [
                f"/{cmd_name}:{v}"
                for v in ("on", "off")
                if v.startswith(arg_prefix)
            ]
        if arg_type == "ralph":
            values = ["on", "off"] + [str(i) for i in range(11)]
            return [
                f"/{cmd_name}:{v}"
                for v in values
                if v.startswith(arg_prefix)
            ]
        return []
    return [f"/{cmd}" for cmd in sorted(_get_slash_commands()) if cmd.startswith(body)]


class _CommandCompleter:
    def __init__(self) -> None:
        self._candidates: list[str] = []

    def complete(self, text: str, state: int) -> str | None:
        if state == 0:
            rl = _ensure_readline()
            line = ""
            if rl is not None and hasattr(rl, "get_line_buffer"):
                line = rl.get_line_buffer()
            self._candidates = _get_completion_candidates(line)
        try:
            return self._candidates[state]
        except IndexError:
            return None


_COMMAND_COMPLETER = _CommandCompleter()

def _setup_readline_completion() -> bool:
    """Enable command completion. Returns True on success."""
    rl = _ensure_readline()
    if rl is None:
        return False
    if hasattr(rl, "set_completer"):
        rl.set_completer(_COMMAND_COMPLETER.complete)
    if hasattr(rl, "set_completer_delims"):
        rl.set_completer_delims(" \t\n")
    if hasattr(rl, "parse_and_bind"):
        rl.parse_and_bind("tab: complete")
    if hasattr(rl, "set_auto_history"):
        rl.set_auto_history(True)
    return True


def _disable_readline_completion() -> tuple[Any, bool] | None:
    """Disable command completion and auto-history; return previous state for restore."""
    rl = _ensure_readline()
    if rl is None:
        return None
    prev_completer = rl.get_completer() if hasattr(rl, "get_completer") else None
    prev_auto_history = True
    if hasattr(rl, "set_auto_history"):
        returned = rl.set_auto_history(False)
        if returned is not None:
            prev_auto_history = bool(returned)
    if hasattr(rl, "set_completer"):
        rl.set_completer(None)
    return prev_completer, prev_auto_history


def _restore_readline_completion(state: tuple[Any, bool] | None) -> None:
    """Restore completer and auto-history to the previous state."""
    if state is None:
        return
    rl = _ensure_readline()
    if rl is None:
        return
    prev_completer, prev_auto_history = state
    if hasattr(rl, "set_completer"):
        rl.set_completer(prev_completer)
    if hasattr(rl, "set_auto_history"):
        rl.set_auto_history(prev_auto_history)


def _input(
    text: str,
    text_arr: list[str],
    multi_line_mode: bool = False,
    *,
    use_completion: bool = False,
) -> str:
    if text_arr is None or len(text_arr) == 0:
        if use_completion:
            _setup_readline_completion()
        else:
            restore_state = _disable_readline_completion()
            try:
                return input(text)
            finally:
                _restore_readline_completion(restore_state)
        return input(text)
    return text_arr.pop(0)


def _split_text(lines: list[str], command_map: set[str] | None = None) -> list[str]:
    text_arr: list[str] = []
    current_text: list[str] = []
    for line in lines:
        strip_line = line.strip()
        if len(strip_line) == 0:
            current_text.append('')
            continue
        if strip_line.startswith('/'):
            if len(strip_line) > 1:
                cmd = strip_line[1:].split()[0]
                if command_map is not None and cmd not in command_map:
                    current_text.append(line)
                    continue
            if current_text:
                text_arr.append('\n'.join(current_text))
                current_text = []
            if len(strip_line) > 1:
                text_arr.append(strip_line)
        else:
            current_text.append(line)
    if current_text:
        text_arr.append('\n'.join(current_text))
    return text_arr
