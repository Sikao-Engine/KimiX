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

There is a third, far noisier failure mode when a transport is force-closed
from a *foreign* thread (an :mod:`atexit` sweep, a second loop's teardown)::

    Exception in callback _ProactorBasePipeTransport._call_connection_lost()
    handle: <Handle _ProactorBasePipeTransport._call_connection_lost()>
    Traceback (most recent call last):
      File ".../asyncio/events.py", line 94, in _run
        self._context.run(self._callback, *self._args)
      File ".../asyncio/proactor_events.py", line 160, in _call_connection_lost
        self._protocol.connection_lost(exc)
      File ".../asyncio/base_subprocess.py", line 313, in connection_lost
        self.proc._pipe_connection_lost(self.fd, exc)
    AttributeError: 'NoneType' object has no attribute '_pipe_connection_lost'

``_call_connection_lost`` is not thread-safe and is not idempotent *during* the
call: the "already notified" flag (``_called_connection_lost``) is only set at
the end of its ``finally``, while the subprocess pipe protocol nulls ``proc``
inside ``connection_lost``.  Two threads — the transport's own loop and this
module's sweep — therefore both pass the flag check, both notify the protocol,
and the loser dereferences the ``None`` that the winner just stored.  The same
thing happens sequentially when CPython's ``finally`` raises before setting the
flag (e.g. ``socket.shutdown()`` -> ``ConnectionResetError``, ``pipe.close()``
-> ``OSError``): the transport is left half-closed and the *next* delivery
crashes.

The rules that keep this from happening, implemented below:

* never touch a transport whose loop is running in another thread — the owning
  loop is the only safe place to deliver ``connection_lost``
  (:func:`_owned_by_foreign_running_loop`);
* claim the single "notify the protocol" slot *before* notifying, so a queued
  callback or a concurrent sweep can never deliver a second notification
  (:func:`_claim_connection_lost`).

Every helper is best-effort: teardown must never raise into a tool result or a
loop shutdown path.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import gc
import inspect
import socket
import threading
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

#: Serialises the "who notifies the protocol" claim across threads.  A GC sweep
#: (atexit) and a live loop can look at the same transport at the same time.
_CLAIM_LOCK = threading.Lock()

#: Distinguishes "the protocol has no ``proc`` attribute" from ``proc is None``.
_MISSING = object()


def _transport_loop(transport: Any) -> Any:
    """Return the loop a transport is bound to (``None`` once it is finished)."""
    return getattr(transport, "_loop", None)


def _loop_owner_thread(loop: Any) -> int | None:
    """Thread id currently driving *loop*, when CPython exposes it."""
    owner = getattr(loop, "_thread_id", None)
    return owner if isinstance(owner, int) else None


def _owned_by_foreign_running_loop(transport: Any) -> bool:
    """True when *transport* is driven by a loop running in **another** thread.

    Such a transport must be left alone.  ``call_soon`` is not thread-safe,
    cancelling its proactor futures tears down overlapped I/O the loop is still
    waiting on, and a concurrent ``_call_connection_lost`` races CPython's
    unsynchronised "already notified" flag — the exact race that makes a sweep
    notify a pipe protocol twice and crash with ``'NoneType' object has no
    attribute '_pipe_connection_lost'``.
    """
    loop = _transport_loop(transport)
    if loop is None:
        return False
    try:
        if loop.is_closed() or not loop.is_running():
            return False
    except Exception:  # pragma: no cover - defensive: unknown loop state
        return True
    owner = _loop_owner_thread(loop)
    if owner is None:
        # A running loop that does not publish its owning thread cannot be
        # proven safe, so treat it as foreign.
        return True
    return owner != threading.get_ident()


def _protocol_already_notified(protocol: Any) -> bool:
    """True when the pipe protocol has already seen ``connection_lost``.

    asyncio's ``WriteSubprocessPipeProto`` sets ``disconnected`` first and then
    nulls ``proc``; either marker means a second delivery would dereference the
    ``None``.
    """
    if protocol is None:
        return True
    if getattr(protocol, "disconnected", False):
        return True
    return getattr(protocol, "proc", _MISSING) is None


def _claim_connection_lost(transport: Any) -> Any:
    """Claim *transport*'s single "notify the protocol" slot.

    Returns the protocol to notify, or ``None`` when nobody must be notified
    (already notified, or no protocol attached any more).

    CPython sets ``_called_connection_lost`` only at the very end of
    ``_call_connection_lost``'s ``finally``; when that block raises, the
    transport is left half-closed with the protocol already notified (``proc``
    is ``None``) but the flag still ``False``.  Marking the flag *before*
    notifying — under a lock, so two threads cannot both win — turns every
    later delivery (a queued ``call_soon``, a concurrent sweep) into a no-op.
    """
    with _CLAIM_LOCK:
        if getattr(transport, "_called_connection_lost", False):
            return None
        try:
            transport._called_connection_lost = True
        except Exception:  # pragma: no cover - exotic transport
            return None
        protocol = getattr(transport, "_protocol", None)
        if _protocol_already_notified(protocol):
            return None
        return protocol


def _handle_is_open(sock: Any) -> bool:
    """True when *sock* still owns a usable OS handle."""
    if sock is None:
        return False
    try:
        return sock.fileno() >= 0
    except Exception:
        return False


def _loop_can_run(transport: Any) -> bool:
    """True when the transport's loop still exists and is not closed."""
    loop = _transport_loop(transport)
    if loop is None:
        return False
    try:
        return not loop.is_closed()
    except Exception:  # pragma: no cover - defensive
        return False


def _cancel_pending_futures(transport: Any) -> None:
    for future_attr in ("_read_fut", "_write_fut"):
        future = getattr(transport, future_attr, None)
        if future is None:
            continue
        with contextlib.suppress(Exception):
            future.cancel()
        with contextlib.suppress(Exception):
            setattr(transport, future_attr, None)


def _release_handle(transport: Any) -> None:
    """Close the transport's pipe/socket handle (idempotent, best-effort)."""
    sock = getattr(transport, "_sock", None)
    if sock is None:
        return
    with contextlib.suppress(Exception):
        if _handle_is_open(sock) and hasattr(sock, "shutdown"):
            sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(Exception):
        sock.close()
    with contextlib.suppress(Exception):
        transport._sock = None


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


def _force_close_transport(transport: Any) -> bool:
    """Release *transport*'s handle even when its loop cannot run callbacks.

    Returns ``False`` when the transport was left alone because its loop is
    running in another thread — that loop is the only safe place to deliver
    ``connection_lost``, and racing it is what produced the reported
    ``AttributeError`` (see the module docstring).

    Otherwise the protocol is notified exactly once: the "notified" flag is
    claimed *before* the call, so a queued ``call_soon`` or a concurrent sweep
    cannot re-enter ``protocol.connection_lost`` on a detached protocol.
    """
    if _owned_by_foreign_running_loop(transport):
        return False
    protocol = _claim_connection_lost(transport)
    sock = getattr(transport, "_sock", None)
    handle_open = _handle_is_open(sock)
    # Cancelling an overlapped future (``close()`` does it for ``_read_fut``)
    # on an already-closed handle or through a closed proactor makes the loop
    # report "Cancelling an overlapped future failed" (ERROR_INVALID_HANDLE),
    # so only take that path while both the handle and the loop are usable.
    can_flush = handle_open and _loop_can_run(transport)
    if can_flush:
        with contextlib.suppress(Exception):
            transport.close()
        _cancel_pending_futures(transport)
    else:
        with contextlib.suppress(Exception):
            transport._closing = True
    if protocol is not None:
        with contextlib.suppress(Exception):
            protocol.connection_lost(None)
    _release_handle(transport)
    return True


def _close_transports(transports: list[Any]) -> int:
    """``transport.close()`` each transport owned by this thread's loop."""
    closed = 0
    for transport in transports:
        if _owned_by_foreign_running_loop(transport):
            continue
        with contextlib.suppress(Exception):
            transport.close()
        closed += 1
    return closed


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
    """Release every pipe transport this thread may safely touch.

    Registered as an :mod:`atexit` hook: transports that outlive their event
    loop would otherwise trip asyncio's deallocator during interpreter
    finalisation, printing the very traceback this module exists to prevent.

    Transports owned by a loop that is still running in another thread are
    skipped: an atexit sweep runs on the main thread while daemon worker loops
    may still be alive, and force-closing their pipes from here races
    ``_call_connection_lost`` (the reported ``AttributeError``) and breaks the
    overlapped I/O they are waiting on.  Their handles die with the process.
    """
    released = 0
    for transport in open_pipe_transports():
        if _force_close_transport(transport):
            released += 1
    return released


atexit.register(release_all_pipe_transports)
