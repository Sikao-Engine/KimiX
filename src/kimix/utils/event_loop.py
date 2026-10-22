"""One long-lived event loop per thread for the synchronous entry points.

Every synchronous entry point that drives a session — :func:`kimix.utils.prompt.prompt`,
``prompt_plan``, ``fix_error``, ``clear_default_context``, ``create_session``, … —
used to wrap its work in its own :func:`asyncio.run`.  That creates a **fresh**
event loop and then closes it again, while the objects the conversation built
(``Session``, ``KimiSoul``, toolset, chat provider, sub-process servers,
background tasks, …) keep living and are reused by the next conversation.

Anything those objects created during the previous conversation stays bound to a
loop that is already gone, so the next conversation fails with::

    RuntimeError: Event loop is closed

raised through ``src/kimix/utils/prompt.py`` — the outermost frame of the
traceback is ``prompt()`` calling ``asyncio.run``.

This module gives the process a single per-thread event loop that is created on
first use and **never closed between conversations**: every synchronous entry
point runs its coroutine on it (and reuses it when the loop is already there), so
the loop-bound state of a session stays valid for the whole process lifetime.

The loop is *not* a background loop: it is only driven while a synchronous
caller blocks in :func:`run_on_event_loop`, exactly like ``asyncio.run`` — but
without tearing the loop down in between.  Keeping the loop bound to the calling
thread (instead of a shared background thread) preserves the thread affinity
that transports, signal handlers and sub-process watchers rely on.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

__all__ = [
    "close_process_event_loop",
    "get_process_event_loop",
    "run_on_event_loop",
]

_T = TypeVar("_T")

# Guards creation of the (per-thread) loop when several threads race for the
# first time; the loop itself is stored on a ``threading.local``.
_lock = threading.Lock()
_local = threading.local()


def _install_daemon_executor(loop: asyncio.AbstractEventLoop) -> None:
    """Give *loop* a daemon-thread default executor (best effort).

    The loop lives for the whole process, so the executor behind
    ``asyncio.to_thread()`` is no longer shut down after every conversation.
    The stock ``ThreadPoolExecutor`` uses non-daemon worker threads, which can
    delay interpreter shutdown; ``kimi_cli`` already ships a daemon-thread
    variant for exactly this reason.
    """
    try:
        from kimi_cli.utils.executor import DaemonThreadPoolExecutor
    except Exception:  # pragma: no cover - kimi_cli always ships the helper
        return
    try:
        loop.set_default_executor(DaemonThreadPoolExecutor())
    except Exception:  # pragma: no cover - defensive: never fail loop creation
        pass


def _new_loop() -> asyncio.AbstractEventLoop:
    loop = asyncio.new_event_loop()
    _install_daemon_executor(loop)
    return loop


def get_process_event_loop() -> asyncio.AbstractEventLoop:
    """Return this thread's process-lifetime event loop, creating it on demand.

    The loop is (re-)installed as the thread's current loop on every call, so the
    legacy ``asyncio.get_event_loop()`` call sites (wire requests, hook handles)
    see the live loop instead of a closed one or none at all.
    """
    loop: asyncio.AbstractEventLoop | None = getattr(_local, "loop", None)
    if loop is None or loop.is_closed():
        with _lock:
            loop = getattr(_local, "loop", None)
            if loop is None or loop.is_closed():
                loop = _new_loop()
                _local.loop = loop
    asyncio.set_event_loop(loop)
    return loop


def run_on_event_loop(coro: Coroutine[Any, Any, _T]) -> _T:
    """Run *coro* to completion on this thread's process event loop.

    Unlike :func:`asyncio.run` the loop is **not** closed afterwards: the
    coroutine's loop-bound state (locks, events, futures, tasks, transports,
    HTTP clients) stays valid for the next call.

    Must not be called from a coroutine running on the process loop itself (that
    would deadlock); such a call falls back to a private loop in a worker thread,
    which is the legacy behaviour and is only safe for self-contained work.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        return _run_in_worker_thread(coro)
    return get_process_event_loop().run_until_complete(coro)


def _run_in_worker_thread(coro: Coroutine[Any, Any, _T]) -> _T:
    """Legacy fallback: run *coro* on a private loop in a throw-away thread."""
    result: list[_T] = []
    failure: list[BaseException] = []

    def _target() -> None:
        try:
            result.append(asyncio.run(coro))
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
            failure.append(exc)

    thread = threading.Thread(target=_target, name="kimix-event-loop-fallback")
    thread.start()
    thread.join()
    if failure:
        raise failure[0]
    return result[0]


def close_process_event_loop() -> None:
    """Close this thread's process loop (tests / embedding teardown).

    Pending tasks are cancelled and async generators are shut down before the
    loop is closed, so the teardown is quiet.  A new loop is created on the next
    :func:`run_on_event_loop` call.  Never raises.
    """
    loop: asyncio.AbstractEventLoop | None = getattr(_local, "loop", None)
    _local.loop = None
    if loop is None or loop.is_closed():
        return
    try:
        asyncio.set_event_loop(None)
    except Exception:  # pragma: no cover - defensive
        pass
    try:
        if loop.is_running():  # pragma: no cover - defensive
            return
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
    except Exception:  # pragma: no cover - teardown is best effort
        pass
    finally:
        try:
            loop.close()
        except Exception:  # pragma: no cover - defensive
            pass
