"""Deterministic teardown of asyncio sub-process pipe transports.

On Windows the Proactor event loop gives every ``asyncio.create_subprocess_*``
child three pipe transports (stdin/stdout/stderr).  A transport is released only
when the loop dispatches its EOF/``connection_lost`` callback, or when somebody
calls ``transport.close()``.  A transport that outlives its loop is closed
*underneath* asyncio by ``loop.close()`` (the IOCP proactor closes the pipe
handles), so asyncio still believes the pipe is open.  When the garbage
collector later runs the transport deallocator it builds a warning message from
``__repr__``, which calls ``PipeHandle.fileno()`` on the already-closed pipe::

    Exception ignored while calling deallocator
    <function _ProactorBasePipeTransport.__del__ at 0x...>:
    Traceback (most recent call last):
      File ".../asyncio/proactor_events.py", line 116, in __del__
        _warn(f"unclosed transport {self!r}", ResourceWarning, source=self)
      File ".../asyncio/proactor_events.py", line 80, in __repr__
        info.append(f'fd={self._sock.fileno()}')
      File ".../asyncio/windows_utils.py", line 102, in fileno
        raise ValueError("I/O operation on closed pipe")
    ValueError: I/O operation on closed pipe

The noise therefore has two independent causes, and this module covers both:

* a child process that is abandoned (killed, timed out or cancelled) without its
  three pipes being closed — :func:`reap_process` /
  :func:`close_pipe_transports`;
* an event loop that is closed while any of its sub-process pipes is still open
  — :func:`close_loop_pipe_transports` /
  :func:`aclose_loop_pipe_transports`.

Every helper is best-effort: teardown must never raise into a tool result or a
loop shutdown path.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import gc
import inspect
from typing import Any

__all__ = [
    "aclose_loop_pipe_transports",
    "close_loop_pipe_transports",
    "close_pipe_transports",
    "open_pipe_transports",
    "pipe_transports",
    "reap_process",
    "release_all_pipe_transports",
]

#: The three standard streams every ``create_subprocess_*`` child gets.
_PIPE_FDS = (0, 1, 2)


def _proactor_pipe_transport_type() -> type | None:
    """Return the Proactor pipe-transport class, or ``None`` off Windows."""
    try:
        from asyncio.proactor_events import _ProactorBasePipeTransport
    except Exception:  # pragma: no cover - only importable on Windows
        return None
    return _ProactorBasePipeTransport


def _asyncio_process(proc: Any) -> Any:
    """Unwrap a KAOS-style process wrapper to the asyncio process it holds."""
    if getattr(proc, "_transport", None) is not None:
        return proc
    inner = getattr(proc, "_process", None)
    if inner is not None and getattr(inner, "_transport", None) is not None:
        return inner
    return proc


def pipe_transports(proc: Any) -> list[Any]:
    """Return the pipe transports of an asyncio sub-process (may be empty)."""
    transport = getattr(_asyncio_process(proc), "_transport", None)
    get_pipe_transport = getattr(transport, "get_pipe_transport", None)
    if get_pipe_transport is None:
        return []
    transports: list[Any] = []
    for fd in _PIPE_FDS:
        try:
            pipe = get_pipe_transport(fd)
        except Exception:
            continue
        if pipe is not None:
            transports.append(pipe)
    return transports


def close_pipe_transports(proc: Any) -> int:
    """Close the stdin/stdout/stderr transports of *proc*.

    Call this once the child has been reaped and its output consumed.  Returns
    the number of transports that were still open.
    """
    closed = 0
    for transport in pipe_transports(proc):
        try:
            if transport.is_closing():
                continue
            transport.close()
            closed += 1
        except Exception:
            continue
    return closed


async def reap_process(
    proc: Any,
    *,
    kill: bool = False,
    close_pipes: bool = True,
    timeout: float | None = None,
) -> int | None:
    """Close stdin, optionally kill, reap *proc*, then release its pipes.

    ``timeout`` bounds the reap; the pipes are closed either way, so a child
    that refuses to die cannot keep three transports alive.  Never raises;
    returns the child's return code when known.
    """
    if proc is None:
        return None
    stdin = getattr(proc, "stdin", None)
    if stdin is not None:
        with contextlib.suppress(Exception):
            stdin.close()
    if kill and getattr(proc, "returncode", None) is None:
        killer = getattr(proc, "kill", None)
        if killer is not None:
            with contextlib.suppress(BaseException):
                killed = killer()
                if inspect.isawaitable(killed):
                    await killed
    with contextlib.suppress(Exception):
        if timeout is None:
            await proc.wait()
        else:
            await asyncio.wait_for(proc.wait(), timeout)
    if close_pipes:
        close_pipe_transports(proc)
    return getattr(proc, "returncode", None)


def open_pipe_transports(loop: asyncio.AbstractEventLoop | None = None) -> list[Any]:
    """Every still-open Proactor pipe transport, optionally only *loop*'s.

    Used at loop teardown, where transports are no longer reachable from the
    loop itself and a garbage-collector sweep is the only way to find them.
    """
    transport_type = _proactor_pipe_transport_type()
    if transport_type is None:
        return []
    found: list[Any] = []
    for obj in gc.get_objects():
        if not isinstance(obj, transport_type):
            continue
        try:
            if obj._sock is None:
                continue
            if loop is not None and obj._loop is not loop:
                continue
        except Exception:
            continue
        found.append(obj)
    return found


def _force_close_transport(transport: Any) -> None:
    """Release *transport*'s handle even when its loop cannot run callbacks."""
    with contextlib.suppress(Exception):
        transport.close()
    for future_attr in ("_read_fut", "_write_fut"):
        future = getattr(transport, future_attr, None)
        if future is not None:
            with contextlib.suppress(Exception):
                future.cancel()
            with contextlib.suppress(Exception):
                setattr(transport, future_attr, None)
    connection_lost = getattr(transport, "_call_connection_lost", None)
    if connection_lost is not None:
        with contextlib.suppress(Exception):
            connection_lost(None)
            return
    sock = getattr(transport, "_sock", None)
    if sock is not None:
        with contextlib.suppress(Exception):
            sock.close()
        with contextlib.suppress(Exception):
            transport._sock = None


def _close_transports(transports: list[Any]) -> int:
    for transport in transports:
        with contextlib.suppress(Exception):
            transport.close()
    return len(transports)


def _force_close_remaining(loop: asyncio.AbstractEventLoop) -> None:
    for transport in open_pipe_transports(loop):
        _force_close_transport(transport)


def close_loop_pipe_transports(loop: asyncio.AbstractEventLoop, *, flush: bool = True) -> int:
    """Close every open sub-process pipe transport owned by *loop*.

    Call this immediately **before** ``loop.close()``.  When *flush* is set and
    the loop is neither running nor closed, one loop iteration runs so the
    ``connection_lost`` callbacks really release the pipe handles.  Anything the
    loop can no longer service is released directly, so asyncio's deallocator
    can never warn about (and then crash on) an unclosed transport.
    """
    transport_count = 0
    try:
        transport_count = _close_transports(open_pipe_transports(loop))
        if transport_count and flush and not loop.is_running() and not loop.is_closed():
            with contextlib.suppress(Exception):
                loop.run_until_complete(asyncio.sleep(0))
    finally:
        with contextlib.suppress(Exception):
            _force_close_remaining(loop)
    return transport_count


async def aclose_loop_pipe_transports(loop: asyncio.AbstractEventLoop) -> int:
    """Async variant of :func:`close_loop_pipe_transports` for a live loop."""
    transport_count = 0
    try:
        transport_count = _close_transports(open_pipe_transports(loop))
        if transport_count:
            with contextlib.suppress(BaseException):
                await asyncio.sleep(0)
    finally:
        with contextlib.suppress(Exception):
            _force_close_remaining(loop)
    return transport_count


def release_all_pipe_transports() -> int:
    """Release every pipe transport still holding a handle; return the count.

    Registered as an :mod:`atexit` hook: transports that outlive their event
    loop would otherwise trip asyncio's deallocator during interpreter
    finalisation, printing the very traceback this module exists to prevent.
    """
    transports = open_pipe_transports()
    for transport in transports:
        _force_close_transport(transport)
    return len(transports)


atexit.register(release_all_pipe_transports)
