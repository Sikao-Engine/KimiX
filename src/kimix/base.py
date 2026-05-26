from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Any, Callable

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

_threads: list[threading.Thread] = []


class MessageType(Enum):
    """Message type for print_agent_json output function."""
    Text = "text"
    Thinking = "thinking"
    ToolCalling = "tool_calling"


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


_ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE.sub("", text)

_colorful_print = True
_print_func: Callable = print


def print(*values: object, sep: str | None = " ", end: str | None = "\n", file: Any = None, flush: bool = False):
    _print_func(*values, sep=sep, end=end, file=file, flush=flush)

def colorful_text(
    text: str,
    fg: Color | None = None,
    bg: BgColor | None = None,
    styles: list[Style] | None = None,
) -> str:
    codes: list[int] = []
    if styles:
        codes.extend(style.value for style in styles)
    if fg:
        codes.append(fg.value)
    if bg:
        codes.append(bg.value)
    if codes:
        text = f"\033[{';'.join(map(str, codes))}m{text}\033[0m"
    return text


def colorful_print(
    text: str,
    fg: Color | None = None,
    bg: BgColor | None = None,
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
        fg: Color | None = None,
        bg: BgColor | None = None,
        styles: list[Style] | None = None) -> None:
        self.print_word(colorful_text(word, fg, bg, styles), require_new_line=require_new_line, raw_word=word)

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
            parts.append(colorful_text(f"Diff: {block.path}", fg=Color.BRIGHT_YELLOW))
            for line in block.old_text.splitlines():
                parts.append(colorful_text(f"- {line}", fg=Color.BRIGHT_RED))
            for line in block.new_text.splitlines():
                parts.append(colorful_text(f"+ {line}", fg=Color.BRIGHT_GREEN))
        elif isinstance(block, TodoDisplayBlock):
            for item in block.items:
                status = item.status.replace("_", " ").lower()
                if status == "done":
                    parts.append(colorful_text(f"- ~~{item.title}~~", fg=Color.BRIGHT_BLACK))
                elif status == "in progress":
                    parts.append(colorful_text(f"- {item.title} \u2190", fg=Color.BRIGHT_YELLOW))
                else:
                    parts.append(colorful_text(f"- {item.title}", fg=Color.BRIGHT_BLACK))
        elif isinstance(block, ShellDisplayBlock):
            parts.append(colorful_text(f"$ {block.command}", fg=Color.BRIGHT_BLUE))
        elif isinstance(block, BackgroundTaskDisplayBlock):
            parts.append(
                colorful_text(f"[{block.status}] {block.task_id}: {block.description}", fg=Color.BRIGHT_BLACK)
            )
        elif isinstance(block, UnknownDisplayBlock):
            parts.append(colorful_text(str(block.data), fg=Color.BRIGHT_BLACK))
        elif isinstance(block, DisplayBlock):
            data = block.model_dump()
            if data:
                parts.append(colorful_text(str(data), fg=Color.BRIGHT_BLACK))
    if not parts:
        return None
    return "\n".join(parts) + "\n"


def _format_tool_result(result: ToolResult) -> str:
    """Format a ToolResult for the output function."""
    rv = result.return_value
    return rv.message or ""
def _print_agent_json(
    wire_msg: Any, output_function: Callable[[str, MessageType], Any] | None = None
) -> None:
    if isinstance(wire_msg, _TOOL_TYPES):
        if isinstance(wire_msg, ToolCall):
            name = wire_msg.function.name
            header = f"⚡ {name}"
            _stream.colorful_print_word(header, fg=Color.BRIGHT_MAGENTA, require_new_line=True)
            if output_function:
                output_function(f"{name} {wire_msg.function.arguments or ''}", MessageType.ToolCalling)
        elif isinstance(wire_msg, ToolCallPart):
            part = wire_msg.arguments_part or ""
            if output_function and part:
                output_function(part, MessageType.ToolCalling)
            _stream.print_word('', True)
        elif isinstance(wire_msg, ToolResult):
            rv = wire_msg.return_value
            display_text = _format_display_blocks(rv.display)
            _stream.print_word(display_text, require_new_line=True)
            result_text = _format_tool_result(wire_msg)
            if result_text:
                prefix = ("✗ " if rv.is_error else "✓ ")
                _stream.colorful_print_word(f"{prefix}{result_text}", fg=Color.BRIGHT_RED if rv.is_error else Color.BRIGHT_GREEN, require_new_line=True)
            else:
                _stream.print_word('', True)
            if output_function:
                formatted = f"[ToolResult] {_format_tool_result(wire_msg)}"
                if formatted:
                    output_function(formatted, MessageType.ToolCalling)
        return
    
    if isinstance(wire_msg, ApprovalRequest):
        wire_msg.resolve("approve")
        return

    if isinstance(wire_msg, (StepBegin, StepInterrupted, CompactionEnd)):
        return

    if isinstance(wire_msg, CompactionBegin):
        _stream.colorful_print_word("Compacting...", require_new_line=True, fg=Color.BRIGHT_MAGENTA)
        return

    if isinstance(wire_msg, ThinkPart):
        think_content = wire_msg.think
        if not _quiet:
            if output_function:
                output_function(think_content, MessageType.Thinking)
            if _stream._state != StreamPrintState.Thinking:
                _stream.colorful_print_word(f"[Think] {think_content}", fg=Color.BRIGHT_CYAN, require_new_line=True)
            else:
                _stream.colorful_print_word(f"{think_content}", fg=Color.BRIGHT_CYAN, require_new_line=False)
            _stream._state = StreamPrintState.Thinking
        return

    if isinstance(wire_msg, TextPart):
        chunk = wire_msg.text
        if output_function:
            output_function(chunk, MessageType.Text)
        _stream.print_word(chunk, require_new_line=_stream._state != StreamPrintState.Text)
        _stream._state = StreamPrintState.Text
        return
    else:
        _stream._state = StreamPrintState.Other
print_lock = threading.Lock()
def print_agent_json(
    wire_msg: Any, output_function: Callable[[str, MessageType], Any] | None = None
) -> None:
    with print_lock:
        _print_agent_json(wire_msg, output_function)

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
    output = result.stdout.decode("utf-8", errors="replace") if result.stdout else ""
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
_default_supervisor: bool = False

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


def set_default_supervisor(value: bool) -> None:
    global _default_supervisor
    _default_supervisor = value


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
