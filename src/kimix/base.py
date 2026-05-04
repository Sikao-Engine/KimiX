from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional
import os
from enum import Enum
import json
import threading
import sys
import asyncio

_threads: list[threading.Thread] = list()


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
_print_func: Callable[[str, str], Any] | None = None


def colorful_text(
    text: str,
    fg: Optional[Color] = None,
    bg: Optional[BgColor] = None,
    styles: Optional[list[Style]] = None,
):
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
    fg: Optional[Color] = None,
    bg: Optional[BgColor] = None,
    styles: Optional[list[Style]] = None,
    end: str = "\n"
) -> None:
    if not _colorful_print:
        if _print_func:
            _print_func(text, end)
        else:
            print(text, end=end)
        return
    text = colorful_text(text, fg, bg, styles)
    if _print_func:
        _print_func(text, end)
    else:
        print(text, end=end)


_quiet = False


def print_success(text: str, end: str = "\n") -> None:
    """Print success message in green."""
    colorful_print(text, fg=Color.BRIGHT_GREEN, styles=[Style.BOLD], end=end)


def print_string(text: str, end: str = "\n") -> None:
    if _print_func:
        _print_func(text, end)
    else:
        print(text, end=end)


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
    import time
    """Limit the number of processes to 32 by waiting and removing completed ones."""
    global _threads
    MAX_PROCESSES = 8

    # Remove already completed processes first
    _threads = [p for p in _threads if p.is_alive()]

    # If still over limit, wait for processes to complete
    while len(_threads) >= MAX_PROCESSES:
        # Wait for the first process to complete with a timeout
        time.sleep(0.1)
        # Remove completed processes
        _threads = [p for p in _threads if p.is_alive()]


PRINT_STREAM = threading.local()


def print_agent_json(wire_msg: Any, output_function: Callable[[str, bool], Any] | None = None) -> None:
    # Lazy imports of wire message types
    from kimi_cli.wire.types import (
        ApprovalRequest,
        StepBegin,
        StepInterrupted,
        TextPart,
        ThinkPart,
        ToolCall,
        ToolCallPart,
        ToolResult,
    )

    think = getattr(PRINT_STREAM, 'think', False)

    if isinstance(wire_msg, ApprovalRequest):
        wire_msg.resolve("approve")
        return

    if isinstance(wire_msg, StepBegin):
        print()
        think = False
        PRINT_STREAM.think = False
        return

    if isinstance(wire_msg, StepInterrupted):
        print()
        think = False
        PRINT_STREAM.think = False
        return

    if isinstance(wire_msg, ThinkPart):
        think_content = wire_msg.think
        if think_content and not _quiet:
            if not think:
                think_content = f"[Think] {think_content}"
                think = True
                PRINT_STREAM.think = True
            if output_function:
                output_function(think_content, True)
            colorful_print(think_content, fg=Color.BRIGHT_CYAN, end='')
        return

    if isinstance(wire_msg, TextPart):
        chunk = wire_msg.text
        if chunk:
            if think:
                print()
                think = False
                PRINT_STREAM.think = False
            if not ("<choice>" in chunk and "</choice>" in chunk):
                if output_function:
                    output_function(chunk, False)
                if _print_func:
                    _print_func(f"\n{chunk}", '')
                else:
                    print(chunk, end='')
        return

    # ToolCall, ToolCallPart, ToolResult - ignore for console printing
    if isinstance(wire_msg, (ToolCall, ToolCallPart, ToolResult)):
        return


def run_thread(function: Callable[..., Any], args: tuple[Any, ...] | None = None) -> threading.Thread:
    assert callable(function)
    global _threads
    # Enforce process limit before creating new one
    _process_lru()

    if args is None:
        args = tuple()
    elif type(args) is not tuple:
        args = (args, )
    thd = threading.Thread(target=function, args=args)
    thd.start()

    _threads.append(thd)
    return thd


def run_script(path: str | Path) -> Any:
    import subprocess
    return subprocess.Popen(
        [sys.executable, str(path)], creationflags=subprocess.CREATE_NEW_CONSOLE)


def sync_all() -> None:
    global _threads
    for thd in _threads:
        thd.join()
    _threads.clear()


def _run_process_with_log(command: str) -> tuple[str, int]:
    import subprocess
    print_info(f'Shell: {command}')
    result = subprocess.run(command, shell=True,
                            capture_output=True, text=False)
    # Decode stdout with UTF-8, handle decode errors
    if result.stdout:
        output = result.stdout.decode('utf-8', errors='replace')
    else:
        output = ""
    # Decode stderr with UTF-8, handle decode errors
    if result.stderr:
        stderr = result.stderr.decode('utf-8', errors='replace')
        output += "\n" + stderr
    return output, result.returncode


async def _run_process_with_log_async(command: str) -> tuple[str, int]:
    print_info(f'Shell: {command}')
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    # Decode stdout with UTF-8, handle decode errors
    if stdout:
        output = stdout.decode('utf-8', errors='replace')
    else:
        output = ""
    # Decode stderr with UTF-8, handle decode errors
    if stderr:
        output += "\n" + stderr.decode('utf-8', errors='replace')
    return output, proc.returncode


def run_process_with_error(command: str, keycode: tuple[str, ...] | None, skip_success: bool = True) -> str | None:
    result, code = _run_process_with_log(command)
    if skip_success and code == 0:
        return None
    lines = result.splitlines()
    if keycode is None or len(keycode) == 0:
        return result
    for idx in range(len(lines)):
        line = lines[idx]
        lower_line = line.lower()
        for c in keycode:
            if c in lower_line:
                return '\n'.join(lines[idx:])

    return result


async def run_process_with_error_async(command: str, keycode: tuple[str, ...] | None, skip_success: bool = True) -> str | None:
    result, code = await _run_process_with_log_async(command)
    if skip_success and code == 0:
        return None
    lines = result.splitlines()
    if keycode is None or len(keycode) == 0:
        return result
    for idx in range(len(lines)):
        line = lines[idx]
        lower_line = line.lower()
        for c in keycode:
            if c in lower_line:
                return '\n'.join(lines[idx:])

    return result


def percentage_str(num: float) -> str:
    return f"{num * 100:.1f}%"


_default_thinking: bool = True
_default_yolo: bool = True
_default_agent_file_dir: Path = Path(__file__).parent
_default_agent_file: Path = _default_agent_file_dir / 'agent_worker.yaml'
_default_skill_dirs: list[Any] = []
_default_provider: dict[str, Any] | None = None

# Common skill directory paths (relative to current working directory)
COMMON_SKILL_DIRS: list[str] = [
    ".agents/skills",
    ".config/.agents/skills",
    ".opencode/skills",
    "skills"
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


def set_default_provider(value: dict[str, Any] | None) -> None:
    global _default_provider
    _default_provider = value


# The failed-list for tool call that
# tuple: function-name, arguments, output, message
_tool_call_failed_lists: dict[str, list[tuple[str, str, str, str]]] = dict()


def get_skill_dirs(use_kaos_path: bool = True) -> list[Any]:
    from kaos.path import KaosPath
    global _default_skill_dirs
    if _default_skill_dirs:
        if use_kaos_path:
            return [KaosPath(str(i)) for i in _default_skill_dirs]
        return _default_skill_dirs

    def _gen() -> list[Path]:
        from concurrent.futures import ThreadPoolExecutor
        paths = [Path(os.curdir) / rel for rel in COMMON_SKILL_DIRS]
        with ThreadPoolExecutor() as executor:
            futures = [(p, executor.submit(p.exists)) for p in paths]
            return [p for p, fut in futures if fut.result()]
    _default_skill_dirs = _gen()
    if _default_skill_dirs:
        for d in _default_skill_dirs:
            print_debug(f'skill dir: {str(d)}')
        if use_kaos_path:
            return [
                KaosPath(str(d))
                for d in _default_skill_dirs
            ]
        return _default_skill_dirs
    return []

_default_ralph: bool | None = None
generate_memory = '''Summarize the session for a coding agent. Output directly; no preamble.
1. **Project Overview**: Purpose, scope, tech stack.
2. **Key Decisions**: Critical choices, rationale, rejected alternatives.
3. **Current State**: What works, what's merged/verified, active branch, test results.
4. **Important Files**: Key paths and their roles (add, modify, delete).
5. **Architecture / Data Flow**: Major components, interfaces, schema changes.
6. **Dependencies**: Added, removed, upgraded packages or services.
7. **TODOs / Blockers**: Remaining tasks, known issues, external dependencies.
8. **Risks / Rollback**: Breaking changes, migration steps, revert strategy.
9. **Technical Notes**: Patterns, constraints, APIs, env setup, performance or security considerations.'''
