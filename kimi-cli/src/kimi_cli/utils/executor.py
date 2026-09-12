from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import weakref
from concurrent.futures.thread import _threads_queues
from concurrent.futures.thread import _worker as _thread_pool_worker


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
        if hasattr(self, "_create_worker_context"):
            # CPython 3.14+: ``submit()`` holds ``self._shutdown_lock`` for
            # its whole body (including this call), so the daemon-marking
            # pass cannot take that lock again — it is not reentrant and the
            # submitting thread already owns it, which would deadlock on the
            # very first submit.  Thread creation is replicated below with
            # ``daemon=...`` folded into the Thread constructor, which is
            # race-free: the daemon flag must be set before ``start()`` anyway.
            self._adjust_thread_count_daemon_at_creation()
            return
        # CPython <= 3.13: the lock is free here; let the parent spawn the
        # thread(s), then mark every existing worker as daemon.
        super()._adjust_thread_count()
        with self._shutdown_lock:
            for t in self._threads:
                t.daemon = self._daemon_flag

    def _adjust_thread_count_daemon_at_creation(self) -> None:
        """3.14+ ``_adjust_thread_count`` that creates daemon worker threads.

        Mirrors ``ThreadPoolExecutor._adjust_thread_count`` from CPython 3.14
        (worker-context variant), with the only change being
        ``daemon=self._daemon_flag`` in the ``threading.Thread`` constructor.
        """
        # if idle threads are available, don't spin new threads
        if self._idle_semaphore.acquire(timeout=0):
            return

        # When the executor gets lost, the weakref callback will wake up
        # the worker threads.
        def weakref_cb(_, q=self._work_queue) -> None:
            q.put(None)

        num_threads = len(self._threads)
        if num_threads < self._max_workers:
            thread_name = f"{self._thread_name_prefix or self}_{num_threads}"
            t = threading.Thread(
                name=thread_name,
                target=_thread_pool_worker,
                args=(
                    weakref.ref(self, weakref_cb),
                    self._create_worker_context(),
                    self._work_queue,
                ),
                daemon=self._daemon_flag,
            )
            t.start()
            self._threads.add(t)
            _threads_queues[t] = self._work_queue


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
