"""Tests for Defects 3.1-3.4: TaskOutput improvements."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from kimix.tools.background import TaskOutput, TaskOutputParams
from kimix.tools.background.utils import (
    BackgroundStream,
    add_task,
)


# ── Timeout units: canonical seconds, legacy timeout_ms alias ────────────


def test_legacy_timeout_ms_converts_to_seconds() -> None:
    """Legacy ``timeout_ms`` (milliseconds) converts to canonical ``timeout`` seconds."""
    assert TaskOutputParams(timeout_ms=60000).timeout == 60
    assert TaskOutputParams(timeout_ms=7200000).timeout == 7200
    # Canonical `timeout` wins when both spellings are supplied.
    assert TaskOutputParams(timeout=5, timeout_ms=60000).timeout == 5
    # Sub-second legacy values floor to the 1s minimum.
    assert TaskOutputParams(timeout_ms=500).timeout == 1


def test_canonical_timeout_is_in_seconds() -> None:
    """The canonical param is `timeout` (seconds), not `timeout_ms`."""
    props = TaskOutputParams.model_json_schema()["properties"]
    assert "timeout" in props
    assert "timeout_ms" not in props
    assert "Max wait in seconds" in props["timeout"]["description"]
    assert props["timeout"]["maximum"] == 7200
    assert TaskOutputParams(timeout=60).timeout == 60


# ── Defect 3.1: block → wait rename ─────────────────────────────────────


# ── Defect 3.3: Structured task list ────────────────────────────────────


# ── Defect 3.4: Action parameter / kill ─────────────────────────────────


class TestTaskOutputActionKill:
    async def test_action_kill_requires_task_id(self, mock_session: MagicMock) -> None:
        to = TaskOutput(session=mock_session)
        result = await to(TaskOutputParams(action="kill"))
        assert result.is_error
        assert "task_id" in result.message.lower()

    async def test_action_kill_missing_task_not_found(self, mock_session: MagicMock) -> None:
        to = TaskOutput(session=mock_session)
        result = await to(TaskOutputParams(action="kill", task_id="nonexistent"))
        assert result.is_error


# ── Defect: __del__ cleanup safety ────────────────────────────────────────


class TestTaskOutputDelCleanup:
    """Verify that __del__ does not crash or leak event loops."""

    def test_del_without_session_does_nothing(self) -> None:
        """__del__ with no _session should not crash."""
        obj = TaskOutput.__new__(TaskOutput)
        obj.__del__()  # Should not raise

    def test_del_with_mock_session_no_event_loop(self) -> None:
        """__del__ with a session but no running loop should not crash."""
        obj = TaskOutput.__new__(TaskOutput)
        obj._session = MagicMock()
        obj.__del__()  # Should not raise

    def test_del_during_finalization_noop(self) -> None:
        """__del__ when sys.is_finalizing() should return early."""
        import sys
        # Simulate interpreter-finalizing state
        orig = sys.is_finalizing
        try:
            sys.is_finalizing = lambda: True  # type: ignore[method-assign]
            obj = TaskOutput.__new__(TaskOutput)
            obj._session = MagicMock()
            obj.__del__()  # Should return without accessing session
        finally:
            sys.is_finalizing = orig


# ── Defect: TaskOutput wait_for_pattern (WP5) ─────────────────────────────


class TestTaskOutputWaitForPattern:
    @staticmethod
    def _stream(matched: bool) -> MagicMock:
        stream = MagicMock()
        stream.wait_for_output = AsyncMock(return_value=("output text", matched, 0.1))
        stream.thread_is_alive = AsyncMock(return_value=True)
        stream.success = AsyncMock(return_value=True)
        stream.pop_output = AsyncMock(return_value="output text")
        stream.process_elapsed = None
        return stream

    def _register(self, mock_session: MagicMock, stream: MagicMock) -> None:
        from kimix.tools.background.utils import TaskData

        data = TaskData()
        data.tasks = {"bash_1": stream}
        mock_session.custom_data["background_task_data"] = data

    async def test_wait_for_pattern_matched_true(self, mock_session: MagicMock) -> None:
        to = TaskOutput(session=mock_session)
        stream = self._stream(matched=True)
        self._register(mock_session, stream)

        result = await to(TaskOutputParams(job_id="bash_1", wait=True, wait_for_pattern="ready"))

        assert not result.is_error
        assert "output text" in result.output
        assert "wait_matched: true" in result.output
        stream.wait_for_output.assert_awaited_once()

    async def test_wait_for_pattern_matched_false(self, mock_session: MagicMock) -> None:
        to = TaskOutput(session=mock_session)
        stream = self._stream(matched=False)
        self._register(mock_session, stream)

        result = await to(TaskOutputParams(job_id="bash_1", wait=True, wait_for_pattern="never"))

        assert not result.is_error
        assert "wait_matched: false" in result.output

    async def test_invalid_regex_returns_tool_error(self, mock_session: MagicMock) -> None:
        to = TaskOutput(session=mock_session)
        self._register(mock_session, self._stream(matched=True))

        result = await to(TaskOutputParams(job_id="bash_1", wait=True, wait_for_pattern="["))

        assert result.is_error
        assert "Invalid wait_for_pattern" in result.message


class TestTaskOutputOriginalSavedSuffix:
    @staticmethod
    def _stream_with_rtk_output(output: str) -> MagicMock:
        stream = MagicMock()
        stream.format_output = None
        stream.wait_for_output = AsyncMock(return_value=(output, False, 0.1))
        stream.wait_with_inactivity_timeout = AsyncMock(return_value=(True, 0.1, False))
        stream.pop_output = AsyncMock(return_value=output)
        stream.thread_is_alive = AsyncMock(return_value=False)
        stream.success = AsyncMock(return_value=True)
        stream.process_elapsed = None
        return stream

    def _register(self, mock_session: MagicMock, stream: MagicMock) -> None:
        from kimix.tools.background.utils import TaskData

        data = TaskData()
        data.tasks = {"bash_1": stream}
        mock_session.custom_data["background_task_data"] = data

    async def test_message_includes_original_path_for_rtk_output(
        self, mock_session: MagicMock
    ) -> None:
        to = TaskOutput(session=mock_session)
        rtk_output = (
            "line1\nline2\n"
            "+5 more files [see remaining: .kimix_cache/tmp_1234/0.txt]\n"
        )
        self._register(mock_session, self._stream_with_rtk_output(rtk_output))

        result = await to(TaskOutputParams(task_id="bash_1"))

        assert not result.is_error
        assert "[original saved to .kimix_cache/tmp_" in result.message

    async def test_message_empty_for_plain_output(
        self, mock_session: MagicMock
    ) -> None:
        to = TaskOutput(session=mock_session)
        self._register(mock_session, self._stream_with_rtk_output("plain output"))

        result = await to(TaskOutputParams(task_id="bash_1"))

        assert not result.is_error
        assert "[original saved to" not in result.message


class TestTaskOutputFormatterCallback:
    """Completed tasks should inherit the originating tool's output formatter."""

    @staticmethod
    def _stream_with_formatter(formatter_return: tuple) -> MagicMock:
        stream = MagicMock()
        stream.format_output = AsyncMock(return_value=formatter_return)
        stream.wait_with_inactivity_timeout = AsyncMock(
            return_value=(True, 0.1, False)
        )
        stream.pop_output = AsyncMock(return_value="raw output")
        stream.thread_is_alive = AsyncMock(return_value=False)
        stream.success = AsyncMock(return_value=True)
        stream.exit_code = 0
        stream.process_elapsed = 1.23
        return stream

    def _register(self, mock_session: MagicMock, stream: MagicMock) -> None:
        from kimix.tools.background.utils import TaskData

        data = TaskData()
        data.tasks = {"bash_1": stream}
        mock_session.custom_data["background_task_data"] = data

    async def test_formatter_callback_is_used_for_success(
        self, mock_session: MagicMock
    ) -> None:
        to = TaskOutput(session=mock_session)
        stream = self._stream_with_formatter(
            ("processed output", "success [original saved to x]", None, None, False)
        )
        self._register(mock_session, stream)

        result = await to(TaskOutputParams(task_id="bash_1"))

        assert not result.is_error
        assert "processed output" in result.output
        assert "success [original saved to x]" in result.message
        stream.format_output.assert_awaited_once_with("raw output", True, 0, 1.23, None)

    async def test_formatter_callback_is_used_for_failure(
        self, mock_session: MagicMock
    ) -> None:
        to = TaskOutput(session=mock_session)
        stream = self._stream_with_formatter(
            ("processed output", "failed [command saved to y]", None, None, False)
        )
        stream.success = AsyncMock(return_value=False)
        self._register(mock_session, stream)

        result = await to(TaskOutputParams(task_id="bash_1"))

        assert result.is_error
        assert "failed [command saved to y]" in result.message
        assert "processed output" in result.output
        stream.format_output.assert_awaited_once_with("raw output", False, 0, 1.23, None)


# ── Finished-task history: completed jobs stay retrievable ────────────────


class TestTaskOutputFinishedHistory:
    """A job that already finished must remain retrievable via job_output.

    Regression coverage for the reported bug: the first ``job_output`` read of
    a completed task removed it from the active registry, so a second read
    returned ``Task 'bash_23' not found`` even though the data existed.
    """

    @staticmethod
    def _register(mock_session: MagicMock, task_id: str, stream: BackgroundStream) -> None:
        add_task(mock_session, task_id, stream)

    @staticmethod
    async def _run_to_completion(stream: BackgroundStream, failed: bool = False) -> None:
        def worker(q) -> None:
            q.put("line1\nerror: something broke\nline2\n")

        await stream.start(worker, stop_function=lambda: None)
        await stream.wait()
        if failed:
            # Simulate a non-zero exit after the thread finished.
            stream._success = False
            stream._exit_code = 1

    async def test_second_read_of_failed_task_comes_from_history(
        self, mock_session: MagicMock
    ) -> None:
        to = TaskOutput(session=mock_session)
        stream = BackgroundStream()
        await self._run_to_completion(stream, failed=True)
        self._register(mock_session, "bash_23", stream)

        # First read: task is completed+failed, returns the failure output and
        # (previously) dropped the task from the registry.
        first = await to(TaskOutputParams(job_id="bash_23", action="get"))
        assert first.is_error
        assert "error: something broke" in first.output

        # Second read (e.g. with wait_for_pattern to grep the failure): must be
        # served from the finished-task history, not "not found".
        second = await to(
            TaskOutputParams(job_id="bash_23", wait_for_pattern="error: something")
        )
        assert second.is_error  # the underlying task still failed
        assert "not found" not in second.message
        assert "not found" not in second.output
        assert "error: something broke" in second.output
        assert "wait_matched: true" in second.output
        assert "finished-task history" in second.output

    async def test_second_read_of_successful_task_comes_from_history(
        self, mock_session: MagicMock
    ) -> None:
        to = TaskOutput(session=mock_session)
        stream = BackgroundStream()
        await self._run_to_completion(stream)
        self._register(mock_session, "bash_23", stream)

        first = await to(TaskOutputParams(job_id="bash_23", action="get"))
        assert not first.is_error
        assert "line1" in first.output

        second = await to(TaskOutputParams(job_id="bash_23", action="get"))
        assert not second.is_error
        assert "not found" not in second.message
        assert "line1" in second.output
        assert "error: something broke" in second.output
        assert "finished-task history" in second.output

    async def test_history_read_wait_for_pattern_no_match(
        self, mock_session: MagicMock
    ) -> None:
        to = TaskOutput(session=mock_session)
        stream = BackgroundStream()
        await self._run_to_completion(stream)
        self._register(mock_session, "bash_23", stream)

        await to(TaskOutputParams(job_id="bash_23", action="get"))
        second = await to(TaskOutputParams(job_id="bash_23", wait_for_pattern="zzz_no_match"))

        assert not second.is_error
        assert "wait_matched: false" in second.output

    async def test_history_read_invalid_pattern_returns_tool_error(
        self, mock_session: MagicMock
    ) -> None:
        to = TaskOutput(session=mock_session)
        stream = BackgroundStream()
        await self._run_to_completion(stream)
        self._register(mock_session, "bash_23", stream)

        await to(TaskOutputParams(job_id="bash_23", action="get"))
        second = await to(TaskOutputParams(job_id="bash_23", wait_for_pattern="["))

        assert second.is_error
        assert "Invalid wait_for_pattern" in second.message

    async def test_history_read_exports_output_path(
        self, mock_session: MagicMock, tmp_path
    ) -> None:
        to = TaskOutput(session=mock_session)
        stream = BackgroundStream()
        await self._run_to_completion(stream)
        self._register(mock_session, "bash_23", stream)

        await to(TaskOutputParams(job_id="bash_23", action="get"))
        out_file = tmp_path / "history_output.txt"
        second = await to(TaskOutputParams(job_id="bash_23", output_path=str(out_file)))

        assert not second.is_error
        assert "output exported to file" in second.output
        assert "error: something broke" in out_file.read_text(encoding="utf-8")

    async def test_kill_of_already_finished_task_comes_from_history(
        self, mock_session: MagicMock
    ) -> None:
        to = TaskOutput(session=mock_session)
        stream = BackgroundStream()
        await self._run_to_completion(stream)
        self._register(mock_session, "bash_23", stream)

        # Consume the completed task so it is removed from the registry.
        await to(TaskOutputParams(job_id="bash_23", action="get"))

        killed = await to(TaskOutputParams(job_id="bash_23", action="kill"))
        assert not killed.is_error
        assert "not found" not in killed.message
        assert "line1" in killed.output
        assert "finished-task history" in killed.output

    async def test_unknown_task_still_not_found(self, mock_session: MagicMock) -> None:
        to = TaskOutput(session=mock_session)
        result = await to(TaskOutputParams(job_id="never_existed"))
        assert result.is_error
        # With an empty registry the legacy message is "No running task".
        assert "not found" in result.message or "No running task" in result.message
        assert "finished-task history" not in result.output

    async def test_task_removed_by_originating_tool_comes_from_history(
        self, mock_session: MagicMock
    ) -> None:
        """Tasks dropped via remove_task_id (e.g. bash/python tools) stay retrievable."""
        from kimix.tools.background.utils import remove_task_id

        to = TaskOutput(session=mock_session)
        stream = BackgroundStream()
        await self._run_to_completion(stream, failed=True)
        self._register(mock_session, "bash_23", stream)

        # The originating tool drops the task from the registry without going
        # through job_output (mirrors bash_tool.py: remove_task_id runs before
        # the final pop_output, so the buffered output is captured by the
        # removal safety net).
        remove_task_id(mock_session, "bash_23")

        result = await to(TaskOutputParams(job_id="bash_23", action="get"))
        assert result.is_error  # the task failed
        assert "not found" not in result.message
        assert "line1" in result.output
        assert "finished-task history" in result.output

