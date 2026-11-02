"""Regression tests for asyncio sub-process pipe teardown.

On Windows the Proactor loop gives every ``asyncio.create_subprocess_*`` child
three pipe transports.  A transport that outlives the loop that owns it is
closed underneath asyncio by ``loop.close()``; the garbage collector then runs
the transport deallocator, which warns about an "unclosed transport" and - while
formatting that warning - raises ``ValueError: I/O operation on closed pipe``::

    Exception ignored while calling deallocator
    <function _ProactorBasePipeTransport.__del__ ...>

That noise used to appear while an unrelated tool call (e.g. ``edit``) was
running, because the leaked transports of an earlier child were collected then.
"""

from __future__ import annotations

import asyncio
import gc
import queue
import sys

import pytest
from kimi_cli.utils.subprocess_cleanup import (
    close_loop_pipe_transports,
    close_pipe_transports,
    open_pipe_transports,
    pipe_transports,
    reap_process,
    release_all_pipe_transports,
)

WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32", reason="Proactor pipe transports only exist on Windows"
)

# A child that stays alive until it is killed by the test.
_SLEEPER = [sys.executable, "-c", "import time; time.sleep(3)"]


async def _spawn_three_pipe_child() -> asyncio.subprocess.Process:
    """Spawn a child with stdin/stdout/stderr pipes (three transports)."""
    return await asyncio.create_subprocess_exec(
        *_SLEEPER,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _settle() -> None:
    """Let the loop dispatch the ``connection_lost`` callbacks of closed pipes."""
    for _ in range(3):
        await asyncio.sleep(0)


def _assert_quiet_teardown(capsys: pytest.CaptureFixture[str]) -> None:
    """Collect garbage and assert asyncio's deallocator did not complain."""
    gc.collect()
    captured = capsys.readouterr()
    assert "unclosed transport" not in captured.err
    assert "I/O operation on closed pipe" not in captured.err


@WINDOWS_ONLY
def test_abandoned_child_owns_three_open_transports(capsys: pytest.CaptureFixture[str]) -> None:
    """A child nobody reaps keeps exactly the three pipe transports open."""
    loop = asyncio.new_event_loop()
    try:
        proc = loop.run_until_complete(_spawn_three_pipe_child())
        assert len(pipe_transports(proc)) == 3
        assert len(open_pipe_transports(loop)) == 3
        # The leak the fix removes: closing the loop while the pipes are open.
        loop.run_until_complete(reap_process(proc, kill=True))
    finally:
        close_loop_pipe_transports(loop)
        loop.close()
    _assert_quiet_teardown(capsys)


@WINDOWS_ONLY
def test_close_loop_pipe_transports_releases_pipes_before_loop_close(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The teardown helper must leave no pipe transport behind for the GC."""
    loop = asyncio.new_event_loop()
    loop.run_until_complete(_spawn_three_pipe_child())
    assert len(open_pipe_transports(loop)) == 3

    assert close_loop_pipe_transports(loop) == 3
    assert open_pipe_transports(loop) == []

    loop.close()
    del loop
    _assert_quiet_teardown(capsys)


@WINDOWS_ONLY
def test_close_loop_pipe_transports_is_idempotent() -> None:
    loop = asyncio.new_event_loop()
    try:
        assert close_loop_pipe_transports(loop) == 0
        assert open_pipe_transports(loop) == []
    finally:
        loop.close()


@WINDOWS_ONLY
def test_release_all_pipe_transports_clears_the_atexit_net(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The atexit safety net releases leftovers from any loop."""
    loop = asyncio.new_event_loop()
    loop.run_until_complete(_spawn_three_pipe_child())
    assert len(open_pipe_transports(loop)) == 3

    assert release_all_pipe_transports() >= 3
    assert open_pipe_transports() == []

    loop.close()
    del loop
    _assert_quiet_teardown(capsys)


async def test_reap_process_kills_and_closes_pipes() -> None:
    loop = asyncio.get_running_loop()
    proc = await _spawn_three_pipe_child()
    assert len(pipe_transports(proc)) == 3

    returncode = await reap_process(proc, kill=True, timeout=10)

    assert returncode is not None
    assert close_pipe_transports(proc) == 0
    await _settle()
    assert open_pipe_transports(loop) == []


async def test_reap_process_tolerates_already_exited_child() -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "print('done')",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    assert await reap_process(proc, kill=True) == 0
    await _settle()
    assert open_pipe_transports(asyncio.get_running_loop()) == []


async def test_run_hook_timeout_leaves_no_open_transports() -> None:
    """A hook that times out must release its three pipes (hooks/runner.py)."""
    from kimi_cli.hooks.runner import run_hook

    loop = asyncio.get_running_loop()
    command = f'"{sys.executable}" -c "import time; time.sleep(3)"'

    result = await run_hook(command, {"hook": "test"}, timeout=1)

    assert result.timed_out is True
    await _settle()
    assert open_pipe_transports(loop) == []


async def test_process_task_run_closes_pipes() -> None:
    """The bash/python/pwsh tool machinery reaps the child and its pipes."""
    from kimi_cli.tools.common import ProcessTask

    loop = asyncio.get_running_loop()
    task = ProcessTask(sys.executable, ["-c", "print('hello')"])
    job_queue: queue.Queue[str] = queue.Queue()

    success, exit_code = await task._run_process_bg(job_queue)

    assert success is True
    assert exit_code == 0
    assert "hello" in "".join(job_queue.queue)
    await _settle()
    assert open_pipe_transports(loop) == []


async def test_background_stream_releases_pipes_before_its_loop_closes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """BackgroundStream closes the worker loop right after the job: no leak.

    ``asyncio.run`` in the worker thread closes the loop as soon as the job
    coroutine returns, so a child the job never reaps must have its pipe
    transports released by the job wrapper - otherwise they survive the loop.
    """
    from kimi_cli.tools.background.utils import BackgroundStream

    gc.collect()
    before = {id(transport) for transport in open_pipe_transports()}

    async def job(job_queue: queue.Queue[str]) -> tuple[bool, int | None]:
        await _spawn_three_pipe_child()  # abandoned on purpose
        return True, 0

    stream = BackgroundStream()
    await stream.start(job, lambda: None)
    await stream.wait(timeout=30)
    await _settle()

    leaked = [t for t in open_pipe_transports() if id(t) not in before]
    assert leaked == []
    _assert_quiet_teardown(capsys)


@WINDOWS_ONLY
def test_close_process_event_loop_releases_abandoned_child(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The process-lifetime loop closes transports before closing itself."""
    from kimix.utils.event_loop import (
        close_process_event_loop,
        get_process_event_loop,
        run_on_event_loop,
    )

    async def spawn() -> None:
        await _spawn_three_pipe_child()

    loop = get_process_event_loop()
    run_on_event_loop(spawn())
    assert len(open_pipe_transports(loop)) == 3

    close_process_event_loop()

    assert open_pipe_transports(loop) == []
    _assert_quiet_teardown(capsys)
