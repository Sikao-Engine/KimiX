"""Sub-process "spent time" reporting for bash / python / pwsh / job_output.

Every one of those tools reports how long the child process spent running in
the tool *message* — for success **and** failure — while staying silent when
the child finished in under a second (the timing of a fast command is noise).
"""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kimi_agent_sdk import ToolError, ToolOk

from kimix.tools.background import TaskOutput, TaskOutputParams
from kimix.tools.background.utils import BackgroundStream, add_task
from kimix.tools.common import (
    ELAPSED_REPORT_MINIMUM_SECONDS,
    ProcessTask,
    _append_elapsed,
    _elapsed_suffix,
    _elapsed_tag,
    _format_elapsed_seconds,
    _subprocess_elapsed,
)
from kimix.tools.file.bash import Powershell
from kimix.tools.file.bash.bash_tool import Bash, BashParams
from kimix.tools.file.bash.pwsh_tool import PowershellParams
from kimix.tools.py import Params as PythonParams
from kimix.tools.py import python


# ── Shared helpers ────────────────────────────────────────────────────────


def _fake_process_task(
    *,
    elapsed: float | None,
    success: bool = True,
    exit_code: int = 0,
    output: str = "mock output",
) -> MagicMock:
    """Fake ProcessTask behaving like a finished one-shot child."""
    task = MagicMock()
    task.start = AsyncMock(return_value="fake-id")
    task.wait_with_monitor = AsyncMock(return_value=(True, 0.0, False))
    task.thread_is_alive = AsyncMock(return_value=False)
    task.stream = MagicMock()
    task.stream.pop_output = AsyncMock(return_value=output)
    task.stream.success = AsyncMock(return_value=success)
    task.stream.exit_code = exit_code
    task.stream.process_elapsed = elapsed
    return task


# ── Duration formatting / threshold helpers ───────────────────────────────


class TestElapsedFormatting:
    def test_sub_second_uses_hundredths(self) -> None:
        assert _format_elapsed_seconds(0.5) == "0.50s"
        assert _format_elapsed_seconds(12.34) == "12.34s"
        assert _format_elapsed_seconds(59.99) == "59.99s"

    def test_minutes_and_hours(self) -> None:
        assert _format_elapsed_seconds(65.0) == "1m05s"
        assert _format_elapsed_seconds(3600.0) == "1h00m"
        assert _format_elapsed_seconds(7325.0) == "2h02m"

    def test_unknown_durations_render_empty(self) -> None:
        assert _format_elapsed_seconds(None) == ""
        assert _format_elapsed_seconds("1.5") == ""
        assert _format_elapsed_seconds(MagicMock()) == ""
        assert _format_elapsed_seconds(float("nan")) == ""
        assert _format_elapsed_seconds(float("inf")) == ""
        assert _format_elapsed_seconds(-3) == ""
        assert _format_elapsed_seconds(True) == ""


class TestElapsedSuffixAndTag:
    def test_fast_processes_are_silent(self) -> None:
        assert _elapsed_suffix(0.0) == ""
        assert _elapsed_suffix(0.99) == ""
        assert _elapsed_suffix(None) == ""

    def test_threshold_is_one_second(self) -> None:
        assert ELAPSED_REPORT_MINIMUM_SECONDS == 1.0
        assert _elapsed_suffix(1.0) == " (1.00s)"
        assert _elapsed_suffix(1.23) == " (1.23s)"

    def test_tag_shares_the_threshold(self) -> None:
        assert _elapsed_tag(0.4) == ""
        assert _elapsed_tag(2.5) == "[Process completed in 2.50s]"
        assert _elapsed_tag(90.0, label="Ran for") == "[Ran for 1m30s]"


class TestAppendElapsed:
    def test_slow_process_gets_a_suffix(self) -> None:
        assert _append_elapsed("success", 2.0) == "success (2.00s)"
        assert _append_elapsed("failed", 65.0) == "failed (1m05s)"

    def test_fast_process_keeps_message_unchanged(self) -> None:
        assert _append_elapsed("success", 0.4) == "success"
        assert _append_elapsed("failed", None) == "failed"

    def test_empty_message_only_gets_the_timing(self) -> None:
        assert _append_elapsed("", 3.0) == "(3.00s)"
        assert _append_elapsed("", 0.1) == ""

    def test_idempotent_when_message_already_annotated(self) -> None:
        once = _append_elapsed("success", 2.0)
        assert _append_elapsed(once, 2.0) == once
        assert _append_elapsed("failed [command saved to x.sh] (1.50s)", 9.9) == (
            "failed [command saved to x.sh] (1.50s)"
        )


class TestSubprocessElapsed:
    def test_prefers_the_recorded_runtime(self) -> None:
        stream = MagicMock()
        stream.process_elapsed = 2.5
        assert _subprocess_elapsed(stream, 0.1) == 2.5

    def test_falls_back_to_the_wait_measurement(self) -> None:
        stream = MagicMock()
        stream.process_elapsed = None
        assert _subprocess_elapsed(stream, None, 1.25) == 1.25
        assert _subprocess_elapsed(None, 0.5) == 0.5

    def test_ignores_non_numeric_placeholders(self) -> None:
        stream = MagicMock()  # `process_elapsed` is an auto-created mock object
        assert _subprocess_elapsed(stream, "nope", None) is None


# ── The process machinery records the real runtime ────────────────────────


class TestProcessTaskRecordsRuntime:
    async def _run(self, code: str) -> float | None:
        session = MagicMock()
        session.custom_data = {}
        task = ProcessTask(sys.executable, ["-c", code])
        await task.start(session, "python")
        completed, _waited, _inactivity = await task.wait_with_monitor(30)
        assert completed
        elapsed = task.stream.process_elapsed
        assert elapsed is not None
        return elapsed

    async def test_slow_process_records_a_reportable_runtime(self) -> None:
        import time

        started = time.monotonic()
        elapsed = await self._run("import time; time.sleep(1.2)")
        assert elapsed >= 1.0
        # The recorded runtime is the child's life span, not a fixed constant.
        assert elapsed <= time.monotonic() - started
        assert _elapsed_suffix(elapsed) != ""

    async def test_fast_process_records_a_sub_second_runtime(self) -> None:
        elapsed = await self._run("pass")
        assert elapsed < 1.0
        # ... which is exactly why the tools stay silent about it.
        assert _elapsed_suffix(elapsed) == ""


# ── bash ──────────────────────────────────────────────────────────────────


@pytest.fixture
def bash_tool(mock_session: MagicMock) -> Bash:
    with patch(
        "kimix.tools.file.bash.bash_tool.find_bash", return_value=r"C:\Git\bin\bash.exe"
    ), patch("kimix.tools.file.bash.bash_tool._should_enable_bash", return_value=True):
        return Bash(session=mock_session)


class TestBashSpentTime:
    async def test_success_message_reports_spent_time(self, bash_tool: Bash) -> None:
        task = _fake_process_task(elapsed=2.0)
        with patch("kimix.tools.file.bash.bash_tool.ProcessTask", return_value=task):
            result = await bash_tool(BashParams(cmd="echo hi"))

        assert isinstance(result, ToolOk)
        assert "success" in result.message
        assert result.message.endswith("(2.00s)")
        assert "elapsed_seconds: 2.00" in result.output

    async def test_failure_message_reports_spent_time(self, bash_tool: Bash) -> None:
        task = _fake_process_task(elapsed=3.5, success=False, exit_code=1)
        with patch("kimix.tools.file.bash.bash_tool.ProcessTask", return_value=task):
            result = await bash_tool(BashParams(cmd="exit 1"))

        assert isinstance(result, ToolError)
        assert "failed" in result.message
        assert result.message.endswith("(3.50s)")

    async def test_fast_command_is_not_annotated(self, bash_tool: Bash) -> None:
        task = _fake_process_task(elapsed=0.35)
        with patch("kimix.tools.file.bash.bash_tool.ProcessTask", return_value=task):
            result = await bash_tool(BashParams(cmd="echo hi"))

        assert isinstance(result, ToolOk)
        assert result.message.endswith("success")
        assert "(0.35s)" not in result.message

    async def test_unmeasured_command_is_not_annotated(self, bash_tool: Bash) -> None:
        task = _fake_process_task(elapsed=None)
        with patch("kimix.tools.file.bash.bash_tool.ProcessTask", return_value=task):
            result = await bash_tool(BashParams(cmd="echo hi"))

        assert isinstance(result, ToolOk)
        assert "s)" not in result.message


# ── pwsh ──────────────────────────────────────────────────────────────────


@pytest.fixture
def pwsh_tool(mock_session: MagicMock) -> Powershell:
    with patch(
        "kimix.tools.file.bash.pwsh_tool._bash_tool._should_enable_powershell",
        return_value=True,
    ), patch("kimix.tools.file.bash.pwsh_tool.find_pwsh", return_value=r"C:\pwsh\pwsh.exe"):
        return Powershell(session=mock_session)


class TestPwshSpentTime:
    async def test_success_message_reports_spent_time(self, pwsh_tool: Powershell) -> None:
        task = _fake_process_task(elapsed=1.5)
        with patch("kimix.tools.file.bash.pwsh_tool.ProcessTask", return_value=task):
            result = await pwsh_tool(PowershellParams(cmd="Get-Location"))

        assert isinstance(result, ToolOk)
        assert "success" in result.message
        assert result.message.endswith("(1.50s)")
        assert "elapsed_seconds: 1.50" in result.output

    async def test_failure_message_reports_spent_time(self, pwsh_tool: Powershell) -> None:
        task = _fake_process_task(elapsed=7.25, success=False, exit_code=1)
        with patch("kimix.tools.file.bash.pwsh_tool.ProcessTask", return_value=task):
            result = await pwsh_tool(PowershellParams(cmd="throw 'boom'"))

        assert isinstance(result, ToolError)
        assert "failed" in result.message
        assert result.message.endswith("(7.25s)")

    async def test_fast_command_is_not_annotated(self, pwsh_tool: Powershell) -> None:
        task = _fake_process_task(elapsed=0.2)
        with patch("kimix.tools.file.bash.pwsh_tool.ProcessTask", return_value=task):
            result = await pwsh_tool(PowershellParams(cmd="Get-Location"))

        assert isinstance(result, ToolOk)
        assert result.message.endswith("success")


# ── python ────────────────────────────────────────────────────────────────


@pytest.fixture
def python_tool(tmp_path, monkeypatch: pytest.MonkeyPatch) -> python:
    session = MagicMock()
    session.custom_data = {}
    session.custom_config = {"config_json": {}}
    session.dir = tmp_path
    monkeypatch.chdir(tmp_path)
    return python(session=session)


class TestPythonSpentTime:
    async def test_success_message_reports_spent_time(self, python_tool: python) -> None:
        task = _fake_process_task(elapsed=2.25)
        with patch("kimix.tools.py.ProcessTask", return_value=task):
            result = await python_tool(PythonParams(code="print(1)"))

        assert isinstance(result, ToolOk)
        assert result.message.endswith("(2.25s)")
        assert "elapsed_seconds: 2.25" in result.output

    async def test_failure_message_reports_spent_time(self, python_tool: python) -> None:
        task = _fake_process_task(elapsed=4.0, success=False, exit_code=1)
        with patch("kimix.tools.py.ProcessTask", return_value=task):
            result = await python_tool(PythonParams(code="raise SystemExit(1)"))

        assert isinstance(result, ToolError)
        assert "failed" in result.message
        assert result.message.endswith("(4.00s)")

    async def test_legacy_output_path_branch_reports_spent_time(
        self, python_tool: python, tmp_path
    ) -> None:
        task = _fake_process_task(elapsed=1.75)
        out_file = tmp_path / "out.txt"
        with patch("kimix.tools.py.ProcessTask", return_value=task):
            result = await python_tool(
                PythonParams(code="print(1)", output_path=str(out_file))
            )

        assert isinstance(result, ToolOk)
        assert result.message.endswith("(1.75s)")

    async def test_fast_code_is_not_annotated(self, python_tool: python) -> None:
        task = _fake_process_task(elapsed=0.1)
        with patch("kimix.tools.py.ProcessTask", return_value=task):
            result = await python_tool(PythonParams(code="print(1)"))

        assert isinstance(result, ToolOk)
        assert "(0.10s)" not in result.message


# ── job_output ────────────────────────────────────────────────────────────


class TestJobOutputSpentTime:
    @staticmethod
    def _register(session: MagicMock, task_id: str, stream: MagicMock) -> None:
        from kimix.tools.background.utils import TaskData

        data = TaskData()
        data.tasks = {task_id: stream}
        session.custom_data["background_task_data"] = data

    @staticmethod
    def _stream(
        *,
        elapsed: float | None,
        success: bool = True,
        exit_code: int = 0,
        message: str = "success",
    ) -> MagicMock:
        stream = MagicMock()
        stream.format_output = AsyncMock(
            return_value=("processed output", message, None, None, False)
        )
        stream.process_elapsed = elapsed
        stream.stop = AsyncMock(return_value=True)
        stream.thread_is_alive = AsyncMock(return_value=False)
        stream.success = AsyncMock(return_value=success)
        stream.exit_code = exit_code
        stream.pop_output = AsyncMock(return_value="raw output")
        return stream

    async def test_completed_job_reports_spent_time(self, mock_session: MagicMock) -> None:
        to = TaskOutput(session=mock_session)
        self._register(mock_session, "bash_1", self._stream(elapsed=4.5))

        result = await to(TaskOutputParams(job_id="bash_1"))

        assert not result.is_error
        assert "processed output" in result.output
        assert "[Process completed in 4.50s]" in result.output
        assert result.message.endswith("(4.50s)")

    async def test_failed_job_reports_spent_time(self, mock_session: MagicMock) -> None:
        to = TaskOutput(session=mock_session)
        self._register(
            mock_session,
            "bash_1",
            self._stream(elapsed=6.0, success=False, exit_code=1, message="failed"),
        )

        result = await to(TaskOutputParams(job_id="bash_1"))

        assert result.is_error
        assert result.message.endswith("(6.00s)")

    async def test_fast_job_is_not_annotated(self, mock_session: MagicMock) -> None:
        to = TaskOutput(session=mock_session)
        self._register(mock_session, "bash_1", self._stream(elapsed=0.25))

        result = await to(TaskOutputParams(job_id="bash_1"))

        assert not result.is_error
        assert result.message == "success"
        assert "Process completed in" not in result.output

    async def test_running_job_is_not_annotated(self, mock_session: MagicMock) -> None:
        to = TaskOutput(session=mock_session)
        stream = self._stream(elapsed=9.0)
        stream.thread_is_alive = AsyncMock(return_value=True)
        self._register(mock_session, "bash_1", stream)

        result = await to(TaskOutputParams(job_id="bash_1"))

        assert not result.is_error
        assert "9.00s" not in result.message
        assert "Process completed in" not in result.output

    async def test_killed_job_reports_spent_time(self, mock_session: MagicMock) -> None:
        to = TaskOutput(session=mock_session)
        stream = self._stream(elapsed=5.5)
        self._register(mock_session, "bash_1", stream)

        result = await to(TaskOutputParams(job_id="bash_1", action="kill"))

        assert not result.is_error
        assert result.message.endswith("(5.50s)")

    async def test_history_read_reports_spent_time(self, mock_session: MagicMock) -> None:
        to = TaskOutput(session=mock_session)
        stream = BackgroundStream()

        def worker(q) -> None:
            q.put("line1\n")

        await stream.start(worker, stop_function=lambda: None)
        await stream.wait()
        stream.process_elapsed = 8.0
        add_task(mock_session, "bash_7", stream)

        first = await to(TaskOutputParams(job_id="bash_7"))
        assert not first.is_error
        assert first.message.endswith("(8.00s)")

        # The task left the active registry: the second read is served from the
        # finished-task history and must report the same spent time once.
        second = await to(TaskOutputParams(job_id="bash_7"))
        assert not second.is_error
        assert "finished-task history" in second.output
        assert "[Process completed in 8.00s]" in second.output
        assert second.message == first.message

    async def test_history_read_of_fast_job_is_not_annotated(
        self, mock_session: MagicMock
    ) -> None:
        to = TaskOutput(session=mock_session)
        stream = BackgroundStream()

        def worker(q) -> None:
            q.put("line1\n")

        await stream.start(worker, stop_function=lambda: None)
        await stream.wait()
        stream.process_elapsed = 0.4
        add_task(mock_session, "bash_7", stream)

        await to(TaskOutputParams(job_id="bash_7"))
        second = await to(TaskOutputParams(job_id="bash_7"))

        assert not second.is_error
        assert "Process completed in" not in second.output
        assert "(0.40s)" not in second.message
