"""Performance benchmarks for kimix.base.

All timings are assert-based so the file doubles as a regression test.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from kimix.base import (
    _ANSI_ESCAPE,
    _format_display_blocks,
    _format_tool_args,
    _handle_text_part,
    _handle_think_part,
    _handle_tool_call,
    _handle_tool_result,
    _message_transition_type,
    _process_lru,
    _resolve_bg,
    _resolve_fg,
    _strip_ansi,
    colorful_text,
    percentage_and_token,
    print_agent_json,
    Color,
    BgColor,
    Color256,
    BgColor256,
    TrueColor,
    BgTrueColor,
    Style,
    PrintStream,
    MessageType,
)

pytestmark = pytest.mark.slow


# ---------------------------------------------------------------------------
# ANSI escape regex benchmarks
# ---------------------------------------------------------------------------


class TestAnsiEscapeBenchmark:
    """Benchmarks for ANSI escape sequence handling."""

    def test_ansi_re_sub_on_ansi_heavy_string(self) -> None:
        """_ANSI_ESCAPE_RE.sub() on long ANSI-heavy string."""
        # Build a string with many ANSI codes
        parts: list[str] = []
        for i in range(1000):
            parts.append(f"\033[31;1mtext_{i}\033[0m")
        heavy_text = " ".join(parts)
        start = time.perf_counter()
        for _ in range(50_000):
            _ANSI_ESCAPE.sub("", heavy_text)
        elapsed = time.perf_counter() - start
        assert elapsed < 60.0

    def test_ansi_re_sub_on_clean_string(self) -> None:
        """Regex on clean string (no ANSI)."""
        clean_text = "Hello, World! " * 1000
        start = time.perf_counter()
        for _ in range(50_000):
            _ANSI_ESCAPE.sub("", clean_text)
        elapsed = time.perf_counter() - start
        assert elapsed < 15.0

    def test_strip_ansi_various(self) -> None:
        """_strip_ansi() on various strings."""
        strings = [
            "plain text",
            "\033[31mred\033[0m",
            "\033[1;32mbold green\033[0m",
            "mixed \033[31mcolor\033[0m and plain",
            "\033[38;5;200m256 color\033[0m",
            "\033[48;2;255;0;0mbg truecolor\033[0m",
            "\x1b]0;title\x07\x1b[K",
            "\x1b[?25l\x1b[?25h",
        ] * 6_250
        start = time.perf_counter()
        for s in strings:
            _strip_ansi(s)
        elapsed = time.perf_counter() - start
        assert elapsed < 30.0


# ---------------------------------------------------------------------------
# _format_tool_args benchmarks
# ---------------------------------------------------------------------------


class TestFormatToolArgsBenchmark:
    """Benchmarks for _format_tool_args."""

    def test_format_tool_args_bash(self) -> None:
        """_format_tool_args('Bash', args_json)."""
        args = '{"cmd": "echo hello", "timeout": 30}'
        start = time.perf_counter()
        for _ in range(50_000):
            _format_tool_args("Bash", args)
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0

    def test_format_tool_args_grep(self) -> None:
        """_format_tool_args('Grep', args_json)."""
        args = '{"pattern": "test", "path": ".", "output_mode": "content", "-i": true}'
        start = time.perf_counter()
        for _ in range(50_000):
            _format_tool_args("Grep", args)
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0

    def test_format_tool_args_all_tools(self) -> None:
        """All 20+ tool names."""
        tool_args: dict[str, str] = {
            "Bash": '{"cmd": "ls"}',
            "Powershell": '{"cmd": "Get-ChildItem"}',
            "Run": '{"command": "echo hello", "cwd": "/tmp"}',
            "Python": '{"code": "print(1)"}',
            "TaskOutput": '{"task_id": "abc"}',
            "TodoList": '{"todos": [{"title": "test", "status": "pending"}]}',
            "ReadFile": '{"path": "test.txt"}',
            "EditFile": '{"path": "test.txt", "edit": [{"old": "a", "new": "b"}]}',
            "WriteFile": '{"path": "test.txt", "content": "hello"}',
            "Glob": '{"pattern": "*.py"}',
            "Grep": '{"pattern": "test", "path": "."}',
            "FetchURL": '{"url": "https://example.com"}',
            "Agent": '{"prompt": "do something"}',
            "AgentList": "{}",
            "AgentClose": '{"session_id": "abc"}',
            "ContextUsage": "{}",
            "Compact": '{"instruction": "summarize"}',
            "WritePlan": '{"content": "plan"}',
            "ReadPlan": '{"line_offset": 1}',
            "EditPlan": '{"edit": [{"old": "a", "new": "b"}]}',
        }
        start = time.perf_counter()
        for _ in range(5_000):
            for name, args in tool_args.items():
                _format_tool_args(name, args)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0


# ---------------------------------------------------------------------------
# _format_display_blocks benchmarks
# ---------------------------------------------------------------------------


class TestFormatDisplayBlocksBenchmark:
    """Benchmarks for _format_display_blocks."""

    def test_todo_display_block(self) -> None:
        """TodoDisplayBlock formatting."""
        from kimi_cli.tools.display import TodoDisplayBlock, TodoDisplayItem
        block = TodoDisplayBlock(
            items=[
                TodoDisplayItem(title=f"Task {i}", status="pending")
                for i in range(20)
            ]
        )
        start = time.perf_counter()
        for _ in range(10_000):
            _format_display_blocks([block])
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_diff_display_block(self) -> None:
        """DiffDisplayBlock formatting."""
        from kimi_cli.tools.display import DiffDisplayBlock
        block = DiffDisplayBlock(
            path="test.py",
            old_text="line1\nline2\nline3\n",
            new_text="line1\nline2_modified\nline3\nline4\n",
        )
        start = time.perf_counter()
        for _ in range(10_000):
            _format_display_blocks([block])
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_shell_display_block(self) -> None:
        """ShellDisplayBlock formatting."""
        from kimi_cli.tools.display import ShellDisplayBlock
        block = ShellDisplayBlock(
            language="python",
            command="print('hello world')",
        )
        start = time.perf_counter()
        for _ in range(10_000):
            _format_display_blocks([block])
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_mixed_display_blocks(self) -> None:
        """Mixed block types."""
        from kosong.tooling import BriefDisplayBlock
        from kimi_cli.tools.display import (
            DiffDisplayBlock,
            ShellDisplayBlock,
            TodoDisplayBlock,
            TodoDisplayItem,
        )
        blocks = [
            BriefDisplayBlock(text="Starting task..."),
            DiffDisplayBlock(path="test.py", old_text="a\nb\n", new_text="a\nc\n"),
            ShellDisplayBlock(language="bash", command="echo hello"),
            TodoDisplayBlock(
                items=[TodoDisplayItem(title="Task 1", status="done")]
            ),
        ]
        start = time.perf_counter()
        for _ in range(10_000):
            _format_display_blocks(blocks)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0


# ---------------------------------------------------------------------------
# colorful_text benchmarks
# ---------------------------------------------------------------------------


class TestColorfulTextBenchmark:
    """Benchmarks for colorful_text."""

    def test_simple_colored_text(self) -> None:
        """Simple colored text."""
        start = time.perf_counter()
        for _ in range(100_000):
            colorful_text("Hello", fg=Color.RED)
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0

    def test_256_color_text(self) -> None:
        """256-color text."""
        start = time.perf_counter()
        for _ in range(50_000):
            colorful_text("Hello", fg=Color256(200), bg=BgColor256(100))
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0

    def test_truecolor_text(self) -> None:
        """TrueColor text."""
        start = time.perf_counter()
        for _ in range(50_000):
            colorful_text(
                "Hello",
                fg=TrueColor(255, 128, 0),
                bg=BgTrueColor(0, 0, 128),
                styles=[Style.BOLD, Style.ITALIC],
            )
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0


# ---------------------------------------------------------------------------
# _resolve_fg / _resolve_bg benchmarks
# ---------------------------------------------------------------------------


class TestResolveColorBenchmark:
    """Benchmarks for _resolve_fg and _resolve_bg."""

    def test_resolve_fg_all_types(self) -> None:
        """_resolve_fg() with all color types."""
        colors = [
            Color.RED,
            Color.BRIGHT_GREEN,
            Color256(128),
            Color256(255),
            TrueColor(255, 0, 0),
            TrueColor(0, 255, 0),
            None,
        ] * 28_572
        start = time.perf_counter()
        for c in colors:
            _resolve_fg(c)
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0

    def test_resolve_bg_all_types(self) -> None:
        """_resolve_bg() with all bg color types."""
        colors = [
            BgColor.RED,
            BgColor.BRIGHT_GREEN,
            BgColor256(128),
            BgColor256(255),
            BgTrueColor(255, 0, 0),
            BgTrueColor(0, 255, 0),
            None,
        ] * 28_572
        start = time.perf_counter()
        for c in colors:
            _resolve_bg(c)
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0


# ---------------------------------------------------------------------------
# PrintStream benchmarks
# ---------------------------------------------------------------------------


class TestPrintStreamBenchmark:
    """Benchmarks for PrintStream."""

    def test_print_word_with_newline_tracking(self) -> None:
        """PrintStream.print_word() with newline tracking."""
        ps = PrintStream()
        words = ["hello", "world", "test"] * 33_334
        start = time.perf_counter()
        for w in words:
            ps.print_word(w, require_new_line=True)
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0


# ---------------------------------------------------------------------------
# Handler benchmarks (require mock session)
# ---------------------------------------------------------------------------


def _make_mock_session() -> Any:
    """Create a minimal mock session for handler benchmarks."""
    from unittest.mock import MagicMock
    session = MagicMock()
    session._tmp_data = {}
    session.status.context_usage = 0.25
    session.status.context_tokens = 5000
    return session


class TestHandlersBenchmark:
    """Benchmarks for message handlers."""

    def test_handle_text_part_with_mock(self) -> None:
        """_handle_text_part() with mock session."""
        session = _make_mock_session()
        from kimi_cli.wire.types import TextPart
        text_part = TextPart(text="Hello, this is a sample text message. " * 20)

        start = time.perf_counter()
        for _ in range(50_000):
            _handle_text_part(text_part, None, session)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_handle_think_part_with_mock(self) -> None:
        """_handle_think_part() with mock session."""
        session = _make_mock_session()
        from kimi_cli.wire.types import ThinkPart
        think_part = ThinkPart(think="I need to think about this carefully...")

        start = time.perf_counter()
        for _ in range(50_000):
            _handle_think_part(think_part, None, session)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_handle_tool_call_with_mock(self) -> None:
        """_handle_tool_call() with mock session."""
        session = _make_mock_session()
        from kimi_cli.wire.types import ToolCall
        tool_call = ToolCall(
            id="call_1",
            function={"name": "Bash", "arguments": '{"cmd": "echo hello"}'},
        )

        start = time.perf_counter()
        for _ in range(10_000):
            _handle_tool_call(tool_call, None, session)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_handle_tool_result_with_mock(self) -> None:
        """_handle_tool_result() with mock session."""
        session = _make_mock_session()
        from kimi_cli.wire.types import ToolResult
        from kosong.tooling import ToolReturnValue
        tool_result = ToolResult(
            tool_call_id="call_1",
            return_value=ToolReturnValue(is_error=False, message="Success!", output="", display=[]),
        )

        start = time.perf_counter()
        for _ in range(10_000):
            _handle_tool_result(tool_result, None, session)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0


# ---------------------------------------------------------------------------
# Other function benchmarks
# ---------------------------------------------------------------------------


class TestMiscFunctionsBenchmark:
    """Benchmarks for other functions in base.py."""

    def test_print_agent_json_dispatch(self) -> None:
        """print_agent_json() dispatch overhead."""
        session = _make_mock_session()
        from kimi_cli.wire.types import StepBegin

        msg = StepBegin(n=1)
        start = time.perf_counter()
        for _ in range(100_000):
            print_agent_json(msg, session)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_message_transition_type(self) -> None:
        """_message_transition_type()."""
        from kimi_cli.wire.types import (
            TextPart,
            ThinkPart,
            ToolCall,
            ToolCallPart,
            ToolResult,
            StepBegin,
            StepInterrupted,
            CompactionBegin,
            CompactionEnd,
            ApprovalRequest,
        )
        msgs = [
            TextPart(text="hello"),
            ThinkPart(think="thinking"),
            ToolCall(id="1", function={"name": "test", "arguments": "{}"}),
            ToolCallPart(arguments_part="part"),
            ToolResult(tool_call_id="1", return_value={"is_error": False, "message": "ok", "output": "", "display": []}),
            StepBegin(n=1),
            StepInterrupted(),
            CompactionBegin(),
            CompactionEnd(),
            ApprovalRequest(id="approve_1", tool_call_id="call_1", sender="user", action="approve", description="Approve?"),
        ]
        start = time.perf_counter()
        for _ in range(200_000):
            for msg in msgs:
                _message_transition_type(msg)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_percentage_and_token(self) -> None:
        """percentage_and_token() with mock session."""
        session = _make_mock_session()
        start = time.perf_counter()
        for _ in range(200_000):
            percentage_and_token(session)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_process_lru(self) -> None:
        """_process_lru() with 0-8 active threads."""
        import threading
        # Clean up any existing threads
        for _ in range(10):
            _process_lru()
        start = time.perf_counter()
        for _ in range(1_000):
            _process_lru()
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0
