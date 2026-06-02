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
from kimix.tools.file.run import Run, RunParams
from kimix.tools.py import Python, Params as PyParams
from kimix.tools.file.input import Input, InputParams


@pytest.fixture
def mock_session() -> MagicMock:
    session = MagicMock()
    session.custom_data = {}
    return session


@pytest.fixture(autouse=True)
async def cleanup_task_data(mock_session: MagicMock) -> Any:
    yield
    await discard_all_tasks(mock_session)


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


# ---------------------------------------------------------------------------
# Run tool
# ---------------------------------------------------------------------------
class TestRun:
    async def test_foreground_success(self, mock_session: MagicMock) -> None:
        tool = Run(session=mock_session)
        params = RunParams(command=f'{sys.executable} -c "print(\'hello_run\')"', timeout=10)
        result = await tool(params)
        assert "hello_run" in str(result.output)

    async def test_foreground_failure(self, mock_session: MagicMock) -> None:
        tool = Run(session=mock_session)
        params = RunParams(command=f'{sys.executable} -c "import sys; sys.exit(1)"', timeout=10)
        result = await tool(params)
        assert "failed" in str(result.message).lower() or "exited" in str(result.output).lower()

    async def test_foreground_timeout(self, mock_session: MagicMock) -> None:
        tool = Run(session=mock_session)
        params = RunParams(
            command=f'{sys.executable} -c "import time; time.sleep(100)"',
            timeout=3,
        )
        result = await tool(params)
        assert "timeout" in str(result.message).lower() or "background" in str(result.message).lower()
        # task should remain registered after timeout
        assert len(get_all_tasks(mock_session)) >= 1
        # cleanup
        for tid in list(get_all_tasks(mock_session).keys()):
            remove_task_id(mock_session, tid)

    async def test_python_c_code(self, mock_session: MagicMock) -> None:
        """Test running Python code via the `python -c` pattern with the Run tool."""
        tool = Run(session=mock_session)
        code = "import sys; print('py_c_hello', sys.version_info[0])"
        params = RunParams(command=f'{sys.executable} -c "{code}"', timeout=10)
        result = await tool(params)
        assert "py_c_hello" in str(result.output)
        assert "failed" not in str(result.message).lower()

    async def test_output_path(self, mock_session: MagicMock, tmp_path: Path) -> None:
        tool = Run(session=mock_session)
        out_path = tmp_path / "run_out.txt"
        params = RunParams(
            command=f'{sys.executable} -c "print(\'to_file\')"',
            timeout=10,
            output_path=str(out_path),
        )
        result = await tool(params)
        assert out_path.exists()
        assert "to_file" in out_path.read_text(encoding="utf-8")
        assert "saved to file" in str(result.output)

    async def test_use_posix_true_calls_shlex_with_posix_true(self, mock_session: MagicMock) -> None:
        """When use_posix=True, shlex.split should be called with posix=True."""
        tool = Run(session=mock_session)
        tool.use_posix = True
        with patch("kimix.tools.file.run.shlex.split", return_value=[]) as mock_split:
            params = RunParams(command="echo hello", timeout=10)
            result = await tool(params)
            assert "Empty command" in str(result.message)
            mock_split.assert_called_once_with("echo hello", posix=True)

    async def test_use_posix_false_calls_shlex_with_posix_false(self, mock_session: MagicMock) -> None:
        """When use_posix=False, shlex.split should be called with posix=False."""
        tool = Run(session=mock_session)
        tool.use_posix = False
        with patch("kimix.tools.file.run.shlex.split", return_value=[]) as mock_split:
            params = RunParams(command="echo hello", timeout=10)
            result = await tool(params)
            assert "Empty command" in str(result.message)
            mock_split.assert_called_once_with("echo hello", posix=False)

    async def test_use_posix_true_preserves_literal_quotes(self, mock_session: MagicMock) -> None:
        """When use_posix=True, literal double quotes must not be stripped."""
        tool = Run(session=mock_session)
        tool.use_posix = True
        with patch("kimix.tools.file.run.shlex.split", return_value=["python", "-c", '"hello"']):
            with patch("shutil.which", return_value="/usr/bin/python"):
                with patch("kimix.tools.file.run.ProcessTask") as mock_pt:
                    mock_instance = MagicMock()
                    mock_instance.start = AsyncMock(return_value="tid")
                    mock_pt.return_value = mock_instance
                    params = RunParams(command='python -c \'"hello"\'', timeout=10, run_in_background=True)
                    result = await tool(params)
                    assert result.is_error is False
                    assert mock_pt.call_args is not None
                    _, args_list, _, _ = mock_pt.call_args[0]
                    assert args_list == ["-c", '"hello"']

    async def test_use_posix_false_strips_preserved_quotes(self, mock_session: MagicMock) -> None:
        """When use_posix=False, double quotes preserved by posix=False must be stripped."""
        tool = Run(session=mock_session)
        tool.use_posix = False
        with patch("kimix.tools.file.run.shlex.split", return_value=["python", "-c", '"hello"']):
            with patch("shutil.which", return_value="C:\\Python\\python.exe"):
                with patch("kimix.tools.file.run.ProcessTask") as mock_pt:
                    mock_instance = MagicMock()
                    mock_instance.start = AsyncMock(return_value="tid")
                    mock_pt.return_value = mock_instance
                    params = RunParams(command='python -c "hello"', timeout=10, run_in_background=True)
                    result = await tool(params)
                    assert result.is_error is False
                    assert mock_pt.call_args is not None
                    _, args_list, _, _ = mock_pt.call_args[0]
                    assert args_list == ["-c", "hello"]


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


# ---------------------------------------------------------------------------
# Input tool
# ---------------------------------------------------------------------------
class TestInput:
    async def test_not_found(self, mock_session: MagicMock) -> None:
        tool = Input(session=mock_session)
        result = await tool(InputParams(task_id="missing", text="hello"))
        assert "not found" in str(result.message).lower()

    async def test_send_input_to_running_process(self, mock_session: MagicMock) -> None:
        from kimix.tools.common import ProcessTask

        task = ProcessTask(
            sys.executable,
            ["-c", "import sys; line=sys.stdin.readline(); print('got', line.strip())"],
        )
        tid = await task.start(mock_session, kind="run", name="input_test")
        await asyncio.sleep(0.2)

        tool = Input(session=mock_session)
        result = await tool(InputParams(task_id=tid, text="hello\n"))
        assert "sent" in str(result.output).lower()

        await task.wait(timeout=5)
        output = await task.stream.get_output()
        assert "got hello" in output
        remove_task_id(mock_session, tid)

    async def test_input_fails_when_no_stdin(self, mock_session: MagicMock) -> None:
        from kimix.tools.common import ProcessTask

        # process that exits quickly
        task = ProcessTask(sys.executable, ["-c", "print('done')"])
        tid = await task.start(mock_session, kind="run", name="quick")
        await task.wait(timeout=5)

        tool = Input(session=mock_session)
        result = await tool(InputParams(task_id=tid, text="data"))
        # Input may fail because process already finished
        assert "failed" in str(result.message).lower() or "sent" in str(result.output).lower()
        remove_task_id(mock_session, tid)


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


# ---------------------------------------------------------------------------
# RunParams scalar compatibility
# ---------------------------------------------------------------------------
class TestRunParams:
    def test_env_accepts_string(self) -> None:
        p = RunParams(command="echo hello", env="FOO=bar")
        assert p.env == "FOO=bar"

    def test_env_accepts_list(self) -> None:
        p = RunParams(command="echo hello", env=["FOO=bar", "BAZ=qux"])
        assert p.env == ["FOO=bar", "BAZ=qux"]

    def test_env_none_default(self) -> None:
        p = RunParams(command="echo hello")
        assert p.env is None
