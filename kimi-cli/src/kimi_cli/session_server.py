"""Self-contained JSON-RPC 2.0 server/client with TCP and HTTP transports.

This module is the future host for builtin tool methods exposed over RPC. It can
run as a standalone sub-process (``python -m kimi_cli.session_server``) listening
on TCP (newline-delimited JSON) or HTTP (``POST /rpc``) and, optionally, writes
its bound address to a ``--config-out`` JSON file atomically so a parent process
(see ``kimi_agent_sdk._server_process``) can discover it.

Protocol: https://www.jsonrpc.org/specification
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import inspect
import logging
import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import orjson
from aiohttp import web
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# Error codes (JSON-RPC 2.0 predefined set)
# ----------------------------------------------------------------------------

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Maximum size (bytes) of one newline-delimited TCP frame in either direction.
# The asyncio StreamReader default (64 KiB) silently truncates / corrupts
# large payloads (e.g. big ``read_file`` results), so both server and client
# explicitly raise it.
TCP_STREAM_LIMIT = 16 * 1024 * 1024

# How many extra (non-matching) response lines the TCP client will skip while
# waiting for the response whose ``id`` matches its request (bounded so a
# permanently desynchronised stream fails instead of hanging).
_MAX_MISMATCHED_RESPONSES = 8


class JsonRpcError(Exception):
    """Application-level error raised by handlers; returned as the error object."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data

    def to_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        return error

    @classmethod
    def from_dict(cls, error: dict[str, Any]) -> JsonRpcError:
        return cls(
            int(error.get("code", INTERNAL_ERROR)),
            str(error.get("message", "")),
            error.get("data"),
        )


# ----------------------------------------------------------------------------
# Protocol models
# ----------------------------------------------------------------------------


class JsonRpcRequest(BaseModel):
    """A JSON-RPC 2.0 request or notification (notification = no ``id`` field)."""

    jsonrpc: Literal["2.0"] = "2.0"
    method: str
    params: dict[str, Any] | list[Any] | None = None
    id: int | str | None = None

    def is_notification(self) -> bool:
        return "id" not in self.model_fields_set


class JsonRpcErrorObject(BaseModel):
    """The ``error`` member of a JSON-RPC response."""

    code: int
    message: str
    data: Any = None


class JsonRpcResponse(BaseModel):
    """A JSON-RPC 2.0 response (exactly one of ``result``/``error`` is set)."""

    jsonrpc: Literal["2.0"] = "2.0"
    id: int | str | None = None
    result: Any = None
    error: JsonRpcErrorObject | None = None

    def to_dict(self) -> dict[str, Any]:
        """Wire form per JSON-RPC 2.0 §5.

        ``jsonrpc`` and ``id`` are ALWAYS present (``id`` may be null), and
        exactly one of ``result`` / ``error`` is set — including a null
        ``result`` on success, which ``exclude_none`` would wrongly drop.
        Within ``error``, ``data`` is only present when not None.
        """
        out: dict[str, Any] = {"jsonrpc": self.jsonrpc, "id": self.id}
        if self.error is not None:
            out["error"] = self.error.model_dump(mode="json", exclude_none=True)
        else:
            out["result"] = _serialize_result(self.result)
        return out


# ----------------------------------------------------------------------------
# Method registry
# ----------------------------------------------------------------------------

Handler = Callable[..., Any]


@dataclasses.dataclass
class _RegisteredMethod:
    name: str
    handler: Handler
    params_model: type[BaseModel] | None = None


def _serialize_result(result: Any) -> Any:
    if isinstance(result, BaseModel):
        return result.model_dump(mode="json")
    if dataclasses.is_dataclass(result) and not isinstance(result, type):
        return dataclasses.asdict(result)
    return result


class MethodRegistry:
    """Registry of JSON-RPC methods with optional pydantic param validation."""

    def __init__(self) -> None:
        self._methods: dict[str, _RegisteredMethod] = {}

    def method(
        self,
        name: str | None = None,
        *,
        params_model: type[BaseModel] | None = None,
    ) -> Callable[[Handler], Handler]:
        """Decorator registering a sync or async handler.

        With ``params_model``: the params dict is validated via
        ``params_model.model_validate(params)`` and the handler receives the
        model instance. Without it: the handler receives the raw
        ``dict | list | None`` params. The return value is serialized:
        ``BaseModel`` -> ``model_dump(mode="json")``, dataclass -> ``asdict``,
        anything else passes through.
        """

        def decorator(func: Handler) -> Handler:
            method_name = name if name is not None else func.__name__
            self._methods[method_name] = _RegisteredMethod(
                name=method_name, handler=func, params_model=params_model
            )
            return func

        return decorator

    def register(
        self,
        name: str,
        handler: Handler,
        *,
        params_model: type[BaseModel] | None = None,
    ) -> Handler:
        """Non-decorator registration, equivalent to ``method(...)``."""
        self._methods[name] = _RegisteredMethod(
            name=name, handler=handler, params_model=params_model
        )
        return handler

    def get(self, name: str) -> _RegisteredMethod | None:
        """Return the registered method entry, or None."""
        return self._methods.get(name)

    def names(self) -> list[str]:
        """All registered method names, in registration order."""
        return list(self._methods.keys())

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._methods

    async def dispatch(self, method: str, params: dict[str, Any] | list[Any] | None) -> Any:
        """Invoke a method. Raises JsonRpcError per the spec's error mapping."""
        entry = self._methods.get(method)
        if entry is None:
            raise JsonRpcError(METHOD_NOT_FOUND, f"Method not found: {method}")
        call_params: Any = params
        if entry.params_model is not None:
            try:
                raw = params if params is not None else {}
                call_params = entry.params_model.model_validate(raw)
            except ValidationError as exc:
                raise JsonRpcError(INVALID_PARAMS, "Invalid params", str(exc)) from exc
        try:
            if inspect.iscoroutinefunction(entry.handler):
                result = await entry.handler(call_params)
            else:
                result = entry.handler(call_params)
        except JsonRpcError:
            raise
        except Exception as exc:
            raise JsonRpcError(INTERNAL_ERROR, "Internal error", repr(exc)) from exc
        return _serialize_result(result)


# ----------------------------------------------------------------------------
# Core request handling (shared by all transports)
# ----------------------------------------------------------------------------


def _sanitize_id(value: Any) -> int | str | None:
    """Coerce an unvalidated wire ``id`` into an echoable response id.

    JSON-RPC 2.0 ids are numbers or strings (``null`` reserved); anything
    else (bool, list, dict, float, ...) echoes back as ``None`` so response
    construction can never raise on attacker-controlled input.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, str)):
        return value
    return None


def _error_response(req_id: Any, exc: JsonRpcError) -> JsonRpcResponse:
    return JsonRpcResponse(
        id=_sanitize_id(req_id),
        error=JsonRpcErrorObject(code=exc.code, message=exc.message, data=exc.data),
    )


def _result_response(req_id: Any, result: Any) -> JsonRpcResponse:
    return JsonRpcResponse(id=_sanitize_id(req_id), result=result)


async def _handle_single(registry: MethodRegistry, msg: Any) -> JsonRpcResponse | None:
    """Handle one (non-batch) message; returns None for notifications.

    The echoed id is always the *raw* ``msg["id"]`` passed through
    :func:`_sanitize_id` — never the pydantic-validated value and never an
    unvalidated object, so malformed ids (list/dict/float/bool) degrade to
    ``null`` instead of raising.
    """
    raw_id = msg.get("id") if isinstance(msg, dict) else None
    if (
        not isinstance(msg, dict)
        or msg.get("jsonrpc") != "2.0"
        or not isinstance(msg.get("method"), str)
    ):
        return _error_response(raw_id, JsonRpcError(INVALID_REQUEST, "Invalid Request"))
    try:
        request = JsonRpcRequest.model_validate(msg)
    except ValidationError as exc:
        return _error_response(
            raw_id, JsonRpcError(INVALID_REQUEST, "Invalid Request", str(exc))
        )
    if request.is_notification():
        try:
            await registry.dispatch(request.method, request.params)
        except JsonRpcError as exc:
            logger.warning("notification %r failed: %s", request.method, exc)
        except Exception:
            # Notifications are never answered, even on catastrophic handler
            # failure — writing a response would desynchronise the TCP stream.
            logger.exception("notification %r crashed", request.method)
        return None
    try:
        result = await registry.dispatch(request.method, request.params)
        return _result_response(raw_id, result)
    except JsonRpcError as exc:
        return _error_response(raw_id, exc)


async def handle_message(
    registry: MethodRegistry, message: Any
) -> dict[str, Any] | list[dict[str, Any]] | None:
    """Handle a decoded JSON-RPC message (object or batch array).

    Returns a response dict for requests, a list of response dicts for batches,
    or None when nothing should be written back (pure notifications).
    """
    if isinstance(message, list):
        if not message:
            return _error_response(None, JsonRpcError(INVALID_REQUEST, "Invalid Request")).to_dict()
        # return_exceptions: one poisonous item (unexpected exception) must not
        # collapse the whole batch; it becomes a per-item INTERNAL_ERROR.
        responses = await asyncio.gather(
            *(_handle_single(registry, m) for m in message), return_exceptions=True
        )
        out: list[dict[str, Any]] = []
        for item, result in zip(message, responses, strict=True):
            if isinstance(result, asyncio.CancelledError):
                raise result  # outer cancellation: never swallow
            if isinstance(result, BaseException):
                logger.error("batch item failed: %r", result)
                if isinstance(item, dict) and "id" not in item:
                    continue  # notification: never answered, even on failure
                item_id = item.get("id") if isinstance(item, dict) else None
                out.append(
                    _error_response(
                        item_id, JsonRpcError(INTERNAL_ERROR, "Internal error")
                    ).to_dict()
                )
            elif result is not None:
                out.append(result.to_dict())
        return out if out else None
    response = await _handle_single(registry, message)
    return response.to_dict() if response is not None else None


def _dumps(obj: Any) -> bytes:
    return orjson.dumps(obj)


def _loads(data: bytes | str) -> Any:
    return orjson.loads(data)


# ----------------------------------------------------------------------------
# TCP transport (newline-delimited JSON)
# ----------------------------------------------------------------------------


async def _tcp_client_connected(
    registry: MethodRegistry, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Handle one TCP connection: read JSON lines, write JSON-line responses."""
    peer = writer.get_extra_info("peername")
    try:
        while True:
            try:
                line = await reader.readline()
            except (ConnectionError, asyncio.LimitOverrunError, ValueError):
                break
            if not line:  # EOF
                break
            line = line.strip()
            if not line:
                continue
            try:
                message = _loads(line)
            except orjson.JSONDecodeError:
                out = _error_response(None, JsonRpcError(PARSE_ERROR, "Parse error")).to_dict()
            else:
                try:
                    out = await handle_message(registry, message)
                except Exception:
                    logger.exception("unexpected error handling TCP message")
                    out = _error_response(
                        None, JsonRpcError(INTERNAL_ERROR, "Internal error")
                    ).to_dict()
            if out is None:  # notification
                continue
            writer.write(_dumps(out) + b"\n")
            try:
                await writer.drain()
            except ConnectionError:
                break
    finally:
        with contextlib.suppress(Exception):
            writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        logger.debug("TCP connection closed: %r", peer)


# ----------------------------------------------------------------------------
# HTTP transport (aiohttp, POST /rpc)
# ----------------------------------------------------------------------------


def _json_response(payload: Any, status: int = 200) -> web.Response:
    return web.Response(
        body=_dumps(payload), status=status, content_type="application/json"
    )


async def _http_rpc_handler(request: web.Request, registry: MethodRegistry) -> web.Response:
    parse_error = _error_response(None, JsonRpcError(PARSE_ERROR, "Parse error")).to_dict()
    try:
        body = await request.read()
    except Exception:
        return _json_response(parse_error)
    if not body:
        return _json_response(
            _error_response(None, JsonRpcError(INVALID_REQUEST, "Invalid Request")).to_dict()
        )
    try:
        message = _loads(body)
    except orjson.JSONDecodeError:
        return _json_response(parse_error)
    try:
        out = await handle_message(registry, message)
    except Exception:
        logger.exception("unexpected error handling HTTP message")
        out = _error_response(None, JsonRpcError(INTERNAL_ERROR, "Internal error")).to_dict()
    if out is None:  # notification
        return web.Response(status=204)
    return _json_response(out)


def _create_http_app(registry: MethodRegistry) -> web.Application:
    app = web.Application()

    async def rpc_handler(request: web.Request) -> web.Response:
        return await _http_rpc_handler(request, registry)

    app.router.add_post("/rpc", rpc_handler)
    # aiohttp answers 405 for wrong methods on /rpc and 404 for unknown paths.
    return app


# ----------------------------------------------------------------------------
# Server
# ----------------------------------------------------------------------------


class JsonRpcServer:
    """JSON-RPC 2.0 server over TCP or HTTP on top of a MethodRegistry."""

    def __init__(self, registry: MethodRegistry, host: str = "127.0.0.1", port: int = 0) -> None:
        self.registry = registry
        self.host = host
        self.port = port
        self._tcp_server: asyncio.AbstractServer | None = None
        self._http_runner: web.AppRunner | None = None
        self._network: str | None = None
        self._bound_host: str | None = None
        self._bound_port: int | None = None
        self._stopped = asyncio.Event()
        self._stopped.set()

    @property
    def network(self) -> str | None:
        return self._network

    @property
    def is_running(self) -> bool:
        """True between ``start()`` and ``stop()`` (public watcher API)."""
        return not self._stopped.is_set()

    async def wait_stopped(self) -> None:
        """Block until :meth:`stop` is called."""
        await self._stopped.wait()

    @property
    def bound_port(self) -> int:
        if self._bound_port is None:
            raise RuntimeError("server is not running")
        return self._bound_port

    async def start_tcp(self) -> tuple[str, int]:
        """Bind the TCP transport. ``port=0`` -> ephemeral. Returns (host, port)."""
        registry = self.registry

        async def client_connected(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await _tcp_client_connected(registry, reader, writer)

        server = await asyncio.start_server(
            client_connected, self.host, self.port, limit=TCP_STREAM_LIMIT
        )
        self._tcp_server = server
        sock = server.sockets[0] if server.sockets else None
        addr = sock.getsockname() if sock is not None else (self.host, self.port)
        self._bound_host = str(addr[0])
        self._bound_port = int(addr[1])
        self._network = "tcp"
        self._stopped.clear()
        return self._bound_host, self._bound_port

    async def start_http(self) -> tuple[str, int]:
        """Bind the HTTP transport. ``port=0`` -> ephemeral. Returns (host, port)."""
        app = _create_http_app(self.registry)
        runner = web.AppRunner(app, handle_signals=False)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        self._http_runner = runner
        port = self.port
        if port == 0:
            addresses = runner.addresses
            if addresses:
                port = int(addresses[0][1])  # type: ignore[index]
            else:  # pragma: no cover - fallback for exotic aiohttp versions
                server = getattr(runner, "server", None)
                sockets = getattr(server, "sockets", None) or []
                if sockets:
                    port = int(sockets[0].getsockname()[1])
        self._bound_host = self.host
        self._bound_port = int(port)
        self._network = "http"
        self._stopped.clear()
        return self._bound_host, self._bound_port

    async def start(self, network: str) -> tuple[str, int]:
        """Start the given transport ("tcp" or "http"). Returns (host, port)."""
        if network == "tcp":
            return await self.start_tcp()
        if network == "http":
            return await self.start_http()
        raise ValueError(f"Unknown network: {network!r} (expected 'tcp' or 'http')")

    async def stop(self) -> None:
        """Stop whichever transport is running. Idempotent."""
        if self._tcp_server is not None:
            self._tcp_server.close()
            with contextlib.suppress(Exception):
                await self._tcp_server.wait_closed()
            self._tcp_server = None
        if self._http_runner is not None:
            with contextlib.suppress(Exception):
                await self._http_runner.cleanup()
            self._http_runner = None
        self._network = None
        self._bound_host = None
        self._bound_port = None
        self._stopped.set()

    async def serve_forever(self, network: str) -> tuple[str, int]:
        """Start the transport and wait until stop() is called. Returns the bound address."""
        address = await self.start(network)
        await self.wait_stopped()
        return address


# ----------------------------------------------------------------------------
# Client
# ----------------------------------------------------------------------------


class JsonRpcClient:
    """JSON-RPC 2.0 client for the TCP and HTTP transports of JsonRpcServer.

    TCP: newline-delimited JSON. HTTP: ``POST /rpc`` via aiohttp ClientSession.
    """

    def __init__(self) -> None:
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._http_url: str | None = None
        self._session: Any = None  # aiohttp.ClientSession, created lazily
        self._id_counter = 0
        self._lock = asyncio.Lock()

    @classmethod
    async def connect_tcp(cls, host: str, port: int) -> JsonRpcClient:
        """Open a TCP connection to a JsonRpcServer TCP transport."""
        client = cls()
        client._reader, client._writer = await asyncio.open_connection(
            host, port, limit=TCP_STREAM_LIMIT
        )
        return client

    @classmethod
    async def connect_http(cls, host_or_url: str, port: int | None = None) -> JsonRpcClient:
        """Create an HTTP client. Accepts a full URL or a bare host + port."""
        client = cls()
        if host_or_url.startswith("http://") or host_or_url.startswith("https://"):
            client._http_url = host_or_url.rstrip("/") + "/rpc"
        else:
            if port is None:
                raise ValueError("port is required when connecting by host")
            client._http_url = f"http://{host_or_url}:{port}/rpc"
        return client

    async def close(self) -> None:
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
            self._writer = None
            self._reader = None
        if self._session is not None:
            with contextlib.suppress(Exception):
                await self._session.close()
            self._session = None

    async def __aenter__(self) -> JsonRpcClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    def _build_request(
        self, method: str, params: dict[str, Any] | list[Any] | None
    ) -> dict[str, Any]:
        request: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": self._next_id()}
        if params is not None:
            request["params"] = params
        return request

    async def _send_and_receive(self, payload: Any, timeout: float) -> Any:
        data = _dumps(payload)
        async with self._lock:
            if self._writer is not None and self._reader is not None:
                self._writer.write(data + b"\n")
                await asyncio.wait_for(self._writer.drain(), timeout)
                return await self._read_tcp_response(payload, timeout)
            if self._http_url is not None:
                return await self._send_http(data, timeout)
            raise ConnectionError("client is not connected")

    async def _read_tcp_response(self, payload: Any, timeout: float) -> Any:
        """Read one JSON-line response on the TCP transport, matching ids.

        A timed-out earlier call can leave a stale response buffered; for
        single (non-batch) requests we skip leading lines whose ``id`` does
        not match ours (bounded by ``_MAX_MISMATCHED_RESPONSES``) so the
        stream cannot desynchronise subsequent calls. Batch payloads (list)
        have no single id to match — the whole list is one response, so the
        first line is returned as-is.
        """
        assert self._reader is not None  # set by _send_and_receive caller
        single = isinstance(payload, dict)
        expected_id = payload.get("id") if single else None
        response: Any = None
        for _ in range(_MAX_MISMATCHED_RESPONSES + 1):
            try:
                line = await asyncio.wait_for(self._reader.readline(), timeout)
            except (asyncio.LimitOverrunError, ValueError) as exc:
                raise ConnectionError(
                    f"server response line exceeds TCP_STREAM_LIMIT ({TCP_STREAM_LIMIT} bytes)"
                ) from exc
            if not line:
                raise ConnectionError("TCP connection closed by server")
            response = _loads(line)
            if not single or not isinstance(response, dict):
                return response
            if response.get("id") == expected_id:
                return response
                logger.warning(
                    "skipping stale TCP response (id=%r, expected %r)",
                    response.get("id"),
                    expected_id,
                )
        # Every line read was a stale (id-mismatched) response: a well-behaved
        # server never does this, so refuse to hand an unrelated payload back
        # to the caller (it would otherwise be surfaced as a valid result).
        if single and isinstance(response, dict):
            raise ConnectionError(
                f"no matching response id ({expected_id!r}) after "
                f"{_MAX_MISMATCHED_RESPONSES} stale lines"
            )
        return response

    async def _send_http(self, data: bytes, timeout: float) -> Any:
        import aiohttp

        if self._session is None:
            self._session = aiohttp.ClientSession()
        async with self._session.post(
            self._http_url,
            data=data,
            headers={"Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status == 204:
                return None
            body = await resp.read()
            try:
                return _loads(body)
            except orjson.JSONDecodeError as exc:
                raise JsonRpcError(
                    INTERNAL_ERROR,
                    f"Invalid response from server: {exc}",
                    body[:200].decode("utf-8", "replace"),
                ) from exc

    @staticmethod
    def _unwrap(response: Any) -> Any:
        if response is None:
            return None
        if isinstance(response, dict) and "error" in response:
            raise JsonRpcError.from_dict(response["error"])
        if isinstance(response, dict):
            return response.get("result")
        return response

    async def call(
        self,
        method: str,
        params: dict[str, Any] | list[Any] | None = None,
        timeout: float = 30.0,
    ) -> Any:
        """Send a request and return its result. Raises JsonRpcError on error."""
        payload = self._build_request(method, params)
        response = await self._send_and_receive(payload, timeout)
        return self._unwrap(response)

    async def call_batch(
        self,
        requests: list[tuple[str, dict[str, Any] | list[Any] | None]],
        timeout: float = 30.0,
    ) -> list[Any]:
        """Send an array of (method, params) requests as one batch; ordered results.

        Each result is the unwrapped value; errors are raised as soon as
        encountered in order (the first JsonRpcError wins).
        """
        payload: list[dict[str, Any]] = []
        ids: list[int | str] = []
        for method, params in requests:
            req = self._build_request(method, params)
            ids.append(req["id"])
            payload.append(req)
        response = await self._send_and_receive(payload, timeout)
        if isinstance(response, dict):
            response = [response]
        if not isinstance(response, list):
            raise JsonRpcError(INTERNAL_ERROR, "Batch response is not a list", response)
        by_id = {r.get("id"): r for r in response if isinstance(r, dict)}
        return [self._unwrap(by_id.get(i)) for i in ids]

    async def notify(self, method: str, params: dict[str, Any] | list[Any] | None = None) -> None:
        """Send a notification (no id); the server writes nothing back."""
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        data = _dumps(payload)
        async with self._lock:
            if self._writer is not None:
                self._writer.write(data + b"\n")
                await self._writer.drain()
            elif self._http_url is not None:
                await self._send_http(data, 30.0)
            else:
                raise ConnectionError("client is not connected")


# ----------------------------------------------------------------------------
# Builtin methods (the script is the future host for builtin tool methods)
# ----------------------------------------------------------------------------

# Filesystem root confinement for the builtin read_file/write_file methods, set
# from the ``--root`` CLI argument. ``None`` (default) means unrestricted.
_SERVER_ROOT: Path | None = None


def _set_server_root(path: str | Path) -> None:
    """Set :data:`_SERVER_ROOT` (called from the CLI before serving)."""
    global _SERVER_ROOT
    _SERVER_ROOT = Path(path)


def _resolve_path(raw_path: str) -> Path:
    """Resolve a ``params.path`` value, enforcing ``--root`` when configured.

    Relative paths resolve against the root; a resolved path that escapes the
    root is rejected with INVALID_PARAMS. Without a root the path is used
    as-is (unrestricted, the historical behaviour).
    """
    if _SERVER_ROOT is None:
        return Path(raw_path)
    root = _SERVER_ROOT.resolve()
    path = Path(raw_path)
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve()
    # normcase: on Windows resolve() keeps the caller's letter case, and
    # is_relative_to compares case-sensitively; POSIX normcase is identity.
    resolved_n = Path(os.path.normcase(str(resolved)))
    root_n = Path(os.path.normcase(str(root)))
    if not resolved_n.is_relative_to(root_n):
        raise JsonRpcError(
            INVALID_PARAMS, f"path escapes server root: {raw_path!r}"
        )
    return resolved


class EchoParams(BaseModel):
    text: str


class EchoResult(BaseModel):
    text: str
    length: int


class ReadFileParams(BaseModel):
    path: str
    offset: int = 0
    limit: int = 2000


class WriteFileParams(BaseModel):
    path: str
    content: str
    append: bool = False


class AddParams(BaseModel):
    a: float
    b: float


class BeforeToolParams(BaseModel):
    """Params of the ``before_tool`` hook (see docs/tool_connect_bridge.md)."""

    tool: str
    action: str = "pre"
    path: str | None = None
    paths: list[str] | None = None
    cwd: str | None = None


class BeforeToolResult(BaseModel):
    """Result of ``before_tool``; the bridge appends ``message`` to the tool."""

    message: str
    tool: str
    action: str
    handled: bool = True


DEFAULT_REGISTRY = MethodRegistry()


@DEFAULT_REGISTRY.method("ping")
def _ping(_params: Any = None) -> str:
    return "pong"


@DEFAULT_REGISTRY.method("echo", params_model=EchoParams)
def _echo(params: EchoParams) -> EchoResult:
    return EchoResult(text=params.text, length=len(params.text))


@DEFAULT_REGISTRY.method("read_file", params_model=ReadFileParams)
def _read_file(params: ReadFileParams) -> dict[str, Any]:
    path = _resolve_path(params.path)
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    total = len(lines)
    selected = lines[params.offset : params.offset + params.limit]
    return {"path": str(path), "lines": selected, "total": total}


@DEFAULT_REGISTRY.method("write_file", params_model=WriteFileParams)
def _write_file(params: WriteFileParams) -> dict[str, Any]:
    path = _resolve_path(params.path)
    data = params.content.encode("utf-8")
    if params.append:
        with path.open("ab") as f:
            f.write(data)
    else:
        path.write_bytes(data)
    return {"path": str(path), "bytes_written": len(data)}


@DEFAULT_REGISTRY.method("add", params_model=AddParams)
def _add(params: AddParams) -> dict[str, float]:
    return {"sum": params.a + params.b}


@DEFAULT_REGISTRY.method("before_tool", params_model=BeforeToolParams)
def _before_tool(params: BeforeToolParams) -> BeforeToolResult:
    """Default echo implementation of the tool pre-call hook.

    It only "repeats" the call back so the round-trip is provable end to end
    (pure sync, no file IO, no ``--root`` interaction). External servers — an
    LSP bridge, for example — replace this method with real diagnostics.
    """
    message = f"ack before_tool: tool={params.tool} action={params.action}"
    if params.path is not None:
        message += f" path={params.path}"
    if params.paths is not None:
        message += f" paths={params.paths}"
    return BeforeToolResult(message=message, tool=params.tool, action=params.action)


# ----------------------------------------------------------------------------
# CLI entry: python -m kimi_cli.session_server
# ----------------------------------------------------------------------------


def _atomic_write_json(data: Any, path: Path) -> None:
    """Write JSON atomically (tmp file + os.replace) so watchers never see partials."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(orjson.dumps(data, option=orjson.OPT_INDENT_2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


async def _run(args: argparse.Namespace) -> None:
    if args.root:
        _set_server_root(args.root)
    server = JsonRpcServer(DEFAULT_REGISTRY, host=args.host, port=args.port)
    address, port = await server.start(args.network)
    if args.config_out:
        _atomic_write_json(
            {"network": args.network, "address": address, "port": port},
            Path(args.config_out),
        )
    print(
        f"session_server listening on {args.network}://{address}:{port}",
        file=sys.stderr,
        flush=True,
    )
    watcher: asyncio.Task[None] | None = None
    if args.stop_file:
        stop_file = Path(args.stop_file)

        async def watch_stop_file() -> None:
            while server.is_running:
                if stop_file.exists():
                    await server.stop()
                    return
                await asyncio.sleep(0.2)

        watcher = asyncio.create_task(watch_stop_file())
    try:
        await server.wait_stopped()
    finally:
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
        await server.stop()


def main(argv: list[str] | None = None) -> None:
    """CLI entry so the server can run as a nested sub-process."""
    parser = argparse.ArgumentParser(
        prog="session_server", description="JSON-RPC 2.0 session server (TCP or HTTP)."
    )
    parser.add_argument("--network", choices=["tcp", "http"], default="tcp")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument(
        "--config-out", default=None, metavar="PATH",
        help="Write {network, address, port} JSON here atomically once bound.",
    )
    parser.add_argument(
        "--stop-file", default=None, metavar="PATH",
        help="Exit once this file appears.",
    )
    parser.add_argument(
        "--root", default=None, metavar="PATH",
        help="Confine read_file/write_file paths to this directory (default: no limit).",
    )
    args = parser.parse_args(argv)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run(args))


if __name__ == "__main__":
    main()
