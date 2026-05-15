from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from kimi_cli.wire.types import (
    ApprovalRequest,
    CompactionBegin,
    CompactionEnd,
    StepBegin,
    StepInterrupted,
    TextPart,
    ThinkPart,
    ToolCall,
    ToolCallPart,
    ToolResult,
)

_threads: list[threading.Thread] = []


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


_colorful_print = True
_print_func: Callable = print


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
) -> None:
    if not _colorful_print:
        _print_func(text, end=end)
        return
    text = colorful_text(text, fg, bg, styles)
    _print_func(text, end=end)


_quiet = False


def print_success(text: str, end: str = "\n") -> None:
    """Print success message in green."""
    colorful_print(text, fg=Color.BRIGHT_GREEN, styles=[Style.BOLD], end=end)


def print_string(text: str, end: str = "\n") -> None:
    _print_func(text, end=end)


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


PRINT_STREAM_last_ended_with_newline = False
PRINT_STREAM_flag: str | None = None


def print_tool(s: str) -> None:
    global PRINT_STREAM_flag, PRINT_STREAM_last_ended_with_newline
    if (
        PRINT_STREAM_flag is not None
        and PRINT_STREAM_flag != "tool"
        and not PRINT_STREAM_last_ended_with_newline
    ):
        if not s.startswith("\n"):
            s = "\n" + s
        PRINT_STREAM_flag = "tool"
        PRINT_STREAM_last_ended_with_newline = True
    _print_func(s)


import kimi_cli.soul.toolset as toolset

toolset.print_tool_func = print_tool

_TOOL_TYPES = (ToolCall, ToolCallPart, ToolResult)


def print_agent_json(
    wire_msg: Any, output_function: Callable[[str, bool], Any] | None = None
) -> None:
    def _set_last_ended_with_newline(ended: bool) -> None:
        global PRINT_STREAM_last_ended_with_newline
        PRINT_STREAM_last_ended_with_newline = ended

    def _switch(new_flag: str | None) -> bool:
        global PRINT_STREAM_flag
        if PRINT_STREAM_flag != new_flag:
            if (
                PRINT_STREAM_flag is not None
                and PRINT_STREAM_flag != "tool"
                and not PRINT_STREAM_last_ended_with_newline
            ):
                _print_func('')
            PRINT_STREAM_flag = new_flag
            return True
        return False

    if isinstance(wire_msg, ApprovalRequest):
        wire_msg.resolve("approve")
        return

    if isinstance(wire_msg, (StepBegin, StepInterrupted, CompactionEnd)):
        _switch(None)
        return

    if isinstance(wire_msg, CompactionBegin):
        _switch(None)
        print_info("Compacting...")
        return

    if isinstance(wire_msg, ThinkPart):
        think_content = wire_msg.think
        if think_content.strip() and not _quiet:
            if _switch("think"):
                think_content = f"[Think] {think_content}"
            if output_function:
                output_function(think_content, True)
            colorful_print(think_content, fg=Color.BRIGHT_CYAN, end="")
            _set_last_ended_with_newline(think_content.endswith("\n"))
        return

    if isinstance(wire_msg, TextPart):
        chunk = wire_msg.text
        if chunk.strip():
            _switch("text")
            if output_function:
                output_function(chunk, False)
            _print_func(chunk, end="")
            _set_last_ended_with_newline(chunk.endswith("\n"))
        return

    if isinstance(wire_msg, _TOOL_TYPES):
        _switch("tool")
        return


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
    if _default_skill_dirs:
        for d in _default_skill_dirs:
            print_debug(f"skill dir: {str(d)}")
        if use_kaos_path:
            return [KaosPath(str(d)) for d in _default_skill_dirs]
        return _default_skill_dirs
    return []


generate_memory = """Summarize the session for a coding agent. Output directly; no preamble.
1. **Project Overview**: Purpose, scope, tech stack.
2. **Key Decisions**: Critical choices, rationale, rejected alternatives.
3. **Current State**: What works, what's merged/verified, active branch, test results.
4. **Important Files**: Key paths and their roles (add, modify, delete).
5. **Architecture / Data Flow**: Major components, interfaces, schema changes.
6. **Dependencies**: Added, removed, upgraded packages or services.
7. **TODOs / Blockers**: Remaining tasks, known issues, external dependencies.
8. **Risks / Rollback**: Breaking changes, migration steps, revert strategy.
9. **Technical Notes**: Patterns, constraints, APIs, env setup, performance or security considerations."""
