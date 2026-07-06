"""Comprehensive async/await tests for tools using BackgroundStream and ProcessTask."""

import asyncio
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest

from kimi_agent_sdk import ToolOk, ToolError
from kimix.tools.background.utils import (
    BackgroundStream,
    add_task,
    discard_all_tasks,
    generate_task_id,
    get_all_tasks,
    remove_task_id,
)
from kimix.tools.background import TaskOutput, TaskOutputParams
from kimix.tools.py import Python, Params as PyParams


@pytest.fixture
def mock_session() -> MagicMock:
    session = MagicMock()
    session.custom_data = {}
    return session


@pytest.fixture(autouse=True)
async def cleanup_task_data(mock_session: MagicMock) -> Any:
    yield
    await discard_all_tasks(mock_session)


@pytest.fixture(autouse=True)
def patch_find_bash() -> Any:
    with patch("kimix.tools.file.bash.bash_tool.find_bash", return_value=None):
        yield


# ---------------------------------------------------------------------------
# TaskList tool (via TaskOutput with task_id=None)
# ---------------------------------------------------------------------------
class TestTaskList:
    async def test_empty(self, mock_session: MagicMock) -> None:
        tool = TaskOutput(session=mock_session)
        result = await tool(TaskOutputParams(task_id=None))
        assert "No running task" in str(result.output)

    async def test_lists_tasks(self, mock_session: MagicMock) -> None:
        tool = TaskOutput(session=mock_session)
        stream = BackgroundStream()

        def worker(q: queue.Queue[str]) -> None:
            q.put("hello")

        await stream.start(worker, stop_function=lambda: None)
        add_task(mock_session, "run_test", stream)
        result = await tool(TaskOutputParams(task_id=None))
        await stream.wait()
        assert "run_test" in str(result.output)


# ---------------------------------------------------------------------------
# TaskOutput tool
# ---------------------------------------------------------------------------
class TestTaskOutput:
    async def test_not_found(self, mock_session: MagicMock) -> None:
        tool = TaskOutput(session=mock_session)
        result = await tool(TaskOutputParams(task_id="missing"))
        assert "No running task" in str(result.message)

    async def test_wait_and_get_output(self, mock_session: MagicMock) -> None:
        tool = TaskOutput(session=mock_session)
        stream = BackgroundStream()

        def worker(q: queue.Queue[str]) -> None:
            q.put("output_line")
            time.sleep(0.05)

        await stream.start(worker, stop_function=lambda: None)
        add_task(mock_session, "run_test", stream)

        result = await tool(TaskOutputParams(task_id="run_test", block=True, timeout=5))
        assert "output_line" in str(result.output)
        assert "run_test" not in get_all_tasks(mock_session)

    async def test_kill_running_task(self, mock_session: MagicMock) -> None:
        tool = TaskOutput(session=mock_session)
        stream = BackgroundStream()

        def worker(q: queue.Queue[str]) -> None:
            time.sleep(10)

        await stream.start(worker, stop_function=lambda: None)
        add_task(mock_session, "run_test", stream)

        result = await tool(TaskOutputParams(task_id="run_test", block=False, kill=True))
        await stream.wait()
        assert "run_test" not in get_all_tasks(mock_session)

    async def test_export_to_file(self, mock_session: MagicMock, tmp_path: Path) -> None:
        tool = TaskOutput(session=mock_session)
        stream = BackgroundStream()

        def worker(q: queue.Queue[str]) -> None:
            q.put("file_content")

        await stream.start(worker, stop_function=lambda: None)
        add_task(mock_session, "run_test", stream)

        out_path = tmp_path / "out.txt"
        result = await tool(TaskOutputParams(task_id="run_test", block=True, timeout=5, output_path=str(out_path)))
        await stream.wait()
        assert out_path.exists()
        assert "file_content" in out_path.read_text(encoding="utf-8")
        assert "exported to file" in str(result.output)

    async def test_running_task_returns_inline_output(self, mock_session: MagicMock) -> None:
        """A running task should return its accumulated output inline,
        not unconditionally export it to a temporary file.
        """
        tool = TaskOutput(session=mock_session)
        stream = BackgroundStream()

        def worker(q: queue.Queue[str]) -> None:
            q.put("hello_running")
            time.sleep(10)

        await stream.start(worker, stop_function=lambda: None)
        add_task(mock_session, "run_test", stream)

        result = await tool(TaskOutputParams(task_id="run_test", block=False))
        assert "hello_running" in str(result.output)
        assert "exported to file" not in str(result.output)

        # cleanup
        await stream.stop()
        await stream.wait()


# ---------------------------------------------------------------------------
# Python tool
# ---------------------------------------------------------------------------
class TestPython:
    async def test_foreground_success(self, mock_session: MagicMock) -> None:
        tool = Python(session=mock_session)
        params = PyParams(code="print('hello_py')", timeout=10)
        result = await tool(params)
        assert "hello_py" in str(result.output)

    async def test_foreground_failure(self, mock_session: MagicMock) -> None:
        tool = Python(session=mock_session)
        params = PyParams(code="import sys; sys.exit(1)", timeout=10)
        result = await tool(params)
        assert "failed" in str(result.message).lower() or "exited" in str(result.output).lower()

    async def test_foreground_timeout(self, mock_session: MagicMock) -> None:
        tool = Python(session=mock_session)
        params = PyParams(code="import time; time.sleep(100)", timeout=3)
        result = await tool(params)
        assert "timeout" in str(result.message).lower() or "background" in str(result.message).lower()
        # cleanup
        for tid in list(get_all_tasks(mock_session).keys()):
            remove_task_id(mock_session, tid)

    async def test_dest_export(self, mock_session: MagicMock, tmp_path: Path) -> None:
        tool = Python(session=mock_session)
        dest = tmp_path / "py_out.txt"
        params = PyParams(code="print('dest_out')", timeout=10, output_path=str(dest))
        result = await tool(params)
        assert dest.exists()
        assert "dest_out" in dest.read_text(encoding="utf-8")
        assert "exported to" in str(result.output)

    async def test_inactivity_timeout_returns_background_error(
        self, mock_session: MagicMock
    ) -> None:
        with patch(
            "kimix.tools.background.utils.DEFAULT_INACTIVITY_TIMEOUT", 2.0
        ):
            tool = Python(session=mock_session)
            params = PyParams(code="import time; time.sleep(120)", timeout=90)
            result = await tool(params)
            assert isinstance(result, ToolError)
            assert result.brief == "Timeout"
            assert "Running in background" in result.message
            assert "task_id" in result.message



# ---------------------------------------------------------------------------
# Async syntax / integration smoke tests
# ---------------------------------------------------------------------------
class TestAsyncIntegration:
    async def test_background_stream_awaited_methods(self) -> None:
        stream = BackgroundStream()
        # All these are declared async; calling them with await should work even
        # if the underlying implementation is synchronous.
        assert await stream.is_started() is False
        assert await stream.is_stopped() is False
        assert await stream.thread_is_alive() is False
        assert await stream.success() is False
        assert await stream.get_output() == ""
        assert await stream.get_queue() is None

    async def test_process_task_all_async_methods_awaited(self, mock_session: MagicMock) -> None:
        from kimix.tools.common import ProcessTask

        task = ProcessTask(sys.executable, ["-c", "print('await_test')"])
        tid = await task.start(mock_session, kind="run", name="await")
        assert await task.thread_is_alive() is True
        await task.wait(timeout=5)
        assert await task.thread_is_alive() is False
        assert await task.stream.success() is True
        output = await task.stream.pop_output()
        assert "await_test" in output
        remove_task_id(mock_session, tid)

    async def test_concurrent_task_outputs(self, mock_session: MagicMock) -> None:
        stream1 = BackgroundStream()
        stream2 = BackgroundStream()

        def w1(q: queue.Queue[str]) -> None:
            q.put("a")

        def w2(q: queue.Queue[str]) -> None:
            q.put("b")

        await stream1.start(w1, stop_function=lambda: None)
        await stream2.start(w2, stop_function=lambda: None)
        add_task(mock_session, "t1", stream1)
        add_task(mock_session, "t2", stream2)

        out1, out2 = await asyncio.gather(
            stream1.get_output(),
            stream2.get_output(),
        )
        assert "a" in out1
        assert "b" in out2

        await stream1.wait()
        await stream2.wait()
        remove_task_id(mock_session, "t1")
        remove_task_id(mock_session, "t2")
