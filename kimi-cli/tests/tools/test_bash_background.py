"""End-to-end regressions for Bash background execution."""

from __future__ import annotations

import pytest

from kimi_cli.session import Session
from kimi_cli.tools import SkipThisTool
from kimi_cli.tools.background import TaskOutput, TaskOutputParams
from kimi_cli.tools.background.utils import (
    get_all_tasks,
    get_finished_task,
    remove_task_id,
)
from kimi_cli.tools.file.bash.bash_tool import Bash, BashParams, find_bash
from kosong.tooling import ToolError, ToolOk

pytestmark = pytest.mark.skipif(find_bash() is None, reason="bash not found on this platform")


def _make_tool(session: Session) -> Bash:
    try:
        return Bash(session)
    except SkipThisTool:
        pytest.skip("bash tool disabled on this platform")


async def _cleanup(session: Session, task_id: str) -> None:
    stream = get_all_tasks(session).get(task_id)
    if stream is not None:
        await stream.stop()
        await stream.wait(timeout=5)
        remove_task_id(session, task_id)


@pytest.mark.asyncio
async def test_background_pipeline_uses_pipefail(session: Session) -> None:
    tool = _make_tool(session)
    started = await tool(BashParams(cmd="false | true", mode="send"))
    assert isinstance(started, ToolOk)
    task_id = next(iter(get_all_tasks(session)))

    try:
        reader = TaskOutput(session)
        result = await reader(TaskOutputParams(job_id=task_id, wait=True, timeout=20))
        assert isinstance(result, ToolError)
        record = get_finished_task(session, task_id)
        assert record is not None
        assert record.exit_code == 1
    finally:
        await _cleanup(session, task_id)


@pytest.mark.asyncio
async def test_background_pattern_wait_preserves_failure_and_output(session: Session) -> None:
    tool = _make_tool(session)
    initial = await tool(
        BashParams(
            cmd="printf 'before-exit\\n'; exit 3",
            mode="send",
            wait_for_pattern="never",
            timeout=20,
        )
    )

    assert isinstance(initial, ToolError)
    assert "exit_code: 3" in initial.output
    assert "before-exit" in initial.output

    task_id = next(iter(get_all_tasks(session)))
    try:
        reader = TaskOutput(session)
        final = await reader(TaskOutputParams(job_id=task_id, wait=True, timeout=20))
        assert isinstance(final, ToolError)
        assert "before-exit" in final.output

        history = await reader(TaskOutputParams(job_id=task_id))
        assert isinstance(history, ToolError)
        assert "before-exit" in history.output
    finally:
        await _cleanup(session, task_id)


@pytest.mark.asyncio
async def test_background_pattern_wait_ignores_output_inactivity(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kimi_cli.tools.background.utils as background_utils

    monkeypatch.setattr(background_utils, "DEFAULT_INACTIVITY_TIMEOUT", 0.1)
    tool = _make_tool(session)
    initial = await tool(
        BashParams(
            cmd="sleep 0.8; printf 'quiet-done\\n'",
            mode="send",
            wait_for_pattern="quiet-done",
            timeout=5,
        )
    )

    assert isinstance(initial, ToolOk)
    assert "quiet-done" in initial.output
    assert "wait_matched: true" in initial.output
    task_id = next(iter(get_all_tasks(session)))
    await _cleanup(session, task_id)
