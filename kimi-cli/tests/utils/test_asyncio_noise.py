"""Tests for the Windows proactor connection-reset noise filter.

Reported noise (user console, right after the session-server bridge closed a
socket whose peer was already gone)::

    Exception in callback _ProactorBasePipeTransport._call_connection_lost()
    handle: <Handle _ProactorBasePipeTransport._call_connection_lost()>
    Traceback (most recent call last):
      File ".../asyncio/events.py", line 94, in _run
        self._context.run(self._callback, *self._args)
      File ".../asyncio/proactor_events.py", line 167, in _call_connection_lost
        self._sock.shutdown(socket.SHUT_RDWR)
    ConnectionResetError: [WinError 10054] ...

The filter must drop **exactly** that callback/exception pair and keep reporting
everything else.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

import pytest

from kimi_cli.utils.asyncio_noise import (
    _callback_qualname,  # pyright: ignore[reportPrivateUsage]
    install_proactor_reset_filter,
    is_proactor_reset_noise,
    suppress_proactor_reset_noise,
)

WIN_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="proactor loop is Windows-only")
RESET_MESSAGE = "远程主机强迫关闭了一个现有的连接。"


def _transport_method() -> Any:
    """The real callback CPython runs on a proactor socket/pipe transport close."""
    from asyncio.proactor_events import _ProactorBasePipeTransport

    return _ProactorBasePipeTransport._call_connection_lost


def _handle(loop: asyncio.AbstractEventLoop, callback: Any) -> asyncio.Handle:
    return asyncio.Handle(callback, (), loop)


def _noise_context(loop: asyncio.AbstractEventLoop) -> dict[str, Any]:
    callback = _transport_method()
    return {
        "message": f"Exception in callback {_callback_qualname({'handle': _handle(loop, callback)})}()",
        "exception": ConnectionResetError(10054, RESET_MESSAGE),
        "handle": _handle(loop, callback),
    }


def test_matches_the_reported_traceback() -> None:
    loop = asyncio.new_event_loop()
    try:
        context = _noise_context(loop)
        assert "_ProactorBasePipeTransport._call_connection_lost" in context["message"]
        assert is_proactor_reset_noise(context) is True
    finally:
        loop.close()


def test_matches_message_fallback_without_handle() -> None:
    context = {
        "message": ("Exception in callback _ProactorBasePipeTransport._call_connection_lost()"),
        "exception": ConnectionResetError(10054, RESET_MESSAGE),
    }
    assert is_proactor_reset_noise(context) is True


def test_connection_aborted_is_also_teardown_noise() -> None:
    loop = asyncio.new_event_loop()
    try:
        context = _noise_context(loop)
        context["exception"] = ConnectionAbortedError(10053, RESET_MESSAGE)
        assert is_proactor_reset_noise(context) is True
    finally:
        loop.close()


def test_real_asyncio_errors_are_not_filtered() -> None:
    loop = asyncio.new_event_loop()
    try:
        base = _noise_context(loop)
        # 1. Any other exception type must be reported.
        for exc in (ValueError("boom"), RuntimeError("boom"), OSError(9, "bad fd")):
            context = dict(base, exception=exc)
            assert is_proactor_reset_noise(context) is False, exc
        # 2. A different winsock failure (connection refused / timed out) is real.
        for winerror in (10060, 10061):
            context = dict(base, exception=ConnectionResetError(winerror, "other"))
            assert is_proactor_reset_noise(context) is False, winerror
        # 3. The same exception from a different callback must be reported.
        context = dict(base, handle=_handle(loop, loop.stop))
        assert is_proactor_reset_noise(context) is False
        # 4. Garbage in, no crash.
        assert is_proactor_reset_noise({}) is False
        assert is_proactor_reset_noise({"exception": "not-an-exception"}) is False  # type: ignore[dict-item]
    finally:
        loop.close()


@WIN_ONLY
@pytest.mark.asyncio
async def test_real_transport_close_is_suppressed(caplog: pytest.LogCaptureFixture) -> None:
    """Drive the genuine CPython path: transport close on a reset socket."""

    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(_handler, "127.0.0.1", 0)
    assert server.sockets
    port = server.sockets[0].getsockname()[1]
    _reader, writer = await asyncio.open_connection("127.0.0.1", port)
    transport = writer.transport
    loop = asyncio.get_running_loop()
    real_sock = transport._sock  # pyright: ignore[reportPrivateUsage]

    class _ResetOnShutdown:
        """Socket stub: the peer already reset, so shutdown() raises 10054."""

        def fileno(self) -> int:
            return real_sock.fileno()

        def shutdown(self, how: int) -> None:
            raise ConnectionResetError(10054, RESET_MESSAGE)

        def close(self) -> None:
            pass

    try:
        # Baseline: without the filter the traceback is reported as an ERROR.
        transport._sock = _ResetOnShutdown()  # pyright: ignore[reportPrivateUsage]
        with caplog.at_level(logging.ERROR, logger="asyncio"):
            loop.call_soon(transport._call_connection_lost, None)  # pyright: ignore[reportPrivateUsage]
            await asyncio.sleep(0)
        assert any("Exception in callback" in record.message for record in caplog.records), (
            "baseline: the proactor noise did not reproduce"
        )
        caplog.clear()

        # With the filter installed the same callback is silent.
        assert install_proactor_reset_filter(loop) is True
        transport._sock = _ResetOnShutdown()  # pyright: ignore[reportPrivateUsage]
        with caplog.at_level(logging.DEBUG):
            loop.call_soon(transport._call_connection_lost, None)  # pyright: ignore[reportPrivateUsage]
            await asyncio.sleep(0)
        assert not any(record.levelno >= logging.ERROR for record in caplog.records), [
            record.message for record in caplog.records
        ]
        assert any("connection-reset teardown noise" in record.message for record in caplog.records)
    finally:
        # Restore a consistent transport so teardown does not warn.
        transport._sock = real_sock  # pyright: ignore[reportPrivateUsage]
        transport._called_connection_lost = True  # pyright: ignore[reportPrivateUsage]
        real_sock.close()
        server.close()
        await server.wait_closed()


def test_process_wide_filter_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """``suppress_proactor_reset_noise`` patches once and only on Windows."""
    import kimi_cli.utils.asyncio_noise as noise

    original = asyncio.base_events.BaseEventLoop.call_exception_handler
    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "call_exception_handler", original)
    monkeypatch.setattr(noise, "_INSTALLED", False)

    first = suppress_proactor_reset_noise()
    second = suppress_proactor_reset_noise()

    assert first is (sys.platform == "win32")
    assert second is False


@WIN_ONLY
def test_process_wide_filter_drops_noise_and_keeps_real_errors(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import kimi_cli.utils.asyncio_noise as noise

    original = asyncio.base_events.BaseEventLoop.call_exception_handler
    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "call_exception_handler", original)
    monkeypatch.setattr(noise, "_INSTALLED", False)
    suppress_proactor_reset_noise()

    loop = asyncio.new_event_loop()
    try:
        with caplog.at_level(logging.DEBUG):
            loop.call_exception_handler(_noise_context(loop))
            assert not any(record.levelno >= logging.ERROR for record in caplog.records)
        caplog.clear()
        # A real callback error still reaches the default handler.
        with caplog.at_level(logging.ERROR, logger="asyncio"):
            loop.call_exception_handler(
                {
                    "message": "Exception in callback something_else()",
                    "exception": RuntimeError("boom"),
                }
            )
        assert any("something_else" in record.message for record in caplog.records)
    finally:
        loop.close()
