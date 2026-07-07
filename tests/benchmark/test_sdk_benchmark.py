"""Performance benchmarks for kimi_agent_sdk.

All timings are assert-based so the file doubles as a regression test.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kimi_agent_sdk._aggregator import MessageAggregator
from kimi_agent_sdk._session import _load_config_json, _resolve_skills_dirs

pytestmark = pytest.mark.slow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_text_part(text: str) -> object:
    """Create a minimal TextPart-like WireMessage."""
    from dataclasses import dataclass
    @dataclass
    class TextPart:
        text: str = ""
        type: str = "text"

        def merge_in_place(self, other: object) -> bool:
            if isinstance(other, TextPart):
                self.text += other.text
                return True
            return False

    return TextPart(text=text)


def _make_tool_call(id: str = "call_1") -> object:
    """Create a minimal ToolCall-like WireMessage."""
    from dataclasses import dataclass, field

    @dataclass
    class Function:
        name: str = "test"
        arguments: str = "{}"

    @dataclass
    class ToolCall:
        id: str = "call_1"
        function: object = field(default_factory=lambda: Function())
        type: str = "function"

        def merge_in_place(self, part: object) -> None:
            if hasattr(part, 'arguments_part') and part.arguments_part:
                self.function.arguments += part.arguments_part

    return ToolCall(id=id)


def _make_tool_call_part(arguments_part: str = "more_args") -> object:
    """Create a minimal ToolCallPart-like WireMessage."""
    from dataclasses import dataclass
    @dataclass
    class ToolCallPart:
        arguments_part: str = ""

    return ToolCallPart(arguments_part=arguments_part)


def _make_tool_result(tool_call_id: str = "call_1") -> object:
    """Create a minimal ToolResult-like WireMessage."""
    from dataclasses import dataclass, field
    @dataclass
    class ReturnValue:
        is_error: bool = False
        message: str = "ok"
        display: list = field(default_factory=list)

    @dataclass
    class ToolResult:
        tool_call_id: str = "call_1"
        return_value: object = field(default_factory=lambda: ReturnValue())

    return ToolResult(tool_call_id=tool_call_id)


def _make_step_begin() -> object:
    """Create a minimal StepBegin-like WireMessage."""
    from dataclasses import dataclass
    @dataclass
    class StepBegin:
        type: str = "step_begin"

    return StepBegin()


# ---------------------------------------------------------------------------
# MessageAggregator benchmarks
# ---------------------------------------------------------------------------


class TestMessageAggregatorBenchmark:
    """Benchmarks for MessageAggregator."""

    def test_feed_text_stream(self) -> None:
        """MessageAggregator.feed() — TextPart stream."""
        aggregator = MessageAggregator()
        parts = [_make_text_part(f"chunk_{i} ") for i in range(100)]

        start = time.perf_counter()
        for _ in range(50_000):
            for part in parts:
                aggregator.feed(part)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_feed_tool_cycle(self) -> None:
        """Full tool-call then tool-result cycle."""
        aggregator = MessageAggregator()
        call = _make_tool_call("call_1")
        call_part = _make_tool_call_part('{"cmd": "echo hello"}')
        result = _make_tool_result("call_1")

        start = time.perf_counter()
        for _ in range(10_000):
            aggregator.feed(call)
            aggregator.feed(call_part)
            aggregator.feed(result)
            aggregator.flush()
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_flush_full(self) -> None:
        """_flush_full() with 10 tool calls."""
        aggregator = MessageAggregator()

        # Build up state
        for i in range(10):
            aggregator.feed(_make_tool_call(f"call_{i}"))
            aggregator.feed(_make_tool_call_part(f"args_{i}"))
            aggregator.feed(_make_tool_result(f"call_{i}"))

        start = time.perf_counter()
        for _ in range(10_000):
            aggregator.flush()
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_flush_final_only(self) -> None:
        """_flush_final_only() overhead."""
        aggregator = MessageAggregator(final_message_only=True)

        # Build up state with text
        for i in range(50):
            aggregator.feed(_make_text_part(f"chunk_{i} "))

        start = time.perf_counter()
        for _ in range(10_000):
            aggregator.flush()
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_reset_buffers_overhead(self) -> None:
        """Buffer reset overhead."""
        aggregator = MessageAggregator()

        start = time.perf_counter()
        for _ in range(50_000):
            aggregator.feed(_make_step_begin())
        elapsed = time.perf_counter() - start
        # Already uses .clear() — threshold accounts for pattern-matching dispatch overhead
        assert elapsed < 10.0


# ---------------------------------------------------------------------------
# Session helper benchmarks (mock-based)
# ---------------------------------------------------------------------------


class TestSessionHelpersBenchmark:
    """Benchmarks for session helper functions."""

    @pytest.mark.asyncio
    async def test_load_config_json(self, tmp_path: Path) -> None:
        """_load_config_json() on temp .kimix/config.json."""
        from kaos.path import KaosPath
        import anyio

        # Create a temp config
        config_dir = tmp_path / ".kimix"
        config_dir.mkdir()
        await anyio.Path(config_dir / "config.json").write_text(
            '{"key": "value", "nested": {"inner": 42}}'
        )

        work_dir = KaosPath.unsafe_from_local_path(tmp_path)

        start = time.perf_counter()
        for _ in range(1_000):
            result = await _load_config_json(work_dir)
            assert "config_json" in result
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_resolve_skills_dirs(self) -> None:
        """_resolve_skills_dirs() throughput."""
        from kaos.path import KaosPath

        skills_dir = KaosPath.unsafe_from_local_path(Path("/tmp/skills"))
        skills_dirs = [
            KaosPath.unsafe_from_local_path(Path(f"/tmp/skills/{i}"))
            for i in range(20)
        ]

        start = time.perf_counter()
        for _ in range(10_000):
            _resolve_skills_dirs(skills_dir, skills_dirs)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0
