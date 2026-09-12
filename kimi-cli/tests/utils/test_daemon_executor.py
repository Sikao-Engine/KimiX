"""Tests for DaemonThreadPoolExecutor (daemon default executor for asyncio).

Regression guard for the CPython 3.14 deadlock: ``ThreadPoolExecutor.submit()``
holds ``self._shutdown_lock`` while invoking ``_adjust_thread_count()``, so a
subclass that re-acquires the lock there (to mark workers daemon) deadlocks the
submitting thread on the very first ``run_in_executor`` call.
"""

from __future__ import annotations

import asyncio
import threading
import time

from kimi_cli.utils.executor import (
    DaemonThreadPoolExecutor,
    install_daemon_thread_pool_executor,
)


def test_submit_does_not_deadlock_and_spawns_daemon_threads() -> None:
    executor = DaemonThreadPoolExecutor(max_workers=2, thread_name_prefix="dtp_test")
    try:
        future = executor.submit(time.sleep, 0.01)
        # The pre-fix code blocked forever inside submit(); give the failure
        # mode a bounded runtime instead of hanging the suite.
        assert future.result(timeout=10) is None
        spawned = list(executor._threads)
        assert spawned, "expected at least one worker thread to be spawned"
        assert all(t.daemon for t in spawned), (
            f"all workers must be daemon threads, got: {[t.daemon for t in spawned]}"
        )
        # A second submit (idle-worker path) must not deadlock either.
        assert executor.submit(lambda: 42).result(timeout=10) == 42
    finally:
        executor.shutdown(wait=True)


def test_parallel_submits_all_workers_daemon() -> None:
    executor = DaemonThreadPoolExecutor(max_workers=3, thread_name_prefix="dtp_test")
    try:
        futures = [executor.submit(time.sleep, 0.01) for _ in range(3)]
        for f in futures:
            f.result(timeout=10)
        spawned = list(executor._threads)
        assert len(spawned) >= 3
        assert all(t.daemon for t in spawned)
    finally:
        executor.shutdown(wait=True)


def _current_default_executor(loop: asyncio.AbstractEventLoop) -> object:
    # ``get_default_executor()`` was removed from the event loop API; the
    # default executor lives in the private ``_default_executor`` slot.
    return getattr(loop, "_default_executor", None)


async def test_install_sets_loop_default_executor() -> None:
    loop = asyncio.get_running_loop()
    previous = _current_default_executor(loop)
    installed = install_daemon_thread_pool_executor()
    try:
        assert _current_default_executor(loop) is installed
        # End-to-end: run_in_executor through the loop's default executor.
        assert await asyncio.to_thread(lambda: "ok") == "ok"
    finally:
        # 3.14 rejects None in set_default_executor; restore the private slot.
        loop._default_executor = previous  # type: ignore[attr-defined]


async def test_to_thread_runs_on_daemon_pool() -> None:
    loop = asyncio.get_running_loop()
    previous = _current_default_executor(loop)
    installed = install_daemon_thread_pool_executor()
    try:
        main_thread = threading.current_thread()

        def _worker_thread() -> tuple[bool, str]:
            t = threading.current_thread()
            return (t is not main_thread, t.name)

        is_other, _name = await asyncio.to_thread(_worker_thread)
        assert is_other
        assert installed._threads
        assert all(t.daemon for t in installed._threads)
    finally:
        loop._default_executor = previous  # type: ignore[attr-defined]
