from __future__ import annotations

import asyncio
import concurrent.futures


class DaemonThreadPoolExecutor(concurrent.futures.ThreadPoolExecutor):
    """ThreadPoolExecutor whose worker threads are daemon threads.

    Python's default ``ThreadPoolExecutor`` (used by ``asyncio.to_thread()``
    when no custom executor is set on the event loop) creates **non-daemon**
    worker threads.  When ``Ctrl+C`` triggers interpreter shutdown while those
    threads are still running, ``threading._shutdown()`` blocks trying to join
    them, and a second ``SIGINT`` raises ``KeyboardInterrupt`` inside C code
    that has no Python frame — producing the ugly "Exception ignored while
    joining a thread" traceback.

    This subclass overrides ``_adjust_thread_count`` so that every worker
    thread is created as a daemon thread.  Daemon threads are **not** joined
    during interpreter shutdown, which eliminates the race entirely.
    """

    def __init__(
        self,
        max_workers: int | None = None,
        thread_name_prefix: str = "",
        daemon: bool = True,
    ) -> None:
        self._daemon_flag = daemon
        super().__init__(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
        )

    def _adjust_thread_count(self) -> None:
        # Let the parent create the thread(s) first.
        super()._adjust_thread_count()
        # Then mark every existing worker as daemon.
        with self._shutdown_lock:
            for t in self._threads:
                t.daemon = self._daemon_flag


def install_daemon_thread_pool_executor() -> DaemonThreadPoolExecutor:
    """Replace the running event loop's default executor with a daemon-thread
    backed one so that ``asyncio.to_thread()`` does not block interpreter
    shutdown on ``Ctrl+C``.

    Call this at the very start of any ``async def`` entry-point that will be
    driven by ``asyncio.run()``, before any call to ``asyncio.to_thread()``.

    Returns the installed executor (handy for introspection, but callers
    typically do not need to keep a reference).
    """
    loop = asyncio.get_running_loop()
    executor = DaemonThreadPoolExecutor()
    loop.set_default_executor(executor)
    return executor
