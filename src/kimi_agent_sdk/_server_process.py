"""Nested sub-process servers bound to a session.

Parses the ``server`` key of ``.kimix/config.json`` (see :func:`parse_server_entry`),
spawns each configured entry as a child process, and manages its lifecycle.
Two modes exist per entry:

* **Static** — ``network``/``address``/``port`` are declared directly in the
  entry; the process is considered ready as soon as it is spawned.
* **Dynamic (config-file)** — the entry sets ``config`` to a JSON file path
  (relative to the session work dir).  After spawning, the file is polled with
  an exponential back-off (:data:`CONFIG_WAIT_DELAYS`); its contents
  (``{"network", "address", "port"}``) become the resolved connection info.
  On timeout or invalid content the child is terminated and the entry is
  marked not ready — the main session must never be affected.

Every public entry point swallows and logs exceptions: server management must
never break the session lifecycle.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Literal

import orjson
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

# Delays (seconds) between config-file polls in dynamic mode: check exists;
# then for each delay { sleep(delay); check exists }. Module-level so tests can
# monkeypatch it (read at call time, never bound as a default argument).
CONFIG_WAIT_DELAYS: tuple[float, ...] = (1, 2, 4, 8, 16)

# Timeout (seconds) for reaping a child after terminate() in failure paths.
_TERMINATE_REAP_TIMEOUT = 2.0

# Dynamic-mode config-file reads tolerate transient failures (non-atomic
# writers mid-rewrite, Windows share locks): when a file exists but fails to
# load, re-read it up to _CONFIG_READ_ATTEMPTS - 1 more times with
# _CONFIG_READ_RETRY_DELAY seconds in between before declaring failure.
_CONFIG_READ_ATTEMPTS = 3
_CONFIG_READ_RETRY_DELAY = 0.25

_VALID_NETWORKS = ("tcp", "http")


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


class ServerSpec(BaseModel):
    """Validated description of one nested ``server`` entry."""

    cmd: str
    args: list[str] = Field(default_factory=list)
    cwd: str = "."
    network: Literal["tcp", "http"] = "tcp"
    address: str = "127.0.0.1"
    # Dynamic mode has no static port default; 0 means "unknown / to be read
    # from the config file". Static entries are validated to port > 0.
    port: int = 0
    config: str | None = None


def parse_server_entry(entry: Any, index: int) -> ServerSpec | None:
    """Parse one ``server`` config entry into a :class:`ServerSpec`.

    Logs an error and returns ``None`` for invalid entries (never raises).
    When the dynamic ``config`` key is set, conflicting ``network``/``address``
    /``port`` keys are reported and ignored — the file wins.
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

        raw_config = entry.get("config")
        config: str | None = None
        if raw_config is not None:
            if isinstance(raw_config, str) and raw_config.strip():
                config = raw_config.strip()
            elif isinstance(raw_config, str):
                # Empty string means "no config" (static mode).
                config = None
            else:
                logger.error("server[%d]: 'config' must be a non-empty string", index)
                return None

        data = dict(entry)
        if config is not None:
            conflicting = [k for k in ("network", "address", "port") if k in data]
            if conflicting:
                _log_error(
                    "server[%d]: 'config' is set; ignoring conflicting keys: %s",
                    index,
                    ", ".join(conflicting),
                )
                for k in conflicting:
                    data.pop(k)
            data["config"] = config
        else:
            data.pop("config", None)
            port = data.get("port")
            if (
                not isinstance(port, int)
                or isinstance(port, bool)
                or port <= 0
            ):
                logger.error(
                    "server[%d]: static mode requires a positive integer 'port' (got %r)",
                    index,
                    port,
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

    def __init__(self, spec: ServerSpec, work_dir: Path) -> None:
        self.spec = spec
        self.process: asyncio.subprocess.Process | None = None
        self.work_dir = work_dir
        # Resolved connection info (updated from the config file in dynamic mode).
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

    def _resolve_config_path(self) -> Path:
        cfg = Path(self.spec.config or "")
        if not cfg.is_absolute():
            cfg = self.work_dir / cfg
        return cfg

    async def start(self) -> None:
        """Spawn the child; in dynamic mode wait for its config file.

        Never raises: failures mark ``ready=False`` and record ``error``.
        On a dynamic-mode timeout/invalid file the child is terminated.
        """
        try:
            try:
                self.process = await asyncio.create_subprocess_exec(
                    self.spec.cmd,
                    *self.spec.args,
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
            if self.spec.config is None:
                self.ready = True
                return
            if await self._wait_for_config():
                self.ready = True
            else:
                await self._terminate_and_reap()
        except Exception:
            logger.exception("server: unexpected failure while starting %s", self.spec.cmd)
            self.ready = False

    async def _wait_for_config(self) -> bool:
        """Poll the dynamic-mode config file per :data:`CONFIG_WAIT_DELAYS`.

        Returns True when a valid ``{"network", "address", "port"}`` object was
        read (and applied to this instance). Timeout or invalid content logs an
        error, sets ``self.error``, and returns False — the caller terminates
        the child. Once the file appears, load failures are retried a couple of
        times (see ``_CONFIG_READ_*``) to tolerate non-atomic writers and
        Windows share locks; a file that stays invalid is treated exactly like
        a timeout.
        """
        cfg_path = self._resolve_config_path()
        if cfg_path.exists():
            return await self._load_config_with_retry(cfg_path)
        for delay in CONFIG_WAIT_DELAYS:
            await asyncio.sleep(delay)
            if cfg_path.exists():
                return await self._load_config_with_retry(cfg_path)
        self._fail(
            f"timeout waiting for server config file {cfg_path} "
            f"after {len(CONFIG_WAIT_DELAYS)} retries"
        )
        return False

    async def _load_config_with_retry(self, cfg_path: Path) -> bool:
        """``_load_config_file`` with quick re-reads for transient failures."""
        for attempt in range(1, _CONFIG_READ_ATTEMPTS + 1):
            if self._load_config_file(cfg_path):
                # A failure logged by an earlier transient attempt is resolved;
                # do not leave the stale ``error`` on a server that is ready.
                self.error = None
                return True
            if attempt < _CONFIG_READ_ATTEMPTS:
                await asyncio.sleep(_CONFIG_READ_RETRY_DELAY)
        return False

    def _load_config_file(self, cfg_path: Path) -> bool:
        """Read and validate the config file; apply connection info on success."""
        try:
            data = orjson.loads(cfg_path.read_bytes())
        except Exception as exc:
            self._fail(f"failed to read server config file {cfg_path}: {exc}")
            return False
        if not isinstance(data, dict):
            self._fail(f"server config file {cfg_path} must contain a JSON object")
            return False
        network = data.get("network")
        address = data.get("address")
        port = data.get("port")
        if (
            network not in _VALID_NETWORKS
            or not isinstance(address, str)
            or not isinstance(port, int)
            or isinstance(port, bool)
            or port <= 0
        ):
            self._fail(
                f"server config file {cfg_path} must be an object with "
                f"'network' ('tcp'|'http'), 'address' (str) and 'port' (int > 0); got {data!r}"
            )
            return False
        self.network = network
        self.address = address
        self.port = port
        return True

    def _fail(self, message: str) -> None:
        self.error = message
        # Termination itself is the caller's job (start()/_wait_for_config flow).
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
    work_dir: Path, server_cfg: Any, out: list[ServerProcess] | None = None
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
    """
    servers: list[ServerProcess] = out if out is not None else []
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
            server = ServerProcess(spec, work_dir)
            # Attach before start(): a cancellation *during* start() still
            # finds the (possibly already-spawned) server in the live list.
            servers.append(server)
            await server.start()
    except Exception:
        logger.exception("Failed to start nested sub-process servers")
    except BaseException:
        # Cancellation / interpreter shutdown: do not orphan live children.
        logger.error("server startup interrupted; terminating processes spawned so far")
        stop_server_processes_sync(servers)
        raise
    return servers


async def stop_server_processes(
    servers: list[ServerProcess], timeout: float = 5.0
) -> None:
    """Terminate every still-running child, escalating to kill() after ``timeout``.

    Never raises; safe to call more than once (dead processes are skipped).
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
            except Exception:
                logger.exception("server %s: failed to reap process", server.spec.cmd)
    except Exception:
        logger.exception("Failed to stop nested sub-process servers")


def stop_server_processes_sync(servers: list[ServerProcess] | None) -> None:
    """Best-effort synchronous terminate for ``close_sync``/``__del__`` paths.

    Without an event loop the children cannot be reaped here; on Windows
    ``terminate()`` maps to ``TerminateProcess`` (immediate), which is enough
    to release the processes.  Never raises.
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
