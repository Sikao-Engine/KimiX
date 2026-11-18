"""Silence the Windows proactor connection-reset teardown noise (WinError 10054).

CPython closes a proactor socket transport through
``asyncio.proactor_events._ProactorBasePipeTransport._call_connection_lost()``,
which shuts the socket down in a ``finally`` block::

    if hasattr(self._sock, 'shutdown') and self._sock.fileno() != -1:
        self._sock.shutdown(socket.SHUT_RDWR)

When the peer has already reset the connection — a killed sub-process, a
keep-alive HTTP connection the server dropped, a session-server bridge torn down
at exit — that ``shutdown()`` is the first operation to observe the reset and
raises ``ConnectionResetError: [WinError 10054]``. It is raised *inside a loop
callback*, so it bypasses every ``try``/``except`` of the code that closed the
stream and reaches the loop's default exception handler, which prints::

    Exception in callback _ProactorBasePipeTransport._call_connection_lost()
    handle: <Handle _ProactorBasePipeTransport._call_connection_lost()>
    Traceback (most recent call last):
      File ".../asyncio/events.py", line 94, in _run
        self._context.run(self._callback, *self._args)
      File ".../asyncio/proactor_events.py", line 167, in _call_connection_lost
        self._sock.shutdown(socket.SHUT_RDWR)
    ConnectionResetError: [WinError 10054] ...

The socket is already dead at that point; the shutdown is best-effort cleanup
(CPython gh-83413, still open). The traceback is pure noise: nothing the
application did is wrong, nothing can be retried, and the socket is closed by
the transport's deallocator anyway.

:func:`suppress_proactor_reset_noise` installs a **narrow, idempotent** filter:
only a connection-reset/abort ``OSError`` raised from that exact transport
callback is demoted to a debug log. Every other asyncio exception — including any
other ``OSError``, from any other callback — is reported exactly as before.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "install_proactor_reset_filter",
    "is_proactor_reset_noise",
    "suppress_proactor_reset_noise",
]

#: The callback CPython runs when a proactor transport notices the peer is gone.
_TRANSPORT_QUALNAME = "_ProactorBasePipeTransport._call_connection_lost"
#: WSAECONNABORTED / WSAECONNRESET — the only winsock codes that mean "the peer
#: is gone", and the only ones this filter may drop.
_RESET_WINERRORS = frozenset({10053, 10054})
#: Exception types a dead peer surfaces as (``ConnectionResetError`` is an
#: ``OSError``; ``BrokenPipeError``/``ConnectionAbortedError`` share the path).
_RESET_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
)

#: Set once the process-wide filter is installed (idempotent installs).
_INSTALLED = False


def _callback_qualname(context: dict[str, Any]) -> str:
    """Return the failed callback's qualified name, or '' when unknown."""
    handle = context.get("handle")
    callback = getattr(handle, "_callback", None)
    if callback is None:
        return ""
    name = getattr(callback, "__qualname__", None) or getattr(callback, "__name__", None)
    return name if isinstance(name, str) else ""


def _is_winsock_reset(exception: BaseException) -> bool:
    """True when *exception* reports a "peer is gone" winsock code.

    Windows socket errors carry ``winerror``; a ``ConnectionResetError`` built
    from a numeric code (tests, wrappers) carries the same value in ``errno``.
    An exception with no numeric code at all is only trusted on Windows — there
    the only producer of this callback is the proactor transport itself.
    """
    winerror = getattr(exception, "winerror", None)
    if winerror is not None:
        return winerror in _RESET_WINERRORS
    errno = getattr(exception, "errno", None)
    if errno is not None:
        return errno in _RESET_WINERRORS
    return sys.platform == "win32"


def is_proactor_reset_noise(context: dict[str, Any]) -> bool:
    """True when *context* is the proactor "reset during close" callback noise.

    Deliberately strict: the exception must be a connection-reset family error
    carrying ``WSAECONNABORTED``/``WSAECONNRESET`` (``winerror``, or the same
    code in ``errno``) **and** it must come from
    ``_ProactorBasePipeTransport._call_connection_lost``. Anything else is a
    real error and must keep reaching the default handler.
    """
    if not isinstance(context, dict):
        return False
    exception = context.get("exception")
    if not isinstance(exception, BaseException):
        return False
    if not isinstance(exception, _RESET_EXCEPTIONS):
        return False
    if not _is_winsock_reset(exception):
        return False
    qualname = _callback_qualname(context)
    if qualname:
        return qualname.endswith(_TRANSPORT_QUALNAME)
    # No handle (or a wrapped callback): fall back to the rendered message,
    # which ``events.Handle._run`` builds from the callback name.
    message = context.get("message")
    return isinstance(message, str) and _TRANSPORT_QUALNAME in message


def install_proactor_reset_filter(loop: asyncio.AbstractEventLoop | None = None) -> bool:
    """Install the reset-noise filter on *loop* (default: the current loop).

    Unlike :func:`suppress_proactor_reset_noise` this only touches one loop,
    which is what tests and embedders that drive their own loop want. Returns
    ``True`` when the filter was attached; never raises.
    """
    try:
        if loop is None:
            loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()

        def _handler(loop_: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
            if is_proactor_reset_noise(context):
                logger.debug(
                    "Ignoring proactor connection-reset teardown noise: %r",
                    context.get("exception"),
                )
                return
            if previous is not None:
                previous(loop_, context)
            else:
                loop_.default_exception_handler(context)

        loop.set_exception_handler(_handler)
        return True
    except Exception:  # pragma: no cover - filtering must never break a loop
        return False


def suppress_proactor_reset_noise() -> bool:
    """Filter the proactor reset noise for **every** loop of this process.

    Patches ``BaseEventLoop.call_exception_handler`` once, so loops we do not
    create ourselves (uvicorn's, a library's, a test harness') are covered too.
    Call it from process entry points only; it is idempotent and a no-op off
    Windows. Returns ``True`` the first time it installs the filter.
    """
    global _INSTALLED
    if _INSTALLED:
        return False
    _INSTALLED = True
    if sys.platform != "win32":
        return False
    try:
        original = asyncio.base_events.BaseEventLoop.call_exception_handler
    except Exception:  # pragma: no cover - asyncio always has it
        return False
    if getattr(original, "_kimix_reset_filter", False):  # pragma: no cover - defensive
        return False

    def _call_exception_handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        if is_proactor_reset_noise(context):
            logger.debug(
                "Ignoring proactor connection-reset teardown noise: %r",
                context.get("exception"),
            )
            return
        original(loop, context)

    _call_exception_handler._kimix_reset_filter = True  # type: ignore[attr-defined]
    asyncio.base_events.BaseEventLoop.call_exception_handler = _call_exception_handler  # type: ignore[method-assign]
    return True
