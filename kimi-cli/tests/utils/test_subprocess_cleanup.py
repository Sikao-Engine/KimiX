"""Tests for deterministic sub-process pipe teardown.

The regression these tests pin down (user console, right after ``bye.`` while a
background worker loop was still winding down)::

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

``_ProactorBasePipeTransport._call_connection_lost`` is neither thread-safe nor
safe to deliver twice: the "already notified" flag is only set at the end of its
``finally`` while the pipe protocol nulls ``proc`` *inside* ``connection_lost``.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

import kimi_cli.utils.subprocess_cleanup as subprocess_cleanup
from kimi_cli.utils.subprocess_cleanup import (
    _claim_connection_lost,  # pyright: ignore[reportPrivateUsage]
    _force_close_transport,  # pyright: ignore[reportPrivateUsage]
    _owned_by_foreign_running_loop,  # pyright: ignore[reportPrivateUsage]
    close_pipe_transports,
    open_pipe_transports,
    release_all_pipe_transports,
)

WIN_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="proactor loop is Windows-only")


class _FakeSock:
    """Stand-in for ``asyncio.windows_utils.PipeHandle``."""

    def __init__(self) -> None:
        self.closed = False
        self.shutdowns = 0

    def fileno(self) -> int:
        if self.closed:
            raise ValueError("I/O operation on closed pipe")
        return 7

    def shutdown(self, _how: int) -> None:
        self.shutdowns += 1

    def close(self) -> None:
        self.closed = True


class _FakeSubprocessTransport:
    """The parent transport a pipe protocol reports to."""

    def __init__(self) -> None:
        self.lost: list[int] = []

    def _pipe_connection_lost(self, fd: int, _exc: BaseException | None) -> None:
        self.lost.append(fd)


class _FakePipeProtocol:
    """Mirror of ``asyncio.base_subprocess.WriteSubprocessPipeProto``."""

    def __init__(self, fd: int = 1) -> None:
        self.fd = fd
        self.proc: _FakeSubprocessTransport | None = _FakeSubprocessTransport()
        self.disconnected = False
        self.calls = 0

    def connection_lost(self, exc: BaseException | None) -> None:
        self.calls += 1
        self.disconnected = True
        # Exactly what CPython does: this is the line that raises AttributeError
        # when the protocol is notified a second time.
        assert self.proc is not None, "'NoneType' object has no attribute '_pipe_connection_lost'"
        self.proc._pipe_connection_lost(self.fd, exc)
        self.proc = None


class _FakeTransport:
    """Minimal ``_ProactorBasePipeTransport`` double."""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
        protocol: _FakePipeProtocol | None = None,
        sock: _FakeSock | None = None,
    ) -> None:
        self._loop = loop
        self._protocol = protocol if protocol is not None else _FakePipeProtocol()
        self._sock: _FakeSock | None = sock if sock is not None else _FakeSock()
        self._read_fut: asyncio.Future[Any] | None = None
        self._write_fut: asyncio.Future[Any] | None = None
        self._called_connection_lost = False
        self.closed = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon(self._call_connection_lost, None)

    def _call_connection_lost(self, exc: BaseException | None) -> None:
        """CPython's implementation, including its flag-at-the-end ordering."""
        if self._called_connection_lost:
            return
        try:
            self._protocol.connection_lost(exc)  # trailing finally mirrors CPython
        finally:
            if self._sock is not None:
                self._sock.close()
            self._sock = None
            self._called_connection_lost = True


@contextlib.contextmanager
def _loop_in_thread() -> Iterator[asyncio.AbstractEventLoop]:
    """A loop that is *running in another thread*, like a background worker."""
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _run() -> None:
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    thread = threading.Thread(target=_run, name="subprocess-cleanup-test", daemon=True)
    thread.start()
    assert ready.wait(5), "test loop failed to start"
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()


# --------------------------------------------------------------------------- #
# Claiming the single notification slot
# --------------------------------------------------------------------------- #


def test_force_close_notifies_the_protocol_exactly_once() -> None:
    """A second force-close, and the queued ``close()`` callback, are no-ops."""
    loop = asyncio.new_event_loop()
    try:
        transport = _FakeTransport(loop=loop)
        protocol = transport._protocol
        assert isinstance(protocol, _FakePipeProtocol)

        assert _force_close_transport(transport) is True
        assert _force_close_transport(transport) is True
        loop.run_until_complete(asyncio.sleep(0))  # deliver the queued callback

        assert protocol.calls == 1
        assert transport.closed is True
        assert transport._called_connection_lost is True
        assert transport._sock is None
    finally:
        loop.close()


def test_force_close_repairs_a_half_closed_transport() -> None:
    """CPython leaves the flag unset when its ``finally`` raises.

    The protocol is already detached (``proc is None``) at that point, so the
    next delivery would raise ``AttributeError``.  The claim must repair the
    state without notifying the protocol again.
    """
    transport = _FakeTransport()
    protocol = transport._protocol
    assert isinstance(protocol, _FakePipeProtocol)
    # 1st delivery completed the handshake, then CPython's finally raised
    # before ``_called_connection_lost = True``.
    protocol.connection_lost(None)
    assert protocol.proc is None
    transport._called_connection_lost = False
    transport._sock = None

    assert _force_close_transport(transport) is True

    assert protocol.calls == 1  # not notified twice
    assert transport._called_connection_lost is True


def test_claim_is_idempotent() -> None:
    transport = _FakeTransport()
    first = _claim_connection_lost(transport)
    second = _claim_connection_lost(transport)
    assert first is not None
    assert second is None


def test_concurrent_force_close_notifies_the_protocol_once() -> None:
    """The atexit sweep and a loop teardown may race on the same transport."""

    class _SlowPipeProtocol(_FakePipeProtocol):
        """Widens the window in which a second thread could re-enter."""

        def connection_lost(self, exc: BaseException | None) -> None:
            time.sleep(0.05)
            super().connection_lost(exc)

    transport = _FakeTransport(protocol=_SlowPipeProtocol())
    protocol = transport._protocol
    assert isinstance(protocol, _SlowPipeProtocol)
    barrier = threading.Barrier(2)

    def _sweep() -> None:
        barrier.wait()
        _force_close_transport(transport)

    threads = [threading.Thread(target=_sweep) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert protocol.calls == 1


def test_claim_skips_an_already_detached_protocol() -> None:
    transport = _FakeTransport()
    protocol = transport._protocol
    assert isinstance(protocol, _FakePipeProtocol)
    protocol.proc = None

    assert _claim_connection_lost(transport) is None
    assert transport._called_connection_lost is True


# --------------------------------------------------------------------------- #
# Foreign threads
# --------------------------------------------------------------------------- #


def test_foreign_running_loop_is_detected() -> None:
    with _loop_in_thread() as loop:
        transport = _FakeTransport(loop=loop)
        assert _owned_by_foreign_running_loop(transport) is True

    idle = asyncio.new_event_loop()
    try:
        assert _owned_by_foreign_running_loop(_FakeTransport(loop=idle)) is False
    finally:
        idle.close()
    assert _owned_by_foreign_running_loop(_FakeTransport(loop=None)) is False


def test_force_close_leaves_a_foreign_loop_transport_alone() -> None:
    """The owning loop is the only safe place to deliver ``connection_lost``."""
    with _loop_in_thread() as loop:
        transport = _FakeTransport(loop=loop)
        protocol = transport._protocol
        assert isinstance(protocol, _FakePipeProtocol)
        sock = transport._sock
        assert sock is not None

        assert _force_close_transport(transport) is False

        assert transport.closed is False
        assert transport._called_connection_lost is False
        assert sock.closed is False
        assert protocol.calls == 0


def test_release_all_skips_transports_of_a_live_foreign_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _loop_in_thread() as loop:
        transport = _FakeTransport(loop=loop)
        sock = transport._sock
        assert sock is not None
        monkeypatch.setattr(
            subprocess_cleanup,
            "open_pipe_transports",
            lambda loop=None: [transport],
        )

        assert release_all_pipe_transports() == 0

        assert sock.closed is False
        assert transport._called_connection_lost is False


# --------------------------------------------------------------------------- #
# Real proactor transports (Windows)
# --------------------------------------------------------------------------- #

_LONG_RUNNING_CHILD = "import time; time.sleep(30)"


async def _spawn_child() -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _LONG_RUNNING_CHILD,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


@WIN_ONLY
def test_release_all_survives_a_half_closed_real_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduces the reported crash state end-to-end with real asyncio objects."""
    loop = asyncio.new_event_loop()
    errors: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    proc = loop.run_until_complete(_spawn_child())
    try:
        transport = proc._transport
        assert transport is not None
        pipe = transport.get_pipe_transport(1)
        assert pipe is not None
        protocol = pipe._protocol
        # A completed first delivery, then CPython's finally raised before the
        # flag was set: the exact state left behind by a reset socket/pipe.
        pipe._call_connection_lost(None)  # pyright: ignore[reportPrivateUsage]
        assert protocol.proc is None
        pipe._called_connection_lost = False  # pyright: ignore[reportPrivateUsage]
        # Queue the second delivery exactly like close()/_force_close() do,
        # without cancelling an overlapped future on the dead handle (that
        # would add CPython's own "Cancelling an overlapped future failed").
        pipe._closing = True  # pyright: ignore[reportPrivateUsage]
        loop.call_soon(pipe._call_connection_lost, None)  # pyright: ignore[reportPrivateUsage]

        monkeypatch.setattr(
            subprocess_cleanup,
            "open_pipe_transports",
            lambda loop=None: [pipe],
        )
        assert release_all_pipe_transports() == 1

        loop.run_until_complete(asyncio.sleep(0.1))
        assert errors == []
    finally:
        proc.kill()
        loop.run_until_complete(proc.wait())
        # Release the pipes this test did not force-close before the loop goes
        # away, so the teardown itself cannot emit deallocator noise.
        close_pipe_transports(proc)
        loop.run_until_complete(asyncio.sleep(0.05))
        loop.close()


@WIN_ONLY
def test_close_pipe_transports_releases_real_handles() -> None:
    loop = asyncio.new_event_loop()
    errors: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    proc = loop.run_until_complete(_spawn_child())
    try:
        transport = proc._transport
        assert transport is not None
        pipes = [transport.get_pipe_transport(fd) for fd in (0, 1, 2)]
        assert all(pipe is not None for pipe in pipes)

        assert close_pipe_transports(proc) == 3
        loop.run_until_complete(asyncio.sleep(0.1))

        assert errors == []
        assert open_pipe_transports(loop) == []
    finally:
        proc.kill()
        loop.run_until_complete(proc.wait())
        loop.close()
