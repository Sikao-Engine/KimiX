"""Regression tests: ``job_output`` must never return an empty error message.

The conversation layer used to hard-fail on an error tool result without a
message ("Error return value should have a message"), which aborted the turn:

    ⚡ job_output job_id:bash_30 timeout:900 wait:True
    Task 'bash_30' failed
    ✗ job_output
    Error return value should have a message

Root cause: a failed task can be reported without any human-readable detail —
the finished-task history records a best-effort entry with ``message == ""``
when a task leaves the active registry without a rich result (see
``_record_from_stream``), and ``_append_elapsed("", None)`` stays empty.  The
``job_output`` failure paths then built ``ToolError(message="")``.
"""

from __future__ import annotations

import asyncio

import pytest

import kimi_cli.tools.background as background_tools
from kimi_cli.soul.message import tool_result_to_message
from kimi_cli.tools.background import (
    FinishedTask,
    TaskOutput,
    TaskOutputParams,
    _exception_message,
    _failure_message,
)
from kimi_cli.tools.background.utils import (
    BackgroundStream,
    add_task,
    record_finished_task,
    remove_task_id,
)
from kimi_cli.wire.types import ToolResult
from kosong.tooling import ToolError, ToolReturnValue


class _FakeSession:
    """Minimal session stand-in: the task registry lives in ``custom_data``."""

    def __init__(self) -> None:
        self.custom_data: dict[str, object] = {}


def _dead_stream(*, success: bool = False, exit_code: int | None = 2) -> BackgroundStream:
    """A started stream whose worker thread is gone (no thread at all)."""
    stream = BackgroundStream()
    stream._started = True
    stream._success = success
    stream._exit_code = exit_code
    stream._process_elapsed = None  # no timing => no elapsed suffix
    return stream


def _convert_error(value: ToolReturnValue) -> str:
    """Run the message conversion and return the resulting error text."""
    message = tool_result_to_message(ToolResult(tool_call_id="call-1", return_value=value))
    text = message.content[0].text  # type: ignore[union-attr]
    # ``system()`` wraps the text in <system>…</system>.
    return text.removeprefix("<system>").removesuffix("</system>")


@pytest.mark.asyncio
async def test_history_failure_without_message_returns_nonempty_error() -> None:
    """A safety-net history record (message="") must not yield an empty error."""
    session = _FakeSession()
    add_task(session, "bash_30", _dead_stream())
    # remove_task_id() is what a registered tool calls when the job finishes;
    # with no rich record stored first it creates the best-effort record whose
    # message is empty.
    remove_task_id(session, "bash_30")

    tool = TaskOutput(session)  # type: ignore[arg-type]
    result = await tool(TaskOutputParams(job_id="bash_30", timeout=900, wait=True))

    assert result.is_error is True
    assert result.message, "error return value must carry a message"
    assert "bash_30" in result.message
    assert result.brief == "Task 'bash_30' failed"
    assert result.output
    _convert_error(result)  # must not raise


@pytest.mark.asyncio
async def test_live_failure_without_formatter_returns_nonempty_error() -> None:
    """A failing task with no registered formatter has no detail message."""
    session = _FakeSession()
    add_task(session, "bash_31", _dead_stream(exit_code=7))

    tool = TaskOutput(session)  # type: ignore[arg-type]
    result = await tool(TaskOutputParams(job_id="bash_31", timeout=30, wait=True))

    assert result.is_error is True
    assert result.message, "error return value must carry a message"
    assert "exit code 7" in result.message
    _convert_error(result)


@pytest.mark.asyncio
async def test_history_failure_keeps_rich_message() -> None:
    """A record with a real message keeps it (no synthesized fallback)."""
    session = _FakeSession()
    record_finished_task(
        session,  # type: ignore[arg-type]
        FinishedTask(
            task_id="bash_32",
            output="boom\n",
            processed="boom",
            message="failed Hint: command not found",
            success=False,
            exit_code=127,
            elapsed=12.5,
        ),
    )

    tool = TaskOutput(session)  # type: ignore[arg-type]
    result = await tool(TaskOutputParams(job_id="bash_32"))

    assert result.is_error is True
    assert result.message.startswith("failed Hint: command not found")
    assert "(12.50s)" in result.message


@pytest.mark.asyncio
async def test_unexpected_exception_without_message_returns_nonempty_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``str(TimeoutError())`` is empty: the error must still explain itself."""
    session = _FakeSession()
    tool = TaskOutput(session)  # type: ignore[arg-type]

    def _boom(_session: object) -> dict[str, BackgroundStream]:
        raise TimeoutError()

    monkeypatch.setattr(background_tools, "get_all_tasks", _boom)

    result = await tool(TaskOutputParams(job_id="bash_33"))

    assert result.is_error is True
    assert result.message
    assert "TimeoutError" in result.message
    _convert_error(result)


def test_failure_message_helper_fallbacks() -> None:
    """The helper synthesizes a message only when there is nothing to show."""
    assert _failure_message("failed", "bash_30") == "failed"
    assert _failure_message("", "bash_30", exit_code=2) == "Task 'bash_30' failed (exit code 2)."
    assert _failure_message("", "bash_30") == "Task 'bash_30' failed."
    assert _failure_message("", "bash_30", elapsed=3.0) == "Task 'bash_30' failed. (3.00s)"


def test_exception_message_helper_fallbacks() -> None:
    assert _exception_message(TimeoutError()) == "TimeoutError while reading the job output."
    assert _exception_message(ValueError("bad id")) == "ValueError: bad id"


def test_tool_error_normalizes_empty_message() -> None:
    assert ToolError(message="", brief="Boom").message == "Boom"
    assert ToolError(message="", brief="").message == "Tool call failed."
    assert ToolError(message="kept", brief="Boom").message == "kept"


def test_tool_result_to_message_survives_empty_error_message() -> None:
    """Defense in depth: legacy callers constructing an empty message directly."""
    raw = ToolReturnValue(
        is_error=True,
        output="some output",
        message="",
        display=[],
    )
    text = _convert_error(raw)
    assert text.startswith("ERROR: ")
    assert "tool call failed" in text


@pytest.mark.asyncio
async def test_empty_message_error_is_sendable() -> None:
    """End-to-end: a failed job_output result converts without raising."""
    session = _FakeSession()
    add_task(session, "bash_34", _dead_stream())
    remove_task_id(session, "bash_34")

    tool = TaskOutput(session)  # type: ignore[arg-type]
    result = await tool(TaskOutputParams(job_id="bash_34"))
    assert result.is_error

    message = await asyncio.to_thread(_convert_error, result)
    assert message == "ERROR: Task 'bash_34' failed (exit code 2)."
