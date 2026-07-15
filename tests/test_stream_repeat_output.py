"""Tests to verify that stream output is not repeated across calls.

The bug: BackgroundStream.get_output() accumulates data in _output without
clearing. After wait_for_output() returns accumulated output, the internal
_output buffer still has the same data. Subsequent calls (e.g. TaskOutput)
return the same output again instead of only fresh data.

These tests verify the fix.
"""

import asyncio
import queue
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kimix.tools.background.utils import (
    BackgroundStream,
    _pop_task_data,
)

from kimix.tools.background import TaskOutput
from kimix.tools.common import ProcessTask


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def stream() -> BackgroundStream:
    return BackgroundStream()


@pytest.fixture
def mock_session() -> MagicMock:
    session = MagicMock()
    session.custom_data = {}
    return session


@pytest.fixture(autouse=True)
def cleanup_task_data(mock_session: MagicMock) -> Any:
    yield
    _pop_task_data(mock_session)


# ---------------------------------------------------------------------------
# Test: BackgroundStream.get_output() does NOT repeat after pop_output()
# ---------------------------------------------------------------------------

async def test_get_output_does_not_repeat_after_pop(stream: BackgroundStream) -> None:
    """After pop_output() clears the buffer, get_output() returns empty."""
    def worker(q: queue.Queue[str]) -> None:
        q.put("hello")

    await stream.start(worker, stop_function=lambda: None)
    await stream.wait()
    
    # First read: returns "hello" and clears buffer
    first = await stream.pop_output()
    assert first == "hello"
    
    # Second read: should be empty (no new data)
    second = await stream.get_output()
    assert second == ""


# ---------------------------------------------------------------------------
# Test: wait_for_output should not leave stale data in _output
# ---------------------------------------------------------------------------

async def test_wait_for_output_clears_internal_buffer(stream: BackgroundStream) -> None:
    """After wait_for_output returns, the internal _output buffer should
    not contain the returned data, so subsequent get_output() calls don't
    repeat the same output."""
    def worker(q: queue.Queue[str]) -> None:
        q.put("data_chunk_1")
        time.sleep(0.05)
        q.put("data_chunk_2")

    await stream.start(worker, stop_function=lambda: None)
    
    # Call wait_for_output - this should accumulate and return output
    output, matched, elapsed = await stream.wait_for_output(timeout=2.0)
    assert "data_chunk_1" in output
    assert "data_chunk_2" in output
    
    # After wait_for_output returns, get_output() should NOT return the same data.
    # It should only return new data that arrived after wait_for_output finished.
    # Since the worker has finished, new_get should be "" or only contain
    # any final flush data, but NOT "data_chunk_1" or "data_chunk_2".
    new_output = await stream.get_output()
    assert "data_chunk_1" not in new_output, (
        f"wait_for_output left stale data in buffer. "
        f"Expected 'data_chunk_1' NOT in new_output='{new_output}'"
    )
    assert "data_chunk_2" not in new_output, (
        f"wait_for_output left stale data in buffer. "
        f"Expected 'data_chunk_2' NOT in new_output='{new_output}'"
    )


# ---------------------------------------------------------------------------
# Test: wait_for_output followed by pop_output should not lose new data
# ---------------------------------------------------------------------------

async def test_wait_for_output_then_pop_output_gets_only_new_data(stream: BackgroundStream) -> None:
    """After wait_for_output returns, a subsequent pop_output() should
    return only data that arrived after wait_for_output completed, not
    the data already returned by wait_for_output."""
    
    received_continue = asyncio.Event()
    
    def worker(q: queue.Queue[str]) -> None:
        q.put("initial_data")
        time.sleep(0.1)
        q.put("more_data")
        time.sleep(0.1)
    
    await stream.start(worker, stop_function=lambda: None)
    
    # Wait for all output
    output, matched, elapsed = await stream.wait_for_output(timeout=2.0)
    assert "initial_data" in output
    assert "more_data" in output
    
    # pop_output should return empty or only data after wait_for_output
    new_output = await stream.pop_output()
    # Worker is done, queue has been fully drained by wait_for_output.
    # pop_output should NOT return "initial_data" or "more_data" again.
    assert "initial_data" not in new_output, (
        f"pop_output returned stale data. "
        f"Expected 'initial_data' NOT in new_output='{new_output}'"
    )
    assert "more_data" not in new_output, (
        f"pop_output returned stale data. "
        f"Expected 'more_data' NOT in new_output='{new_output}'"
    )


# ---------------------------------------------------------------------------
# Test: TaskOutput repeats output (the original bug scenario)
# ---------------------------------------------------------------------------

async def test_task_output_does_not_repeat_output(mock_session: MagicMock) -> None:
    """Simulate the scenario where:
    1. A process runs and produces output
    2. wait_for_output is called and returns the output
    3. get_output() is then called - it should NOT return the same output
    """
    def worker(q: queue.Queue[str]) -> None:
        q.put("process_starting")
        time.sleep(0.05)
        q.put("ready_prompt>")
    
    stream = BackgroundStream()
    await stream.start(worker, stop_function=lambda: None)
    
    # Call wait_for_output to get all output (simulates first interactive call)
    output, matched, elapsed = await stream.wait_for_output(
        timeout=2.0, pattern=None
    )
    assert "process_starting" in output
    assert "ready_prompt>" in output
    
    # After wait_for_output returns, the internal buffer should be cleared.
    # get_output() should return empty or only data that arrived after
    # wait_for_output completed.
    await stream.wait(timeout=0.5)
    after_output = await stream.get_output()
    assert "process_starting" not in after_output, (
        f"BUG: get_output returned stale data after wait_for_output. "
        f"Got: '{after_output}'"
    )
    assert "ready_prompt>" not in after_output, (
        f"BUG: get_output returned stale data after wait_for_output. "
        f"Got: '{after_output}'"
    )
    
    # pop_output should also be clean
    pop_output = await stream.pop_output()
    assert "process_starting" not in pop_output, (
        f"BUG: pop_output returned stale data after wait_for_output. "
        f"Got: '{pop_output}'"
    )
    
    # get_output after pop_output should be empty
    final_output = await stream.get_output()
    assert final_output == "" or "process_starting" not in final_output


# ---------------------------------------------------------------------------
# Test: Interactive process - consecutive reads return only new data
# ---------------------------------------------------------------------------

async def test_wait_for_output_with_pattern_clears_buffer(mock_session: MagicMock) -> None:
    """After wait_for_output returns data (matching a pattern or timing out),
    a subsequent pop_output() should NOT return the same data again."""
    
    def worker(q: queue.Queue[str]) -> None:
        q.put("initial_greeting")
        time.sleep(0.1)
        q.put("prompt>")
        time.sleep(0.1)
        q.put("final_message")
        # Keep running
        time.sleep(10)
    
    stream = BackgroundStream()
    await stream.start(worker, stop_function=lambda: None)
    
    # Use wait_for_output with a pattern to stop at the prompt
    import re
    result1, matched, _ = await stream.wait_for_output(
        timeout=5.0, pattern=re.compile(r"prompt>")
    )
    assert matched, "wait_for_output should have matched 'prompt>'"
    assert "initial_greeting" in result1
    assert "prompt>" in result1
    assert "final_message" not in result1  # hasn't arrived yet
    
    # Now pop_output should return ONLY data that arrived after
    # wait_for_output returned, NOT the data already seen in result1.
    # final_message should be in the next read.
    await asyncio.sleep(0.2)  # Wait for final_message to arrive
    later_output = await stream.pop_output()
    assert "final_message" in later_output, (
        f"Expected final_message in later_output, got: '{later_output}'"
    )
    assert "initial_greeting" not in later_output, (
        f"BUG: pop_output returned stale data after wait_for_output. "
        f"Got: '{later_output}'"
    )
    assert "prompt>" not in later_output, (
        f"BUG: pop_output returned stale data after wait_for_output. "
        f"Got: '{later_output}'"
    )
    
    # Cleanup
    await stream.stop()
