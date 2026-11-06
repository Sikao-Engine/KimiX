"""Regression tests for blocking background waits and output draining."""

from __future__ import annotations

import asyncio
import queue

import pytest

from kimi_cli.tools.background import TaskOutput, TaskOutputParams
from kimi_cli.tools.background.utils import BackgroundStream, add_task


class _FakeSession:
    def __init__(self) -> None:
        self.custom_data: dict[str, object] = {}


@pytest.mark.asyncio
async def test_zero_inactivity_timeout_waits_for_silent_completion() -> None:
    """Disabling inactivity must not disable periodic output draining."""
    stream = BackgroundStream()

    async def worker(q: queue.Queue[str]) -> tuple[bool, int]:
        await asyncio.sleep(0.8)
        q.put("done")
        return True, 0

    await stream.start(worker, lambda: None)
    # Budget generously: under a full-suite run the loop can be starved for
    # longer than the worker's 0.8s sleep, so a 2s budget made this test give
    # up before it ever observed the worker as finished.
    completed, _elapsed, inactivity_timed_out = (
        await stream.wait_with_inactivity_timeout(timeout=10, inactivity_timeout=0)
    )
    await stream.wait(timeout=10)

    assert completed
    assert not inactivity_timed_out
    assert await stream.pop_output() == "done"


@pytest.mark.asyncio
async def test_job_output_explicitly_disables_inactivity_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job_output wait lasts until settlement or its requested timeout."""
    session = _FakeSession()
    stream = BackgroundStream()

    async def worker(q: queue.Queue[str]) -> tuple[bool, int]:
        q.put("result")
        return True, 0

    await stream.start(worker, lambda: None)
    add_task(session, "silent", stream)  # type: ignore[arg-type]

    seen: list[float | None] = []
    original = BackgroundStream.wait_with_inactivity_timeout

    async def checked_wait(
        self: BackgroundStream,
        timeout: float,
        inactivity_timeout: float | None = None,
    ) -> tuple[bool, float, bool]:
        seen.append(inactivity_timeout)
        return await original(self, timeout, inactivity_timeout)

    monkeypatch.setattr(BackgroundStream, "wait_with_inactivity_timeout", checked_wait)
    reader = TaskOutput(session)  # type: ignore[arg-type]
    result = await reader(TaskOutputParams(job_id="silent", wait=True, timeout=2))

    assert not result.is_error
    assert result.output == "result"
    assert seen == [0]
