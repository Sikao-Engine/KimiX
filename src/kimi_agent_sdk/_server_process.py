"""Nested sub-process servers bound to a session.

Parses the ``server`` key of ``.kimix/config.json`` (see :func:`parse_server_entry`),
spawns each configured entry as a child process, and manages its lifecycle.

The **client (main process) determines the connection address and port**:

* Before spawning, a free TCP port is allocated (:func:`_find_free_port`).
  ``port`` in an entry is optional and acts as the *preferred starting port*
  to probe (incrementing by one on conflict, up to :data:`MAX_PORT_ATTEMPTS`
  tries); when omitted the OS assigns an ephemeral port.
* The resolved values are substituted into ``cmd`` and ``args`` via
  :func:`_substitute_placeholders` using the tokens ``<address>``, ``<port>``
  and ``<session_id>``, so the child learns where to listen from its own
  command line (e.g. ``--port=<port>``).
* The process is considered ready as soon as it is spawned; the legacy
  config-file handshake (server-determined port) is no longer supported —
  entries declaring a ``config`` key are rejected at parse time.

Every public entry point swallows and logs exceptions: server management must
never break the session lifecycle.

Started servers are also tracked in a module-level live registry
(:func:`get_live_server_processes`) so in-process consumers that cannot reach
the owning ``Session`` object (notably the ``connect`` tool bridge in
``kimi_cli``) can discover them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

# Timeout (seconds) for reaping a child after terminate() in failure paths.
_TERMINATE_REAP_TIMEOUT = 2.0

# Maximum number of consecutive ports probed by :func:`_find_free_port` when
# the preferred starting port (and its successors) are already in use.
MAX_PORT_ATTEMPTS = 100

_VALID_NETWORKS = ("tcp", "http")

# ---------------------------------------------------------------------------
# Live-server registry
#
# Session servers are owned by the SDK ``Session`` object, but tools run inside
# ``kimi_cli`` and cannot see it. This module-level registry lets any in-process
# consumer (e.g. the ``connect`` tool bridge) discover the servers that are
# currently alive. It is guarded by a plain ``threading.Lock`` (registration
# happens on the event-loop thread, reads may happen from anywhere) and every
# entry point swallows exceptions: discovering servers must never break a
# session or a tool call.
# ---------------------------------------------------------------------------

_LIVE_SERVERS: list[ServerProcess] = []
_LIVE_LOCK = threading.Lock()


def _register_live_server(server: ServerProcess) -> None:
    """Add ``server`` to the live registry. Never raises."""
    try:
        with _LIVE_LOCK:
            _LIVE_SERVERS.append(server)
    except Exception:
        logger.exception("live server registry: failed to register %s", server.spec.cmd)


def _unregister_live_servers(servers: list[ServerProcess] | None) -> None:
    """Remove ``servers`` from the live registry, by identity.

    Entries that are not registered are ignored; never raises.
    """
    try:
        targets = list(servers or [])
        if not targets:
            return
        with _LIVE_LOCK:
            _LIVE_SERVERS[:] = [s for s in _LIVE_SERVERS if not any(s is t for t in targets)]
    except Exception:
        logger.exception("live server registry: failed to unregister servers")


def get_live_server_processes() -> list[ServerProcess]:
    """Snapshot (list copy) of every live session server. Never raises.

    Callers must re-check each entry before use: ``ready`` / ``process`` /
    ``process.returncode`` / ``network`` may change at any time (the snapshot
    itself is safe to iterate).
    """
    try:
        with _LIVE_LOCK:
            return list(_LIVE_SERVERS)
    except Exception:
        logger.exception("live server registry: failed to snapshot")
        return []


def _clear_live_registry_for_tests() -> None:
    """Empty the live registry (test isolation helper).

    Does *not* terminate anything: it only forgets the entries, so a test can
    assert registry state without inheriting leftovers from other tests.
    """
    with _LIVE_LOCK:
        _LIVE_SERVERS.clear()


def _log_error(fmt: str, *args: Any) -> None:
    """Log an error and mirror it to ``kimix.ui.printing.print_error`` when importable.

    ``kimix`` sits *above* ``kimi_agent_sdk`` in the dependency stack, so the
    import is lazy and fully guarded: the logger is the source of truth and a
    broken/unavailable UI layer must never affect server management.
    """
    logger.error(fmt, *args)
    try:
        from kimix.ui.printing import print_error

        print_error(fmt % args if args else fmt)
    except Exception:
        pass


def _log_debug(text: str) -> None:
    """Log ``text`` at INFO and mirror it to ``kimix.ui.printing.print_debug``.

    ``kimix`` sits *above* ``kimi_agent_sdk`` in the dependency stack, so the
    import is lazy and fully guarded: the logger is the source of truth and a
    broken/unavailable UI layer must never affect server management.
    """
    logger.info("%s", text)
    try:
        from kimix.ui.printing import print_debug

        print_debug(text)
    except Exception:
        pass


def _render_command(cmd: str, args: list[str]) -> str:
    """Render a command + args as a single shell-like string for logging.

    Uses POSIX quoting only when an argument contains whitespace/quotes, so
    the common case reads cleanly (``python -m mod --port 1234``).
    """
    parts = [cmd]
    for arg in args:
        if arg == "" or any(ch in arg for ch in " \t\"'"):
            escaped = arg.replace("\\", "\\\\").replace('"', '\\"')
            parts.append(f'"{escaped}"')
        else:
            parts.append(arg)
    return " ".join(parts)


def _is_port_free(address: str, port: int) -> bool:
    """True when a TCP socket can bind address:port right now."""
    sock: socket.socket | None = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # No SO_REUSEADDR: the bind must reflect real availability.
        sock.bind((address, port))
        return True
    except OSError:
        return False
    finally:
        if sock is not None:
            sock.close()


def _find_free_port(address: str, preferred: int = 0) -> int:
    """Pick a TCP port that can currently be bound on ``address``.

    * ``preferred <= 0``: bind port 0 and return the OS-assigned ephemeral port.
    * ``preferred > 0``: probe ``preferred``, then ``preferred + 1``, ... up to
      :data:`MAX_PORT_ATTEMPTS` consecutive ports; raise :class:`RuntimeError`
      when all of them are in use.

    Addresses ``"0.0.0.0"`` and ``""`` are probed against the wildcard address
    (INADDR_ANY). This runs synchronously before spawn and is cheap; it stays
    sync so it is trivially testable.
    """
    bind_addr = "" if address in ("0.0.0.0", "") else address
    if preferred <= 0:
        sock: socket.socket | None = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind((bind_addr, 0))
            return int(sock.getsockname()[1])
        except OSError as exc:
            raise RuntimeError(
                f"cannot allocate an ephemeral port on {bind_addr or '0.0.0.0'}: {exc}"
            ) from exc
        finally:
            if sock is not None:
                sock.close()
    port = preferred
    attempts = 0
    for _ in range(MAX_PORT_ATTEMPTS):
        if port > 65535:
            break  # ran off the top of the port range
        attempts += 1
        if _is_port_free(bind_addr, port):
            return port
        port += 1
    raise RuntimeError(
        f"no free port found near {preferred} "
        f"(tried {attempts} consecutive ports: {preferred}..{port - 1})"
    )


def _substitute_placeholders(
    values: list[str], address: str, port: int, session_id: str | None
) -> list[str]:
    """Replace ``<address>`` / ``<port>`` / ``<session_id>`` tokens in ``values``.

    Each token may appear multiple times and inside larger strings (e.g.
    ``--port=<port>``). A missing or empty ``session_id`` becomes the literal
    string ``"default"``.
    """
    sid = session_id if session_id else "default"
    return [
        value.replace("<address>", address).replace("<port>", str(port)).replace("<session_id>", sid)
        for value in values
    ]


class ServerSpec(BaseModel):
    """Validated description of one nested ``server`` entry."""

    cmd: str
    args: list[str] = Field(default_factory=list)
    cwd: str = "."
    network: Literal["tcp", "http"] = "tcp"
    address: str = "127.0.0.1"
    # Optional *preferred starting port* to probe before spawn; 0 (the default)
    # means "auto-assign" (the OS picks an ephemeral port). The finally chosen
    # port is resolved by the client (see _find_free_port), never by the child.
    port: int = 0


def parse_server_entry(entry: Any, index: int) -> ServerSpec | None:
    """Parse one ``server`` config entry into a :class:`ServerSpec`.

    Logs an error and returns ``None`` for invalid entries (never raises).
    The legacy ``config`` key (server-determined port via a config-file
    handshake) is no longer supported: entries that still declare it are
    rejected. ``port`` is optional; when present it must be a positive integer
    and acts as the preferred starting port to probe.
    """
    try:
        if not isinstance(entry, dict):
            logger.error(
                "server[%d]: entry must be a dict, got %s", index, type(entry).__name__
            )
            return None
        cmd = entry.get("cmd")
        if not isinstance(cmd, str) or not cmd.strip():
            logger.error("server[%d]: 'cmd' is missing or empty", index)
            return None

        if "config" in entry:
            _log_error(
                "server[%d]: 'config' key is no longer supported; ignoring entry", index
            )
            return None

        data = dict(entry)
        if "port" in data:
            port = data["port"]
            if not isinstance(port, int) or isinstance(port, bool) or port <= 0:
                logger.error(
                    "server[%d]: 'port' must be a positive integer (got %r)", index, port
                )
                return None

        network = data.get("network", "tcp")
        if network not in _VALID_NETWORKS:
            logger.error(
                "server[%d]: unknown network %r (expected 'tcp' or 'http')", index, network
            )
            return None

        return ServerSpec.model_validate(data)
    except ValidationError as exc:
        logger.error("server[%d]: invalid entry: %s", index, exc)
        return None
    except Exception:
        logger.exception("server[%d]: failed to parse entry", index)
        return None


class ServerProcess:
    """One nested sub-process server bound to a session."""

    def __init__(self, spec: ServerSpec, work_dir: Path, session_id: str | None = None) -> None:
        self.spec = spec
        self.process: asyncio.subprocess.Process | None = None
        self.work_dir = work_dir
        self.session_id = session_id
        # Connection info: ``port`` is resolved by the client before spawn
        # (see start()) and stays at that resolved value for the lifetime of
        # the process.
        self.network: str = spec.network
        self.address: str = spec.address
        self.port: int = spec.port
        self.ready: bool = False
        self.error: str | None = None

    def _resolve_cwd(self) -> Path:
        cwd = Path(self.spec.cwd)
        if not cwd.is_absolute():
            cwd = self.work_dir / cwd
        return cwd

    async def start(self) -> None:
        """Allocate a free port, substitute placeholders, then spawn the child.

        The port is resolved *before* spawning: ``<address>``, ``<port>`` and
        ``<session_id>`` tokens are replaced in ``cmd`` and ``args`` so the
        child learns its listen address from the command line.

        Never raises: failures mark ``ready=False`` and record ``error``.
        """
        try:
            try:
                port = _find_free_port(self.spec.address, self.spec.port)
                self.port = port
                cmd = _substitute_placeholders(
                    [self.spec.cmd], self.spec.address, port, self.session_id
                )[0]
                args = _substitute_placeholders(
                    self.spec.args, self.spec.address, port, self.session_id
                )
                _log_debug(f"server: {_render_command(cmd, args)}")
                self.process = await asyncio.create_subprocess_exec(
                    cmd,
                    *args,
                    cwd=str(self._resolve_cwd()),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except Exception as exc:
                logger.error(
                    "server: failed to spawn %s in %s: %s",
                    self.spec.cmd,
                    self._resolve_cwd(),
                    exc,
                )
                self.error = f"spawn failed: {exc}"
                self.ready = False
                return
            self.network = self.spec.network
            # The spec address may be a wildcard bind address ("0.0.0.0"/"");
            # clients must connect to a concrete address, so normalize it for
            # the live connection info (the child still binds the wildcard).
            self.address = (
                "127.0.0.1" if self.spec.address in ("0.0.0.0", "", "::") else self.spec.address
            )
            self.ready = True
        except Exception:
            logger.exception("server: unexpected failure while starting %s", self.spec.cmd)
            self.ready = False

    def _fail(self, message: str) -> None:
        self.error = message
        logger.error("server %s: %s", self.spec.cmd, message)

    async def _terminate_and_reap(self) -> None:
        """Terminate the child (if alive) and wait briefly for it to exit."""
        proc = self.process
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), _TERMINATE_REAP_TIMEOUT)
        except asyncio.TimeoutError:
            logger.error("server %s did not exit after terminate; killing", self.spec.cmd)
            try:
                proc.kill()
            except Exception:
                logger.exception("server %s: kill() failed", self.spec.cmd)
            try:
                await asyncio.wait_for(proc.wait(), _TERMINATE_REAP_TIMEOUT)
            except Exception:
                logger.exception("server %s: failed to reap process", self.spec.cmd)
        except Exception:
            logger.exception("server %s: failed to terminate process", self.spec.cmd)


async def start_server_processes(
    work_dir: Path,
    server_cfg: Any,
    out: list[ServerProcess] | None = None,
    *,
    session_id: str | None = None,
) -> list[ServerProcess]:
    """Parse the raw ``server`` config value and start every valid entry.

    Returns one :class:`ServerProcess` per parsed spec (check ``ready``);
    invalid entries are logged and skipped.  Non-list values are logged and
    yield an empty list.  Never raises :class:`Exception`.

    ``out`` (optional): a live list owned by the caller; every spawned server
    is appended to it *before* its (awaitable) start completes, so the caller
    sees children as soon as they exist.  If this coroutine is cancelled (or
    otherwise exits via a :class:`BaseException`), the processes spawned so
    far are terminated best-effort before re-raising — no orphaned children.

    ``session_id`` (keyword-only): forwarded to every :class:`ServerProcess`
    and substituted into ``<session_id>`` placeholders in ``cmd``/``args``.

    Every spawned server is registered in the module-level live registry (see
    :func:`get_live_server_processes`) at the same moment it is appended to
    the live list, and unregistered by ``stop_server_processes`` /
    ``stop_server_processes_sync`` (including the cancellation cleanup here).
    """
    servers: list[ServerProcess] = out if out is not None else []
    pending: ServerProcess | None = None
    try:
        if not isinstance(server_cfg, list):
            logger.error(
                "server config must be a list, got %s; ignoring",
                type(server_cfg).__name__,
            )
            return servers
        for index, entry in enumerate(server_cfg):
            spec = parse_server_entry(entry, index)
            if spec is None:
                continue
            server = ServerProcess(spec, work_dir, session_id=session_id)
            # Attach before start(): a cancellation *during* start() still
            # finds the (possibly already-spawned) server in the live list.
            servers.append(server)
            _register_live_server(server)
            pending = server
            await server.start()
            pending = None
    except Exception:
        logger.exception("Failed to start nested sub-process servers")
        # A server whose start() blew up must not linger in the live
        # registry: the caller may discard ``servers`` (out=None) and then
        # nothing would ever unregister it.
        if pending is not None:
            _unregister_live_servers([pending])
    except BaseException:
        # Cancellation / interpreter shutdown: do not orphan live children.
        logger.error("server startup interrupted; terminating processes spawned so far")
        stop_server_processes_sync(servers)
        raise
    return servers


def _reap_without_loop(proc: asyncio.subprocess.Process, timeout: float) -> bool:
    """Best-effort synchronous reap for a child bound to a *different* event loop.

    Used when awaiting ``proc.wait()`` raises ``RuntimeError`` because the
    transport's waiter future is attached to another loop — this happens after
    Ctrl+C: session teardown runs ``asyncio.run()`` in a fresh loop while the
    child was spawned on the interrupted one (and the spawn loop may even be
    closed).  ``terminate()`` has already been issued; here we only make sure
    the OS process is dead and released without needing the original loop.
    Returns True when the exit was observed.
    """
    if os.name == "nt":
        # terminate() on Windows is TerminateProcess: unconditional and
        # immediate, and there are no zombies to reap.  ``proc.returncode``
        # is only updated by the spawn loop's transport, which may be dead,
        # so we cannot (and need not) observe the exit from here.
        return True
    # POSIX: the spawn loop's child watcher is unusable from here, so reap
    # the pid directly.  ChildProcessError means something else (e.g. the
    # transport on a still-running loop) already reaped it.
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        if proc.returncode is not None:
            return True
        try:
            pid, _status = os.waitpid(proc.pid, os.WNOHANG)
            if pid != 0:
                return True
        except (ChildProcessError, OSError):
            return True
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    # Still alive after the grace period: force-kill and reap blocking.
    try:
        proc.kill()
        try:
            os.waitpid(proc.pid, 0)
            return True
        except (ChildProcessError, OSError):
            return True
    except Exception:
        logger.exception("server pid %s: cross-loop kill failed", proc.pid)
    return False


async def stop_server_processes(
    servers: list[ServerProcess], timeout: float = 5.0
) -> None:
    """Terminate every still-running child, escalating to kill() after ``timeout``.

    Never raises; safe to call more than once (dead processes are skipped).
    Every server passed in is removed from the live registry, stopped or not.

    When a child was spawned on a different (possibly interrupted/closed) event
    loop — e.g. teardown via ``asyncio.run()`` after Ctrl+C — awaiting
    ``proc.wait()`` raises ``RuntimeError`` ("attached to a different loop").
    In that case the reap falls back to :func:`_reap_without_loop`, which
    confirms the OS process is dead without the spawn loop.
    """
    try:
        for server in list(servers):
            proc = server.process
            if proc is None or proc.returncode is not None:
                continue
            try:
                proc.terminate()
            except Exception:
                logger.exception("server %s: terminate() failed", server.spec.cmd)
            try:
                await asyncio.wait_for(proc.wait(), timeout)
            except asyncio.TimeoutError:
                logger.error(
                    "server %s did not exit within %.1fs after terminate; killing",
                    server.spec.cmd,
                    timeout,
                )
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout)
                except Exception:
                    logger.exception("server %s: failed to kill/reap process", server.spec.cmd)
            except RuntimeError as exc:
                # Cross-loop teardown (Ctrl+C: asyncio.run in a fresh loop over
                # a process spawned on the interrupted one).  Fall back to a
                # loop-independent reap instead of logging a scary traceback.
                if _reap_without_loop(proc, timeout):
                    logger.debug(
                        "server %s: reaped without its spawn loop (%s)",
                        server.spec.cmd,
                        exc,
                    )
                else:
                    logger.warning(
                        "server %s: terminate() issued but exit could not be "
                        "confirmed (spawn loop unavailable); the process may "
                        "linger until its parent exits",
                        server.spec.cmd,
                    )
            except Exception:
                logger.exception("server %s: failed to reap process", server.spec.cmd)
    except Exception:
        logger.exception("Failed to stop nested sub-process servers")
    finally:
        _unregister_live_servers(servers)


def stop_server_processes_sync(servers: list[ServerProcess] | None) -> None:
    """Best-effort synchronous terminate for ``close_sync``/``__del__`` paths.

    Without an event loop the children cannot be reaped here; on Windows
    ``terminate()`` maps to ``TerminateProcess`` (immediate), which is enough
    to release the processes.  Every server passed in is removed from the live
    registry.  Never raises.
    """
    try:
        for server in list(servers or []):
            proc = server.process
            if proc is None or proc.returncode is not None:
                continue
            try:
                proc.terminate()
            except Exception:
                logger.debug("server %s: sync terminate() failed", server.spec.cmd, exc_info=True)
    except Exception:
        logger.exception("Failed to stop nested sub-process servers synchronously")
    finally:
        _unregister_live_servers(servers)
