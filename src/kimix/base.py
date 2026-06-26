from __future__ import annotations

import asyncio
import functools
import orjson
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from kimi_cli.wire.types import (
    ApprovalRequest,
    BackgroundTaskDisplayBlock,
    BriefDisplayBlock,
    CompactionBegin,
    DisplayBlock,
    UnknownDisplayBlock,
    CompactionEnd,
    DiffDisplayBlock,
    ShellDisplayBlock,
    StepBegin,
    StepInterrupted,
    TextPart,
    ThinkPart,
    TodoDisplayBlock,
    ToolCall,
    ToolCallPart,
    ToolResult,
)

if TYPE_CHECKING:
    from kimi_agent_sdk import Session

_threads: list[threading.Thread] = []
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
# 1a. Switch the Windows console code page to UTF-8 (CP 65001).
#     This ensures that child processes reading from the console
#     (including PowerShell's own [Console]::OutputEncoding when
#     not overridden) see UTF-8 rather than cp1252.  The
#     per-subprocess [Console]::OutputEncoding preamble is a
#     belt-and-suspenders complement; this system-level setting
#     catches everything else.
try:
    import ctypes
    _CP_UTF8 = 65001
    kernel32 = ctypes.windll.kernel32
    kernel32.SetConsoleCP(_CP_UTF8)
    kernel32.SetConsoleOutputCP(_CP_UTF8)
except Exception:
    # Non-fatal — the per-subprocess preamble still works.
    pass
class MessageType(Enum):
    """Message type for print_agent_json output function."""
    Text = "text"
    Thinking = "thinking"
    ToolCalling = "tool_calling"
    ToolCallingPart = "tool_calling_part"
    ToolResult = "tool_result"


class Color(Enum):
    """ANSI color codes for foreground colors."""
    BLACK = 30
    RED = 31
    GREEN = 32
    YELLOW = 33
    BLUE = 34
    MAGENTA = 35
    CYAN = 36
    WHITE = 37
    BRIGHT_BLACK = 90
    BRIGHT_RED = 91
    BRIGHT_GREEN = 92
    BRIGHT_YELLOW = 93
    BRIGHT_BLUE = 94
    BRIGHT_MAGENTA = 95
    BRIGHT_CYAN = 96
    BRIGHT_WHITE = 97


class BgColor(Enum):
    """ANSI color codes for background colors."""
    BLACK = 40
    RED = 41
    GREEN = 42
    YELLOW = 43
    BLUE = 44
    MAGENTA = 45
    CYAN = 46
    WHITE = 47
    BRIGHT_BLACK = 100
    BRIGHT_RED = 101
    BRIGHT_GREEN = 102
    BRIGHT_YELLOW = 103
    BRIGHT_BLUE = 104
    BRIGHT_MAGENTA = 105
    BRIGHT_CYAN = 106
    BRIGHT_WHITE = 107


class Style(Enum):
    """ANSI style codes."""
    RESET = 0
    BOLD = 1
    DIM = 2
    ITALIC = 3
    UNDERLINE = 4
    BLINK = 5
    REVERSE = 7
    HIDDEN = 8
    STRIKETHROUGH = 9


@dataclass(frozen=True)
class Color256:
    """256-color mode (8-bit) foreground color."""
    value: int


@dataclass(frozen=True)
class BgColor256:
    """256-color mode (8-bit) background color."""
    value: int


@dataclass(frozen=True)
class TrueColor:
    """24-bit true color (RGB) foreground."""
    r: int
    g: int
    b: int

    @classmethod
    def from_hex(cls, hex_color: str) -> "TrueColor":
        hex_color = hex_color.lstrip("#")
        return cls(
            int(hex_color[0:2], 16),
            int(hex_color[2:4], 16),
            int(hex_color[4:6], 16),
        )


@dataclass(frozen=True)
class BgTrueColor:
    """24-bit true color (RGB) background."""
    r: int
    g: int
    b: int

    @classmethod
    def from_hex(cls, hex_color: str) -> "BgTrueColor":
        hex_color = hex_color.lstrip("#")
        return cls(
            int(hex_color[0:2], 16),
            int(hex_color[2:4], 16),
            int(hex_color[4:6], 16),
        )


# Common 256-color grayscale colors (232-255)
GRAY_NEAR_BLACK = Color256(232)
GRAY_DARK = Color256(240)
GRAY = Color256(245)
GRAY_LIGHT = Color256(250)

# Common true color grayscale
TRUE_GRAY = TrueColor(128, 128, 128)


_ANSI_ESCAPE = re.compile(
    r"\x1B(?:"
    r"\][^\x07\x1B]*(?:\x07|\x1B\\)|"  # OSC sequences (BEL or ST terminated)
    r"[P^_][^\x07\x1B]*(?:\x07|\x1B\\)|"  # DCS / PM / APC sequences
    r"[@-Z\\-_]|"              # Single-character Fe sequences
    r"\[[0-?]*[ -/]*[@-~]"      # CSI sequences
    r")"
)


def _strip_ansi(text: str) -> str:
    if "\x1b" not in text:
        return text
    return _ANSI_ESCAPE.sub("", text)


_colorful_print = True
_print_func: Callable = print


def print(*values: object, sep: str | None = " ", end: str | None = "\n", file: Any = None, flush: bool = False):
    _print_func(*values, sep=sep, end=end, file=file, flush=flush)


@functools.lru_cache(maxsize=256)
def _ansi_prefix(
    fg_value: int | str | None,
    bg_value: int | str | None,
    styles_tuple: tuple[int, ...],
) -> str | None:
    codes: list[str] = []
    if styles_tuple:
        codes.extend(map(str, styles_tuple))
    if fg_value is not None:
        codes.append(str(fg_value))
    if bg_value is not None:
        codes.append(str(bg_value))
    if codes:
        return f"\033[{';'.join(codes)}m"
    return None


def _resolve_fg(color: Color | Color256 | TrueColor | None) -> int | str | None:
    if color is None:
        return None
    if isinstance(color, Color):
        return color.value
    if isinstance(color, Color256):
        return f"38;5;{color.value}"
    if isinstance(color, TrueColor):
        return f"38;2;{color.r};{color.g};{color.b}"
    return None


def _resolve_bg(color: BgColor | BgColor256 | BgTrueColor | None) -> int | str | None:
    if color is None:
        return None
    if isinstance(color, BgColor):
        return color.value
    if isinstance(color, BgColor256):
        return f"48;5;{color.value}"
    if isinstance(color, BgTrueColor):
        return f"48;2;{color.r};{color.g};{color.b}"
    return None


def colorful_text(
    text: str,
    fg: Color | Color256 | TrueColor | None = None,
    bg: BgColor | BgColor256 | BgTrueColor | None = None,
    styles: list[Style] | None = None,
) -> str:
    if not _colorful_print:
        return text
    prefix = _ansi_prefix(
        _resolve_fg(fg),
        _resolve_bg(bg),
        tuple(s.value for s in styles) if styles else (),
    )
    if prefix:
        text = f"{prefix}{text}\033[0m"
    return text


def colorful_print(
    text: str,
    fg: Color | Color256 | TrueColor | None = None,
    bg: BgColor | BgColor256 | BgTrueColor | None = None,
    styles: list[Style] | None = None,
    end: str = "\n",
    file: Any = None,
    flush: bool = False,
) -> None:
    if not _colorful_print:
        _print_func(text, end=end, file=file, flush=flush)
        return
    text = colorful_text(text, fg, bg, styles)
    _print_func(text, end=end, file=file, flush=flush)


class StreamPrintState(Enum):
    Text = 0
    Thinking = 1
    Other = 2


class PrintStream:
    """A stream wrapper that tracks whether the last printed character was a newline.

    Provides print_word(word) that automatically inserts a leading
    newline when the previous output didn't end with one.
    """

    def __init__(self, print_func: Callable = _print_func) -> None:
        self._print_func = print_func
        self._last_char_was_newline = True
        self._state = StreamPrintState.Other

    def print_word(self, word: str, require_new_line: bool, raw_word: str | None = None) -> None:
        """Print a word, auto-inserting a leading newline when the previous
        output didn't end with one."""
        if not word:
            if require_new_line and not self._last_char_was_newline:
                self._print_func('', end='\n')
                self._last_char_was_newline = True
            return

        if require_new_line and not self._last_char_was_newline:
            self._print_func('', end='\n')

        self._print_func(word, end='')
        check_word = raw_word if raw_word is not None else word
        self._last_char_was_newline = _strip_ansi(check_word).endswith('\n')

    def colorful_print_word(
            self, word: str,
            require_new_line: bool,
            fg: Color | Color256 | TrueColor | None = None,
            bg: BgColor | BgColor256 | BgTrueColor | None = None,
            styles: list[Style] | None = None) -> None:
        self.print_word(colorful_text(word, fg, bg, styles),
                        require_new_line=require_new_line, raw_word=word)


_quiet = False


def print_success(text: str, end: str = "\n") -> None:
    """Print success message in green."""
    colorful_print(text, fg=Color.BRIGHT_GREEN, styles=[Style.BOLD], end=end)


def print_string(text: str, end: str = "\n", file: Any = None, flush: bool = False) -> None:
    _print_func(text, end=end, file=file, flush=flush)


def print_error(text: str, end: str = "\n") -> None:
    """Print error message in red."""
    colorful_print(text, fg=Color.BRIGHT_RED, styles=[Style.BOLD], end=end)


def print_warning(text: str, end: str = "\n") -> None:
    """Print warning message in yellow."""
    colorful_print(text, fg=Color.BRIGHT_YELLOW, styles=[Style.BOLD], end=end)


def print_info(text: str, end: str = "\n") -> None:
    """Print info message in blue."""
    colorful_print(text, fg=Color.BRIGHT_MAGENTA, end=end)


def print_debug(text: str, end: str = "\n") -> None:
    """Print debug message in cyan."""
    if _quiet:
        return
    colorful_print(text, fg=Color.BRIGHT_CYAN, end=end)


def _process_lru() -> None:
    """Limit the number of threads to 8 by waiting and removing completed ones."""
    global _threads
    MAX_PROCESSES = 8

    _threads = [p for p in _threads if p.is_alive()]

    while len(_threads) >= MAX_PROCESSES:
        time.sleep(0.1)
        _threads = [p for p in _threads if p.is_alive()]


_stream = PrintStream()


_TOOL_TYPES = (ToolCall, ToolCallPart, ToolResult)
_PRINT_AGENT_JSON_MESSAGE_TYPE_ATTR = "_kimix_print_agent_json_message_type"


def _message_transition_type(wire_msg: Any) -> MessageType | None:
    if isinstance(wire_msg, TextPart):
        return MessageType.Text
    if isinstance(wire_msg, ThinkPart):
        return MessageType.Thinking
    if isinstance(wire_msg, _TOOL_TYPES):
        return MessageType.ToolCalling
    return None


def _print_transition_usage(session: Session, message_type: MessageType | None) -> None:
    if message_type is None:
        return
    previous_type = getattr(session, _PRINT_AGENT_JSON_MESSAGE_TYPE_ATTR, None)
    if previous_type is not None and previous_type != message_type:
        split_str = '=' * 20
        usage = percentage_and_token(session)
        left = f"{split_str} Context usage: {usage} "
        target_width = 80
        right_split = '=' * max(target_width - len(left), 1)
        _stream.colorful_print_word(
            f"{left}{right_split}\n",
            fg=GRAY,
            require_new_line=True,
        )
    setattr(session, _PRINT_AGENT_JSON_MESSAGE_TYPE_ATTR, message_type)


def _format_display_blocks(display: list[Any]) -> str | None:
    """Format display blocks into a colored terminal string.

    Returns a string ending with ``\n`` so that ``PrintStream.print_word``
    correctly tracks ``_last_char_was_newline`` after the output.
    """
    if not display:
        return None
    parts: list[str] = []
    for block in display:
        if isinstance(block, BriefDisplayBlock):
            if block.text:
                parts.append(colorful_text(block.text, fg=Color.BRIGHT_BLACK))
        elif isinstance(block, DiffDisplayBlock):
            parts.append(colorful_text(
                f"Diff: {block.path}", fg=Color.BRIGHT_YELLOW))
            for line in block.old_text.splitlines():
                parts.append(colorful_text(f"- {line}", fg=Color.BRIGHT_RED))
            for line in block.new_text.splitlines():
                parts.append(colorful_text(f"+ {line}", fg=Color.BRIGHT_GREEN))
        elif isinstance(block, TodoDisplayBlock):
            for item in block.items:
                status = item.status.replace("_", " ").lower()
                if status == "done":
                    parts.append(colorful_text(
                        f"- ~~{item.title}~~", fg=Color.BRIGHT_BLACK))
                elif status == "in progress":
                    parts.append(colorful_text(
                        f"- {item.title} \u2190", fg=Color.BRIGHT_YELLOW))
                else:
                    parts.append(colorful_text(
                        f"- {item.title}", fg=GRAY_LIGHT))
        elif isinstance(block, ShellDisplayBlock):
            parts.append(colorful_text(
                f"$ {block.command}", fg=Color.BRIGHT_BLUE))
        elif isinstance(block, BackgroundTaskDisplayBlock):
            parts.append(
                colorful_text(
                    f"[{block.status}] {block.task_id}: {block.description}", fg=Color.BRIGHT_BLACK)
            )
        elif isinstance(block, UnknownDisplayBlock):
            parts.append(colorful_text(str(block.data), fg=Color.BRIGHT_BLACK))
        elif isinstance(block, DisplayBlock):
            data = block.model_dump()
            if data:
                parts.append(colorful_text(str(data), fg=GRAY_LIGHT))
    if not parts:
        return None
    return "\n".join(parts) + "\n"


def _format_tool_result(result: ToolResult) -> str:
    """Format a ToolResult for the output function."""
    rv = result.return_value
    return rv.message or ""


_LAST_TOOL_CALL_KEY = "_kimix_last_tool_call"


def _format_tool_args(name: str, args: str | None) -> str | None:
    """Format tool arguments into a friendly one-line sentence."""
    if args is None:
        return None
    if args == "":
        return ""
    try:
        parsed = orjson.loads(args)
    except (orjson.JSONDecodeError, TypeError):
        return None

    try:
        if not isinstance(parsed, dict):
            return orjson.dumps(parsed).decode("utf-8")

        def _fmt(v: Any, max_len: int = 60) -> str:
            if v is None:
                return "None"
            s = str(v)
            if len(s) > max_len:
                return s[:max_len] + "..."
            return s

        def _collect(*keys: str, hide: set[str] | None = None) -> list[str]:
            hide = hide or set()
            parts: list[str] = []
            for key in keys:
                if key in parsed:
                    if key in hide:
                        parts.append(f"{key}: ...")
                    else:
                        parts.append(f"{key}: {_fmt(parsed[key])}")
            return parts

        match name:
            case "Bash":
                return ", ".join(_collect("cmd", "timeout"))
            case "Powershell":
                return ", ".join(_collect("cmd", "timeout"))
            case "Run":
                return ", ".join(_collect("command", "cwd", "timeout", "output_path", "env", "run_in_background"))
            case "Python":
                return ", ".join(_collect("code", "output_path", "timeout", "run_in_background", hide={"code"}))
            case "Input":
                return ", ".join(_collect("task_id", "text"))
            case "TaskOutput":
                return ", ".join(_collect("task_id", "block", "timeout", "output_path", "kill"))
            case "Search":
                return ", ".join(_collect("prompt", "dest_path"))
            case "TodoList":
                parts: list[str] = []
                if "todos" in parsed:
                    todos = parsed["todos"]
                    if todos is None:
                        parts.append("todos=None")
                    elif isinstance(todos, list):
                        parts.append(f"todos=[{len(todos)} items]")
                    else:
                        parts.append("todos=[1 item]")
                if parsed.get("mode") and parsed.get("mode") != "append":
                    parts.append(f"mode={parsed['mode']}")
                return ", ".join(parts)
            case "ReadFile":
                return ", ".join(_collect("path", "line_offset", "n_lines", "max_char", "char_offset"))
            case "EditFile":
                parts = []
                if "path" in parsed:
                    parts.append(f"path={_fmt(parsed['path'])}")
                if "edit" in parsed:
                    edit = parsed["edit"]
                    if edit is None:
                        parts.append("edit=None")
                    elif isinstance(edit, list):
                        parts.append(f"edit=[{len(edit)} edit(s)]")
                    else:
                        parts.append("edit=[1 edit]")
                return ", ".join(parts)
            case "WriteFile":
                return ", ".join(_collect("path", "content", "mode", hide={"content"}))
            case "Glob":
                return ", ".join(_collect("pattern", "directory", "include_dirs", "include_ignored"))
            case "Grep":
                return ", ".join(
                    _collect(
                        "pattern", "path", "glob", "output_mode",
                        "-B", "-A", "-C", "-n", "-i",
                        "type", "head_limit", "offset", "multiline", "include_ignored",
                    )
                )
            case "FetchURL":
                return ", ".join(_collect("url", "output_path"))
            case "Agent":
                return ", ".join(_collect("prompt", "session_id"))
            case "AgentList":
                return ""
            case "AgentClose":
                return ", ".join(_collect("session_id"))
            case "WritePlan":
                return ", ".join(_collect("content", "mode", hide={"content"}))
            case "ReadPlan":
                return ", ".join(_collect("line_offset", "n_lines", "max_char", "char_offset"))
            case "EditPlan":
                parts = []
                if "edit" in parsed:
                    edit = parsed["edit"]
                    if edit is None:
                        parts.append("edit=None")
                    elif isinstance(edit, list):
                        parts.append(f"edit=[{len(edit)} edit(s)]")
                    else:
                        parts.append("edit=[1 edit]")
                return ", ".join(parts)
            case "AskParent":
                return ", ".join(_collect("question", "context"))
            case _:
                return orjson.dumps(parsed).decode("utf-8")
    except TypeError:
        return None


def _handle_tool_call(
    wire_msg: ToolCall | ToolCallPart,
    output_function: Callable[[str, MessageType], Any] | None,
    session: Session,
) -> None:
    if isinstance(wire_msg, ToolCall):
        session._tmp_data[_LAST_TOOL_CALL_KEY] = wire_msg
        session._tmp_data[wire_msg.id] = wire_msg
        name = wire_msg.function.name
        args = wire_msg.function.arguments
        formatted = _format_tool_args(name, args)
        if formatted:
            header = f"⚡ {name} {formatted}"
        else:
            return
        _stream.colorful_print_word(
            header, fg=Color.BRIGHT_MAGENTA, require_new_line=True)
        _stream._state = StreamPrintState.Other
        if output_function:
            output_function(
                f"{name} {args or ''}", MessageType.ToolCalling)
    else:  # ToolCallPart
        last_tc: ToolCall = session._tmp_data.get(_LAST_TOOL_CALL_KEY)
        if last_tc is not None:
            last_tc.merge_in_place(wire_msg)
        if last_tc is not None:
            name = last_tc.function.name
            args = last_tc.function.arguments
            if args:
                formatted = _format_tool_args(name, args)
                header = None
                if formatted:
                    header = f"⚡ {name} {formatted}"
                if header:
                    _stream.colorful_print_word(header, fg=Color.BRIGHT_MAGENTA, require_new_line=True)
            if output_function:
                output_function(f"{name} {args or ''}", MessageType.ToolCallingPart)
        elif output_function:
            part = wire_msg.arguments_part or ""
            if part:
                output_function(part, MessageType.ToolCallingPart)
        _stream.print_word('', True)
        _stream._state = StreamPrintState.Other


def _handle_tool_result(wire_msg: ToolResult, output_function: Callable[[str, MessageType], Any] | None, _session: Session) -> None:
    rv = wire_msg.return_value
    display_text = _format_display_blocks(rv.display)
    _stream.print_word(display_text, require_new_line=True)
    result_text = _format_tool_result(wire_msg)
    if result_text:
        prefix = ("✗ " if rv.is_error else "✓ ")
        if display_text:
            tc: ToolCall | None = _session._tmp_data.pop(wire_msg.tool_call_id, None)
            if tc is None:
                tc = _session._tmp_data.pop(_LAST_TOOL_CALL_KEY, None)
            if tc:
                tool_name = tc.function.name if tc else "tool"
                _stream.colorful_print_word(
                    f"{prefix}{tool_name}",
                    fg=Color.BRIGHT_RED if rv.is_error else Color.BRIGHT_GREEN,
                    require_new_line=True,
                )
        else:
            _stream.colorful_print_word(
                f"{prefix}{result_text}",
                fg=Color.BRIGHT_RED if rv.is_error else Color.BRIGHT_GREEN,
                require_new_line=True,
            )
    else:
        _stream.print_word('', True)
    _stream._state = StreamPrintState.Other
    if output_function:
        formatted = f"[ToolResult] {_format_tool_result(wire_msg)}"
        if formatted:
            output_function(formatted, MessageType.ToolResult)


def _handle_approval_request(wire_msg: ApprovalRequest, _output_function: Callable[[str, MessageType], Any] | None, _session: Session) -> None:
    wire_msg.resolve("approve")


def _handle_noop(_wire_msg: Any, _output_function: Callable[[str, MessageType], Any] | None, _session: Session) -> None:
    pass


def _handle_compaction_begin(_wire_msg: Any, _output_function: Callable[[str, MessageType], Any] | None, _session: Session) -> None:
    _stream.colorful_print_word(
        "Compacting...", require_new_line=True, fg=Color.BRIGHT_MAGENTA)


def _handle_think_part(wire_msg: ThinkPart, output_function: Callable[[str, MessageType], Any] | None, _session: Session) -> None:
    think_content = wire_msg.think
    if not _quiet:
        if output_function:
            output_function(think_content, MessageType.Thinking)
        if _stream._state != StreamPrintState.Thinking:
            _stream.colorful_print_word(
                f"[Think] {think_content}", fg=Color.BRIGHT_CYAN, require_new_line=True)
        else:
            _stream.colorful_print_word(
                f"{think_content}", fg=Color.BRIGHT_CYAN, require_new_line=False)
        _stream._state = StreamPrintState.Thinking


def _handle_text_part(wire_msg: TextPart, output_function: Callable[[str, MessageType], Any] | None, _session: Session) -> None:
    chunk = wire_msg.text
    if output_function:
        output_function(chunk, MessageType.Text)
    _stream.print_word(
        chunk, require_new_line=_stream._state != StreamPrintState.Text)
    _stream._state = StreamPrintState.Text


def _handle_other(_wire_msg: Any, _output_function: Callable[[str, MessageType], Any] | None, _session: Session) -> None:
    _stream._state = StreamPrintState.Other


_PRINT_AGENT_JSON_DISPATCH: dict[type, Callable[[Any, Callable[[str, MessageType], Any] | None, Session], None]] = {
    ToolCall: _handle_tool_call,
    ToolCallPart: _handle_tool_call,
    ToolResult: _handle_tool_result,
    ApprovalRequest: _handle_approval_request,
    StepBegin: _handle_noop,
    StepInterrupted: _handle_noop,
    CompactionEnd: _handle_noop,
    CompactionBegin: _handle_compaction_begin,
    ThinkPart: _handle_think_part,
    TextPart: _handle_text_part,
}


def print_agent_json(
    wire_msg: Any,
    session: Session,
    output_function: Callable[[str, MessageType], Any] | None = None,
) -> None:
    _print_transition_usage(session, _message_transition_type(wire_msg))
    handler = _PRINT_AGENT_JSON_DISPATCH.get(type(wire_msg))
    if handler is not None:
        handler(wire_msg, output_function, session)
    else:
        _handle_other(wire_msg, output_function, session)


def run_thread(
    function: Callable[..., Any], args: tuple[Any, ...] | None = None
) -> threading.Thread:
    assert callable(function)
    global _threads
    _process_lru()

    if args is None:
        args = ()
    elif type(args) is not tuple:
        args = (args, )
    thd = threading.Thread(target=function, args=args)
    thd.start()
    _threads.append(thd)
    return thd


def run_script(path: str | Path) -> Any:
    return subprocess.Popen(
        [sys.executable, str(path)], creationflags=subprocess.CREATE_NEW_CONSOLE
    )


def sync_all() -> None:
    global _threads
    for thd in _threads:
        thd.join()
    _threads.clear()


def _run_process_with_log(command: str) -> tuple[str, int]:
    print_info(f"Shell: {command}")
    result = subprocess.run(command, shell=True, capture_output=True)
    output = result.stdout.decode(
        "utf-8", errors="replace") if result.stdout else ""
    if result.stderr:
        output += "\n" + result.stderr.decode("utf-8", errors="replace")
    return output, result.returncode


async def _run_process_with_log_async(command: str) -> tuple[str, int]:
    print_info(f"Shell: {command}")
    proc = await asyncio.create_subprocess_shell(
        command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    output = stdout.decode("utf-8", errors="replace") if stdout else ""
    if stderr:
        output += "\n" + stderr.decode("utf-8", errors="replace")
    return output, proc.returncode


def _filter_error_output(
    result: str, code: int, keycode: tuple[str, ...] | None, skip_success: bool
) -> str | None:
    if skip_success and code == 0:
        return None
    if not keycode:
        return result
    lines = result.splitlines()
    for idx, line in enumerate(lines):
        lower_line = line.lower()
        for c in keycode:
            if c in lower_line:
                return "\n".join(lines[idx:])
    return result


def run_process_with_error(
    command: str,
    keycode: tuple[str, ...] | None,
    skip_success: bool = True,
) -> str | None:
    result, code = _run_process_with_log(command)
    return _filter_error_output(result, code, keycode, skip_success)


async def run_process_with_error_async(
    command: str,
    keycode: tuple[str, ...] | None,
    skip_success: bool = True,
) -> str | None:
    result, code = await _run_process_with_log_async(command)
    return _filter_error_output(result, code, keycode, skip_success)


def percentage_str(num: float) -> str:
    return f"{num * 100:.1f}%"


def percentage_and_token(session: Any) -> str:
    status = session.status
    return f"{status.context_usage * 100:.1f}% ({status.context_tokens} tokens)"


_default_thinking: bool = True
_default_yolo: bool = True
_default_agent_file_dir: Path = Path(__file__).parent
_default_agent_file: Path = _default_agent_file_dir / "agent_worker.json"
_default_skill_dirs: list[Any] = []
_default_provider: dict[str, Any] | None = None
_default_sub_provider: dict[str, Any] | None = None
_default_manually_cot: bool = False
_default_ralph: int | None = None

# Common skill directory paths (relative to current working directory)
COMMON_SKILL_DIRS: list[str] = [
    ".agents/skills",
    ".config/.agents/skills",
    ".opencode/skills",
    ".skills",
    "skills",
]


def set_default_thinking(value: bool) -> None:
    global _default_thinking
    _default_thinking = value


def set_default_yolo(value: bool) -> None:
    global _default_yolo
    _default_yolo = value


def set_default_agent_file_dir(value: Path) -> None:
    global _default_agent_file_dir
    _default_agent_file_dir = value


def set_default_agent_file(value: Path) -> None:
    global _default_agent_file
    _default_agent_file = value


def set_default_skill_dirs(value: list[Any]) -> None:
    global _default_skill_dirs
    _default_skill_dirs = value


def set_default_manually_cot(value: bool) -> None:
    global _default_manually_cot
    _default_manually_cot = value


def set_default_provider(value: dict[str, Any] | None) -> None:
    global _default_provider
    _default_provider = value


def set_default_sub_provider(value: dict[str, Any] | None) -> None:
    global _default_sub_provider
    _default_sub_provider = value


# The failed-list for tool call that
# tuple: function-name, arguments, output, message


def get_skill_dirs(use_kaos_path: bool = True) -> list[Any]:
    from kaos.path import KaosPath

    global _default_skill_dirs
    if _default_skill_dirs:
        if use_kaos_path:
            return [KaosPath(str(i)) for i in _default_skill_dirs]
        return _default_skill_dirs

    _default_skill_dirs = [
        p for rel in COMMON_SKILL_DIRS if (p := Path(os.curdir) / rel).exists()
    ]
    # If there's a `skills` subdirectory under the skill dir, use `*/skills` pattern
    _default_skill_dirs = [
        p / "*/skills" if (p / "skills").is_dir() else p
        for p in _default_skill_dirs
    ]
    if _default_skill_dirs:
        for d in _default_skill_dirs:
            print_debug(f"skill dir: {str(d)}")
        if use_kaos_path:
            return [KaosPath(str(d)) for d in _default_skill_dirs]
        return _default_skill_dirs
    return []


generate_memory = """---

Compact the above agent conversation context according to the following priorities and rules.

**Priorities:**
- **Current Task State** — what is being worked on right now
- **Errors & Solutions** — all errors encountered and how they were resolved
- **Code Evolution** — final working versions only (drop intermediate attempts)
- **System Context** — project structure, dependencies, environment setup
- **Design Decisions** — architectural choices and rationale
- **TODO Items** — unfinished tasks and known issues
- **Project Overview** — purpose, scope, tech stack
- **Key Decisions** — critical choices, rationale, rejected alternatives
- **Current State** — what works, what's merged/verified, active branch, test results
- **Important Files** — key paths and their roles (add, modify, delete)
- **Architecture / Data Flow** — major components, interfaces, schema changes
- **Dependencies** — added, removed, upgraded packages or services
- **Risks / Rollback** — breaking changes, migration steps, revert strategy
- **Technical Notes** — patterns, constraints, APIs, env setup, performance or security considerations

**Rules:**
- **Keep:** error messages, stack traces, working solutions, current task
- **Merge:** similar discussions into single summary points
- **Remove:** redundant explanations, failed attempts (retain lessons learned), verbose comments
- **Condense:** long code blocks → signatures + key logic only

**Special Handling:**
- **Code:** keep full version if < 20 lines; otherwise keep signature + key logic
- **Errors:** keep full error message + final solution
- **Discussions:** extract decisions and action items only
```"""
