"""Performance benchmarks for kimix.tools.common.

All timings are assert-based so the file doubles as a regression test.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from kimix.tools.common import (
    _find_error_line_index,
    _export_to_temp_file,
    _maybe_export_output,
    _build_session_output_block,
    _env_with_rg_bin_path,
    filter_output,
    ProcessTask,
)

pytestmark = pytest.mark.slow


# ---------------------------------------------------------------------------
# filter_output benchmarks
# ---------------------------------------------------------------------------


class TestFilterOutputBenchmark:
    """Benchmarks for filter_output()."""

    def test_ansi_heavy_string(self) -> None:
        """String with many ANSI codes."""
        parts: list[str] = []
        for i in range(500):
            parts.append(f"\033[31;1mError: something failed at line {i}\033[0m")
        heavy_text = "\n".join(parts)
        start = time.perf_counter()
        for _ in range(10_000):
            filter_output(heavy_text)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_clean_string_no_ansi(self) -> None:
        """String without ANSI codes."""
        clean_text = "\n".join(f"line {i}: some output content" for i in range(500))
        start = time.perf_counter()
        for _ in range(10_000):
            filter_output(clean_text)
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0

    def test_mixed_ansi_and_clean(self) -> None:
        """Mixed ANSI and clean text."""
        lines: list[str] = []
        for i in range(1000):
            if i % 3 == 0:
                lines.append(f"\033[32msuccess: operation {i}\033[0m")
            elif i % 5 == 0:
                lines.append(f"\033[33mwarning: something at line {i}\033[0m")
        text = "\n".join(lines)
        start = time.perf_counter()
        for _ in range(5_000):
            filter_output(text)
        elapsed = time.perf_counter() - start
        assert elapsed < 20.0

    def test_simple_ansi_codes(self) -> None:
        """Simple ANSI color codes."""
        text = "\x1b[31mHello\x1b[32mWorld\x1b[0m" * 5000
        start = time.perf_counter()
        for _ in range(5_000):
            filter_output(text)
        elapsed = time.perf_counter() - start
        assert elapsed < 20.0

    def test_osc_and_dcs_sequences(self) -> None:
        """String with OSC / DCS / APC sequences."""
        text = (
            "\x1b]0;terminal title\x07"
            "\x1b[31mcolored\x1b[0m"
            "\x1bPsome data\x1b\\"
            "\x1b_apc data\x1b\\"
            "\x1b[?25l"
            "\x1b[?25h"
            "normal text"
        ) * 2000
        start = time.perf_counter()
        for _ in range(5_000):
            filter_output(text)
        elapsed = time.perf_counter() - start
        assert elapsed < 20.0
# ---------------------------------------------------------------------------
# _find_error_line_index benchmarks
# ---------------------------------------------------------------------------


class TestFindErrorLineIndexBenchmark:
    """Benchmarks for _find_error_line_index()."""

    def test_short_output_with_error(self) -> None:
        """Short output with error keyword."""
        output = "line 1: starting process\nline 2: Error: something went wrong\nline 3: done\n"
        start = time.perf_counter()
        for _ in range(50_000):
            _find_error_line_index(output)
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0

    def test_long_output_error_at_end(self) -> None:
        """1000-line output, error at end."""
        lines = [f"line {i}: processing data" for i in range(999)]
        lines.append("FATAL ERROR: system crash")
        output = "\n".join(lines)
        start = time.perf_counter()
        for _ in range(1_000):
            _find_error_line_index(output)
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0

    def test_no_error_found(self) -> None:
        """Long output with no error keywords."""
        lines = [f"info: step {i} completed successfully" for i in range(500)]
        output = "\n".join(lines)
        start = time.perf_counter()
        for _ in range(10_000):
            _find_error_line_index(output)
        elapsed = time.perf_counter() - start
        assert elapsed < 20.0

    def test_mixed_keywords(self) -> None:
        """Output with various error keywords."""
        lines: list[str] = []
        for i in range(200):
            if i % 7 == 0:
                lines.append(f"Traceback (most recent call last):")
                lines.append(f"  File \"test.py\", line {i}, in <module>")
                lines.append(f"    raise ValueError(\"test\")")
                lines.append(f"ValueError: test")
            elif i % 11 == 0:
                lines.append(f"panic: runtime error at iteration {i}")
            elif i % 13 == 0:
                lines.append(f"AssertionError: assertion failed in step {i}")
            else:
                lines.append(f"normal line {i}")
        output = "\n".join(lines)
        start = time.perf_counter()
        for _ in range(5_000):
            _find_error_line_index(output)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0


# ---------------------------------------------------------------------------
# _export_to_temp_file benchmarks
# ---------------------------------------------------------------------------


class TestExportToTempFileBenchmark:
    """Benchmarks for _export_to_temp_file()."""

    def test_export_10kb_content(self) -> None:
        """Export 10KB content to temp file."""
        content = "x" * 10_240
        start = time.perf_counter()
        for _ in range(500):
            path, is_new = _export_to_temp_file(None, content)
            assert isinstance(path, str)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0


# ---------------------------------------------------------------------------
# _maybe_export_output benchmarks
# ---------------------------------------------------------------------------


class TestMaybeExportOutputBenchmark:
    """Benchmarks for _maybe_export_output()."""

    def test_under_limit(self) -> None:
        """Content under OUTPUT_LIMIT."""
        short = "short output" * 500  # ~6000 chars, under 16384 limit
        start = time.perf_counter()
        for _ in range(10_000):
            result = _maybe_export_output(short)
            assert "\n" not in result  # not exported
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0

    def test_over_limit(self) -> None:
        """Content over OUTPUT_LIMIT."""
        long_content = "x" * 20_000  # over 16384 limit
        start = time.perf_counter()
        for _ in range(500):
            result = _maybe_export_output(long_content)
            assert "exported" in result or "added" in result
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0


# ---------------------------------------------------------------------------
# _build_session_output_block benchmarks
# ---------------------------------------------------------------------------


class TestBuildSessionOutputBlockBenchmark:
    """Benchmarks for _build_session_output_block()."""

    def test_small_block(self) -> None:
        """Small metadata block."""
        start = time.perf_counter()
        for _ in range(10_000):
            _build_session_output_block(
                task_id="abc-123",
                status="completed",
                output="Command ran successfully.\nExit code: 0",
                exit_code=0,
                elapsed_seconds=1.5,
            )
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0

    def test_large_output_block(self) -> None:
        """Block with large output content."""
        output = "\n".join(f"line {i}: some output" for i in range(500))
        start = time.perf_counter()
        for _ in range(5_000):
            _build_session_output_block(
                task_id="abc-123",
                status="completed",
                output=output,
                exit_code=0,
                output_path="/tmp/output.txt",
                output_truncated=True,
                elapsed_seconds=10.5,
            )
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0


# ---------------------------------------------------------------------------
# _env_with_rg_bin_path benchmarks
# ---------------------------------------------------------------------------


class TestEnvWithRgBinPathBenchmark:
    """Benchmarks for _env_with_rg_bin_path()."""

    def test_path_manipulation(self) -> None:
        """PATH manipulation."""
        start = time.perf_counter()
        for _ in range(10_000):
            result = _env_with_rg_bin_path()
            assert "PATH" in result
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0


# ---------------------------------------------------------------------------
# ProcessTask benchmarks — actual subprocess execution
# ---------------------------------------------------------------------------


class TestProcessTaskBenchmark:
    """Benchmarks for ProcessTask — actual subprocess execution."""

    @pytest.mark.asyncio
    async def test_echo_hello(self) -> None:
        """Run `echo hello`."""
        pt = ProcessTask(path="echo", args=["hello"])
        start = time.perf_counter()
        for _ in range(100):
            import asyncio
            q: asyncio.Queue[str] = asyncio.Queue()
            success = await pt._run_process_bg(q)
            # Drain queue
            while not q.empty():
                await q.get()
        elapsed = time.perf_counter() - start
        assert elapsed < 15.0

    @pytest.mark.asyncio
    async def test_with_stderr(self) -> None:
        """Run command that produces stderr."""
        pt = ProcessTask(path="python", args=["-c", "import sys; print('stdout'); print('stderr', file=sys.stderr)"])
        start = time.perf_counter()
        for _ in range(100):
            import asyncio
            q: asyncio.Queue[str] = asyncio.Queue()
            success = await pt._run_process_bg(q)
            while not q.empty():
                await q.get()
        elapsed = time.perf_counter() - start
        assert elapsed < 15.0
