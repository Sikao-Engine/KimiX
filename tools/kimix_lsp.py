#!/usr/bin/env python
"""Kimix LSP-bridge session server: Python syntax check on write/edit.

A standalone JSON-RPC 2.0 session server (``docs/session_server_rpc.md``) —
pure stdlib, no ``kimi_cli`` import — following the reference pattern of
``tests/dummy_rpc_server.py`` (docs §12: "any process speaking this protocol
can act as a session server"). It implements the builtin method set
(``ping``, ``echo``, ``read_file``, ``write_file``, ``add``) with the wire
semantics of docs §5, but replaces the ``before_tool`` echo with a syntax
check.

Why standalone: the SDK spawns the entry's ``cmd`` directly
(``create_subprocess_exec``, docs §8.1) and on Windows a bare ``python``
resolves to the system interpreter, not necessarily the environment that has
``kimi_cli`` installed. Depending only on the standard library makes the
``.kimix/config.json`` entry work under any interpreter.

What it does:

* The connect tool bridge (``docs/tool_connect_bridge.md``) calls
  ``before_tool`` before every file-tool call and appends the returned
  ``message`` to the tool result as ``[server] ...``.
* ``action`` ``write`` / ``edit`` on a ``*.py`` target → the current on-disk
  content is compiled with :func:`compile` — the same check performed by
  ``python -m py_compile`` and ``tools/syntax_check.py``. Any syntax error is
  returned as ``message`` (``path:line:offset: msg``).
* No syntax error, a non-Python file, or a ``write`` creating a brand-new
  file that does not exist yet → empty ``message`` → the bridge adds nothing
  to the tool result ("return nothing if there is no syntax error").

Semantics note: the bridge fires *before* the tool runs and does not forward
the tool's content parameter (only ``tool``/``action``/``path``/``paths``/
``cwd``, bridge contract §5), so the check reflects the file as it is on disk
at call time (the pre-write / pre-edit state), never the content about to be
applied. Results are cached per (path, mtime, size) so repeated calls on an
untouched file stay well inside the bridge's ~5 s per-server budget.

CLI contract is identical to ``python -m kimi_cli.session_server``
(``--network``, ``--host``, ``--port``, ``--stop-file``, ``--root``,
``--config-out``), so it drops into a ``.kimix/config.json`` server entry
as-is: the SDK allocates a free port and substitutes ``<address>``/``<port>``
into the arguments before the spawn (docs §8.2), and this script binds
exactly what it is handed.

Usage::

    python tools/kimix_lsp.py --network tcp --host 127.0.0.1 --port 0 \
        --config-out .kimix/network.json --stop-file stop.flag --root .
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import tempfile
import tokenize
from pathlib import Path
from typing import Any

# NOTE: stdlib ``json`` is used deliberately — this script must run under any
# interpreter (see module docstring), so the project's orjson/msgspec
# performance rules do not apply here.

# JSON-RPC 2.0 pre-defined error codes (docs/session_server_rpc.md §3).
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Same framing limit as the real server: 16 MiB per TCP line.
TCP_STREAM_LIMIT = 16 * 1024 * 1024

#: Actions (write / edit) that trigger a syntax check on Python targets.
_CHECK_ACTIONS = frozenset({"write", "edit"})

#: Cap on the (path, mtime, size) → message cache; evicts oldest on overflow.
_CACHE_MAX = 128

#: message | None per resolved path, keyed by (path, mtime_ns, size).
_result_cache: dict[tuple[str, int, int], str | None] = {}

_SERVER_ROOT: Path | None = None


class RpcError(Exception):
    """Handler-level error carrying a JSON-RPC error code."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


# ── Python syntax check (the LSP part) ───────────────────────────────────────


def _resolve_target(path_str: str, cwd: str | None) -> Path:
    """Resolve a raw (possibly relative) tool path against the tool's cwd."""
    path = Path(path_str)
    if not path.is_absolute() and cwd:
        path = Path(cwd) / path
    return path


def _compile_source(path: Path) -> str | None:
    """Compile the file like ``python -m py_compile``; return a diagnostic or None.

    ``tokenize.open`` honors PEP 263 coding cookies exactly like the
    interpreter does. ``SyntaxError`` / ``ValueError`` (e.g. null bytes) are
    reported as ``path:line:offset: msg``; anything else escapes so the JSON
    RPC layer maps it to -32603 (bridge failure isolation, §7).
    """
    try:
        with tokenize.open(str(path)) as f:
            source = f.read()
    except (SyntaxError, ValueError) as exc:  # bad encoding cookie etc.
        return f"{path}:1:1: {type(exc).__name__}: {exc}"
    try:
        compile(source, str(path), "exec")
    except SyntaxError as exc:
        line = exc.lineno or 0
        offset = exc.offset or 0
        return f"{path}:{line}:{offset}: {exc.msg}"
    except ValueError as exc:  # e.g. source code string cannot contain null bytes
        return f"{path}:1:1: {type(exc).__name__}: {exc}"
    return None


def syntax_check_file(path: Path) -> str | None:
    """Syntax-check ``path`` (cached by mtime/size); None when nothing to say.

    Returns None for missing/non-file targets and for files that compile
    cleanly, so callers can treat None as "append nothing to the tool result".
    """
    try:
        st = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key in _result_cache:
        return _result_cache[key]
    message = _compile_source(path)
    if len(_result_cache) >= _CACHE_MAX:
        _result_cache.pop(next(iter(_result_cache)))
    _result_cache[key] = message
    return message


def syntax_check_target(path_str: str, cwd: str | None) -> str | None:
    """Syntax-check one tool target; only ``*.py`` files are ever checked."""
    path = _resolve_target(path_str, cwd)
    if path.suffix != ".py":
        return None
    return syntax_check_file(path)


def syntax_before_tool(params: Any) -> dict[str, Any]:
    """``before_tool`` handler: syntax-check on write/edit, else stay silent."""
    tool = _require(params, "tool", str)
    action = _optional(params, "action", str, "pre")
    message = ""
    if action in _CHECK_ACTIONS:
        target = params.get("path") if isinstance(params, dict) else None
        if target is None and isinstance(params, dict) and params.get("paths"):
            target = params["paths"][0]
        if isinstance(target, str):
            cwd = params.get("cwd") if isinstance(params, dict) else None
            message = syntax_check_target(target, cwd if isinstance(cwd, str) else None) or ""
    return {"message": message, "tool": tool, "action": action, "handled": True}


# ── builtin methods (docs/session_server_rpc.md §5) ──────────────────────────


def _resolve_path(raw_path: Any) -> Path:
    """``--root`` confinement for the file methods (§6), mirroring the real one."""
    if not isinstance(raw_path, str) or not raw_path:
        raise RpcError(INVALID_PARAMS, "'path' must be a non-empty string")
    if _SERVER_ROOT is None:
        return Path(raw_path)
    root = _SERVER_ROOT.resolve()
    path = Path(raw_path)
    if not path.is_absolute():
        path = root / path
    resolved = Path(os.path.normcase(str(path.resolve())))
    root_n = Path(os.path.normcase(str(root)))
    if not resolved.is_relative_to(root_n):
        raise RpcError(INVALID_PARAMS, f"path escapes server root: {raw_path!r}")
    return path.resolve()


def _require(params: Any, key: str, typ: type) -> Any:
    if not isinstance(params, dict) or key not in params:
        raise RpcError(INVALID_PARAMS, f"missing required param {key!r}")
    value = params[key]
    if not isinstance(value, typ) or isinstance(value, bool):
        raise RpcError(
            INVALID_PARAMS, f"param {key!r} must be {typ.__name__}, got {value!r}"
        )
    return value


def _optional(params: Any, key: str, typ: type, default: Any) -> Any:
    if not isinstance(params, dict) or key not in params or params[key] is None:
        return default
    value = params[key]
    if not isinstance(value, typ) or isinstance(value, bool):
        raise RpcError(
            INVALID_PARAMS, f"param {key!r} must be {typ.__name__}, got {value!r}"
        )
    return value


def rpc_ping(params: Any) -> str:
    """Health check: params ignored entirely, bare-string result."""
    return "pong"


def rpc_echo(params: Any) -> dict[str, Any]:
    text = _require(params, "text", str)
    return {"text": text, "length": len(text)}


def rpc_read_file(params: Any) -> dict[str, Any]:
    path = _resolve_path(_require(params, "path", str))
    offset = _optional(params, "offset", int, 0)
    limit = _optional(params, "limit", int, 2000)
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    return {"path": str(path), "lines": lines[offset : offset + limit], "total": len(lines)}


def rpc_write_file(params: Any) -> dict[str, Any]:
    path = _resolve_path(_require(params, "path", str))
    content = _require(params, "content", str)
    append = params.get("append", False) if isinstance(params, dict) else False
    if not isinstance(append, bool):
        raise RpcError(INVALID_PARAMS, f"param 'append' must be bool, got {append!r}")
    data = content.encode("utf-8")
    if append:
        with path.open("ab") as f:
            f.write(data)
    else:
        path.write_bytes(data)
    return {"path": str(path), "bytes_written": len(data)}


def rpc_add(params: Any) -> dict[str, Any]:
    a = _require(params, "a", (int, float))
    b = _require(params, "b", (int, float))
    return {"sum": float(a) + float(b)}


METHODS: dict[str, Any] = {
    "ping": rpc_ping,
    "echo": rpc_echo,
    "read_file": rpc_read_file,
    "write_file": rpc_write_file,
    "add": rpc_add,
    "before_tool": syntax_before_tool,
}

# ── JSON-RPC 2.0 message handling (§2) ───────────────────────────────────────


def _sanitize_id(value: Any) -> int | str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    return value


def _error_body(req_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": error}


def _dispatch_one(msg: Any) -> dict[str, Any] | None:
    """Handle one request/notification; None means "write nothing back"."""
    if not isinstance(msg, dict):
        return _error_body(None, INVALID_REQUEST, "Invalid Request")
    if msg.get("jsonrpc") != "2.0":
        return _error_body(
            _sanitize_id(msg.get("id")), INVALID_REQUEST, "Invalid Request"
        )
    req_id = _sanitize_id(msg.get("id"))
    is_notification = "id" not in msg
    method = msg.get("method")
    if not isinstance(method, str):
        if is_notification:
            return None
        return _error_body(req_id, INVALID_REQUEST, "Invalid Request")
    handler = METHODS.get(method)
    if handler is None:
        if is_notification:
            return None
        return _error_body(req_id, METHOD_NOT_FOUND, f"Method not found: {method}")
    try:
        result = handler(msg.get("params"))
    except RpcError as exc:
        if is_notification:
            return None
        return _error_body(req_id, exc.code, exc.message, exc.data)
    except Exception as exc:  # handler crash → -32603 with repr(exc)
        if is_notification:
            return None
        return _error_body(req_id, INTERNAL_ERROR, "Internal error", repr(exc))
    if is_notification:
        return None
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def handle_message(raw: bytes) -> Any:
    """Parse + dispatch; returns the response payload (dict/list) or None."""
    try:
        msg = json.loads(raw)
    except Exception as exc:
        return _error_body(None, PARSE_ERROR, "Parse error", str(exc))
    if isinstance(msg, list):
        if not msg:
            return _error_body(None, INVALID_REQUEST, "Invalid Request")
        responses = [r for r in (_dispatch_one(item) for item in msg) if r is not None]
        return responses or None
    return _dispatch_one(msg)


# ── transports (§1) ──────────────────────────────────────────────────────────


async def _tcp_client_connected(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        while True:
            line = await reader.readuntil(b"\n")
            response = handle_message(line.rstrip(b"\n"))
            if response is not None:
                writer.write(json.dumps(response).encode("utf-8") + b"\n")
                await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass
    except asyncio.LimitOverrunError:
        writer.close()
    except Exception:
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


def _handle_http_request(data: bytes) -> tuple[int, bytes]:
    """Minimal HTTP/1.1 handling for ``POST /rpc`` (single complete request)."""
    try:
        head, _, body = data.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        request_line = lines[0].decode("latin-1").split()
        method, target = request_line[0], request_line[1]
        if method != "POST" or target != "/rpc":
            return 404, b'{"error":"not found"}'
        response = handle_message(body)
        if response is None:  # notification → 204 No Content
            return 204, b""
        payload = json.dumps(response).encode("utf-8")
        return 200, payload
    except Exception:
        return 400, b'{"error":"bad request"}'


class _HttpProtocol(asyncio.Protocol):
    """Feed whole request bytes into the coroutine-style handler above."""

    def __init__(self) -> None:
        self._buffer = b""
        self._transport: asyncio.Transport | None = None
        self._expected: int | None = None

    def connection_made(self, transport: asyncio.Transport) -> None:
        self._transport = transport

    def data_received(self, data: bytes) -> None:
        self._buffer += data
        if self._expected is None:
            marker = self._buffer.find(b"\r\n\r\n")
            if marker < 0:
                return
            head = self._buffer[:marker].decode("latin-1", "replace")
            length = 0
            for line in head.split("\r\n")[1:]:
                key, _, value = line.partition(":")
                if key.lower() == "content-length":
                    with contextlib.suppress(ValueError):
                        length = int(value.strip())
            self._expected = marker + 4 + length
        if len(self._buffer) < self._expected:
            return
        request, self._buffer = self._buffer[: self._expected], self._buffer[self._expected :]
        self._expected = None
        status, payload = _handle_http_request(request)
        assert self._transport is not None
        reason = {200: "OK", 204: "No Content", 400: "Bad Request", 404: "Not Found"}.get(
            status, "OK"
        )
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("latin-1")
        self._transport.write(head + payload)
        self._transport.close()


# ── lifecycle: config handshake + stop-file watcher (§7/§8.2) ─────────────────


def _atomic_write_json(data: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(json.dumps(data, indent=2).encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


async def _run(args: argparse.Namespace) -> None:
    global _SERVER_ROOT
    if args.root:
        _SERVER_ROOT = Path(args.root)

    host, port = args.host, args.port
    if args.network == "tcp":
        server = await asyncio.start_server(
            _tcp_client_connected, host, port, limit=TCP_STREAM_LIMIT
        )
    else:
        loop = asyncio.get_running_loop()
        server = await loop.create_server(_HttpProtocol, host, port)
    sock = server.sockets[0] if server.sockets else None
    bound_host, bound_port = (sock.getsockname()[:2] if sock else (host, port))

    if args.config_out:
        _atomic_write_json(
            {"network": args.network, "address": str(bound_host), "port": int(bound_port)},
            Path(args.config_out),
        )
    print(
        f"kimix_lsp listening on {args.network}://{bound_host}:{bound_port}",
        file=sys.stderr,
        flush=True,
    )

    stopped = asyncio.Event()

    async def _watch() -> None:
        if args.stop_file:
            stop_file = Path(args.stop_file)
            while not stopped.is_set():
                if stop_file.exists():
                    break
                await asyncio.sleep(0.1)
        else:
            await stopped.wait()
        stopped.set()
        server.close()

    watcher = asyncio.create_task(_watch())
    try:
        await stopped.wait()
        with contextlib.suppress(Exception):
            await server.wait_closed()
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        server.close()
        with contextlib.suppress(Exception):
            await server.wait_closed()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="kimix_lsp",
        description="Kimix LSP-bridge session server: syntax check on write/edit.",
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
        help="Confine read_file/write_file paths to this directory.",
    )
    args = parser.parse_args(argv)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run(args))


if __name__ == "__main__":
    main()
