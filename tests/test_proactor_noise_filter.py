"""The session loop must not print CPython's proactor connection-reset traceback.

Reported while a normal kimix session was closing sockets whose peer was already
gone (the LSP-bridge sub-process, dropped keep-alive connections)::

    Exception in callback _ProactorBasePipeTransport._call_connection_lost()
    handle: <Handle _ProactorBasePipeTransport._call_connection_lost()>
    Traceback (most recent call last):
      File ".../asyncio/events.py", line 94, in _run
        self._context.run(self._callback, *self._args)
      File ".../asyncio/proactor_events.py", line 167, in _call_connection_lost
        self._sock.shutdown(socket.SHUT_RDWR)
    ConnectionResetError: [WinError 10054] ...

``kimix.utils.event_loop`` installs ``kimi_cli.utils.asyncio_noise`` on the
long-lived process loop, so the noise is demoted to a debug log while every real
asyncio error still reaches the default handler.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from kimix.utils.event_loop import close_process_event_loop, get_process_event_loop

WIN_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="proactor loop is Windows-only")


def _fake_transport_callback() -> Any:
    """A callable whose qualname is the proactor transport's close callback."""

    def _call_connection_lost(*args: Any) -> None:  # pragma: no cover - never run
        pass

    _call_connection_lost.__qualname__ = "_ProactorBasePipeTransport._call_connection_lost"
    return _call_connection_lost


def _noise_context(loop: asyncio.AbstractEventLoop) -> dict[str, Any]:
    callback = _fake_transport_callback()
    return {
        "message": "Exception in callback _ProactorBasePipeTransport._call_connection_lost()",
        "exception": ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接。"),
        "handle": SimpleNamespace(_callback=callback),
    }


def test_session_loop_ignores_proactor_reset_noise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    loop = get_process_event_loop()
    try:
        with caplog.at_level(logging.DEBUG):
            loop.call_exception_handler(_noise_context(loop))
        assert not any(record.levelno >= logging.ERROR for record in caplog.records), [
            record.getMessage() for record in caplog.records
        ]
        caplog.clear()
        # A genuine loop callback error is still reported.
        with caplog.at_level(logging.ERROR, logger="asyncio"):
            loop.call_exception_handler(
                {
                    "message": "Exception in callback task_step()",
                    "exception": RuntimeError("boom"),
                }
            )
        assert any("task_step" in record.getMessage() for record in caplog.records)
    finally:
        close_process_event_loop()


@WIN_ONLY
def test_session_loop_filters_the_real_proactor_callback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Same assertion, but through the real ``Handle``/callback object."""
    from asyncio.proactor_events import _ProactorBasePipeTransport

    loop = get_process_event_loop()
    try:
        handle = asyncio.Handle(_ProactorBasePipeTransport._call_connection_lost, (), loop)
        context = {
            "message": "Exception in callback _ProactorBasePipeTransport._call_connection_lost()",
            "exception": ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接。"),
            "handle": handle,
        }
        with caplog.at_level(logging.DEBUG):
            loop.call_exception_handler(context)
        assert not any(record.levelno >= logging.ERROR for record in caplog.records)
    finally:
        close_process_event_loop()
