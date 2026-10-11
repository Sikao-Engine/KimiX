#!/usr/bin/env python
"""Dummy session-server example — a standalone JSON-RPC 2.0 server.

This script is the *reference example* for
``docs/session_server_rpc.md`` ("Writing your own server"): it implements
every builtin method of ``kimi_cli.session_server.DEFAULT_REGISTRY``
(``ping``, ``echo``, ``read_file``, ``write_file``, ``add``, ``before_tool``)
with the exact wire semantics and example return values documented there,
but **without importing kimi_cli at all** — pure stdlib (asyncio + json).
It proves that any process speaking the protocol can act as a session
server, and it is used by the real e2e tests:

* ``tests/test_dummy_rpc_server_e2e.py`` (root suite)
* ``kimi-cli/tests/tools/test_session_bridge_e2e.py`` (connect bridge,
  real sub-process behind the SDK live registry)

Usage (identical CLI contract to ``python -m kimi_cli.session_server``)::

    python tests/dummy_rpc_server.py --network tcp --host 127.0.0.1 --port 0 \
        --config-out .kimix/network.json --stop-file stop.flag --root .

``--config-out`` receives the atomic discovery handshake
``{"network": ..., "address": ..., "port": ...}`` (tmp file + ``os.replace``)
consumed by ``kimi_agent_sdk._server_process`` dynamic mode.  ``--root``
confines ``read_file``/``write_file`` paths exactly like the real server.

Example request/response pairs (see docs/session_server_rpc.md §5):

    --> {"jsonrpc":"2.0","method":"ping","id":1}
    <-- {"jsonrpc":"2.0","id":1,"result":"pong"}

    --> {"jsonrpc":"2.0","method":"echo","params":{"text":"hi"},"id":2}
    <-- {"jsonrpc":"2.0","id":2,"result":{"text":"hi","length":2}}

    --> {"jsonrpc":"2.0","method":"read_file","params":{"path":"notes.txt"},"id":3}
    <-- {"jsonrpc":"2.0","id":3,"result":{"path":".../notes.txt","lines":["hello"],"total":1}}

    --> {"jsonrpc":"2.0","method":"write_file","params":{"path":"in.txt","content":"hi"},"id":4}
    <-- {"jsonrpc":"2.0","id":4,"result":{"path":".../in.txt","bytes_written":2}}

    --> {"jsonrpc":"2.0","method":"add","params":{"a":1.5,"b":2.25},"id":5}
    <-- {"jsonrpc":"2.0","id":5,"result":{"sum":3.75}}

    --> {"jsonrpc":"2.0","method":"before_tool","params":{"tool":"read","action":"read","path":"src/main.py"},"id":6}
    <-- {"jsonrpc":"2.0","id":6,"result":{"message":"ack before_tool: tool=read action=read path=src/main.py","tool":"read","action":"read","handled":true}}
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

# JSON-RPC 2.0 pre-defined error codes (docs/session_server_rpc.md §3).
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Same framing limit as the real server: 16 MiB per TCP line.
TCP_STREAM_LIMIT = 16 * 1024 * 1024

_SERVER_ROOT: Path | None = None


class RpcError(Exception):
    """Handler-level error carrying a JSON-RPC error code."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


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


# ── builtin methods (§5) ──────────────────────────────────────────────────────

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


def rpc_add(params: Any) -> dict[str, float]:
    a = _require(params, "a", (int, float))
    b = _require(params, "b", (int, float))
    return {"sum": float(a) + float(b)}


def rpc_before_tool(params: Any) -> dict[str, Any]:
    """Echo implementation of the connect-bridge hook (§5.6)."""
    tool = _require(params, "tool", str)
    action = _optional(params, "action", str, "pre")
    message = f"ack before_tool: tool={tool} action={action}"
    if isinstance(params, dict):
        if params.get("path") is not None:
            message += f" path={params['path']}"
        if params.get("paths") is not None:
            message += f" paths={params['paths']}"
    return {"message": message, "tool": tool, "action": action, "handled": True}


METHODS: dict[str, Any] = {
    "ping": rpc_ping,
    "echo": rpc_echo,
    "read_file": rpc_read_file,
    "write_file": rpc_write_file,
    "add": rpc_add,
    "before_tool": rpc_before_tool,
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
        sock = server.sockets[0] if server.sockets else None
        bound_host, bound_port = (sock.getsockname()[:2] if sock else (host, port))
        close_server = server.close
        wait_closed = server.wait_closed
    else:
        loop = asyncio.get_running_loop()
        server = await loop.create_server(_HttpProtocol, host, port)
        sock = server.sockets[0] if server.sockets else None
        bound_host, bound_port = (sock.getsockname()[:2] if sock else (host, port))
        close_server = server.close
        wait_closed = server.wait_closed

    if args.config_out:
        _atomic_write_json(
            {"network": args.network, "address": str(bound_host), "port": int(bound_port)},
            Path(args.config_out),
        )
    print(
        f"dummy_rpc_server listening on {args.network}://{bound_host}:{bound_port}",
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
        close_server()

    watcher = asyncio.create_task(_watch())
    try:
        await stopped.wait()
        with contextlib.suppress(Exception):
            await wait_closed()
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        close_server()
        with contextlib.suppress(Exception):
            await wait_closed()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="dummy_rpc_server",
        description="Standalone example session server (JSON-RPC 2.0, stdlib only).",
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
