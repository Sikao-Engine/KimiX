"""Raw-wire JSON-RPC 2.0 conformance tests for ``kimi_cli.session_server``.

A plain asyncio TCP stream client (deliberately *not* ``JsonRpcClient``)
sends raw JSON bytes to one module-scoped ``python -m kimi_cli.session_server``
sub-process and asserts the exact wire shape of the responses:

* ``jsonrpc`` and ``id`` are always present (``id`` may be null), even for
  error responses to requests with a null or malformed id (fix 1/2).
* a >64 KiB payload round-trips over TCP (fix 3, ``TCP_STREAM_LIMIT``).
* a batch with one bad-id element yields per-item responses and stays usable.
* ``read_file``/``write_file`` are confined to the ``--root`` directory
  (fix 7): escapes are rejected with INVALID_PARAMS, inside-root works.

Plus a few in-process unit tests for ``JsonRpcResponse.to_dict``,
``handle_message`` poison-batch handling, null results, and the TCP client's
stale-response skipping (fix 6).
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import orjson
import pytest
import pytest_asyncio
from kimi_cli.session_server import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    TCP_STREAM_LIMIT,
    JsonRpcClient,
    JsonRpcErrorObject,
    JsonRpcResponse,
    MethodRegistry,
    handle_message,
)

# Per-call read timeout: generous enough for a slow CI start, small enough
# that a broken server fails the test instead of hanging it.
RPC_TIMEOUT = 15.0
CONFIG_TIMEOUT = 20.0


# ── module-scoped sub-process server (spawned once, shared by raw tests) ───


async def _spawn(net: str, root: Path) -> tuple[asyncio.subprocess.Process, dict[str, Any]]:
    cfg_path = root / "network.json"
    stop_file = root / "stop.flag"
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "kimi_cli.session_server",
        "--network",
        net,
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--config-out",
        str(cfg_path),
        "--stop-file",
        str(stop_file),
        "--root",
        str(root),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    deadline = asyncio.get_running_loop().time() + CONFIG_TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        if cfg_path.exists():
            try:
                info = orjson.loads(cfg_path.read_bytes())
            except orjson.JSONDecodeError:
                info = None
            if isinstance(info, dict) and isinstance(info.get("port"), int):
                return proc, info
        if proc.returncode is not None:
            raise RuntimeError(f"server exited early (rc={proc.returncode})")
        await asyncio.sleep(0.05)
    raise RuntimeError(f"timed out waiting for {cfg_path}")


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def raw_server() -> AsyncIterator[dict[str, Any]]:
    """One TCP sub-process server (with --root) shared by all raw-wire tests."""
    root = Path(tempfile.mkdtemp(prefix="kimi-rawsrv-"))
    stop_file = root / "stop.flag"
    proc: asyncio.subprocess.Process | None = None
    try:
        proc, info = await _spawn("tcp", root)
        info["root"] = root
        yield info
    finally:
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(OSError):
                stop_file.touch()
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), 5)
        shutil.rmtree(root, ignore_errors=True)


async def _raw_rpc(info: dict[str, Any], data: bytes) -> list[bytes]:
    """Send one raw frame and read back the response line (exactly one)."""
    reader, writer = await asyncio.open_connection(
        str(info["address"]), int(info["port"]), limit=TCP_STREAM_LIMIT
    )
    try:
        writer.write(data + b"\n")
        await asyncio.wait_for(writer.drain(), RPC_TIMEOUT)
        line = await asyncio.wait_for(reader.readline(), RPC_TIMEOUT)
        return [line] if line else []
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def _raw_one(info: dict[str, Any], payload: Any) -> dict[str, Any]:
    """Round-trip one request object; return the decoded response object."""
    lines = await _raw_rpc(info, orjson.dumps(payload))
    assert len(lines) == 1, f"expected exactly one response line, got {len(lines)}"
    response = orjson.loads(lines[0])
    assert isinstance(response, dict)
    return response


# ── raw-wire conformance: id / jsonrpc always present ──────────────────────


async def test_raw_error_response_includes_null_id_and_jsonrpc(
    raw_server: dict[str, Any]
) -> None:
    """Error response to an unknown method with ``id: null`` echoes ``id``."""
    response = await _raw_one(
        raw_server, {"jsonrpc": "2.0", "method": "no_such", "id": None}
    )
    assert "id" in response and response["id"] is None
    assert response["jsonrpc"] == "2.0"
    assert "result" not in response
    assert response["error"]["code"] == METHOD_NOT_FOUND


@pytest.mark.parametrize(
    "bad_id",
    [[1, 2], {"a": 1}, 1.5, True],
    ids=["list", "dict", "float", "bool"],
)
async def test_raw_malformed_id_echoes_null_without_crash(
    raw_server: dict[str, Any], bad_id: Any
) -> None:
    """Unvalidated ids must never raise server-side; they echo as null."""
    response = await _raw_one(
        raw_server, {"jsonrpc": "2.0", "method": "ping", "id": bad_id}
    )
    assert "id" in response and response["id"] is None
    assert response["jsonrpc"] == "2.0"
    # the malformed id is either rejected outright or, if the request still
    # dispatched, echoed back as null — never the raw object itself.
    assert "error" in response or response.get("result") == "pong"


async def test_raw_connection_survives_malformed_id(raw_server: dict[str, Any]) -> None:
    """After a bad-id response the very same request format still works."""
    response = await _raw_one(raw_server, {"jsonrpc": "2.0", "method": "ping", "id": 42})
    assert response == {"jsonrpc": "2.0", "id": 42, "result": "pong"}


async def test_raw_batch_with_bad_id_element_returns_per_item(
    raw_server: dict[str, Any]
) -> None:
    """A batch mixing valid/invalid ids returns one list of per-item responses."""
    batch = [
        {"jsonrpc": "2.0", "method": "ping", "id": 1},
        {"jsonrpc": "2.0", "method": "ping", "id": [7]},  # malformed id
        {"jsonrpc": "2.0", "method": "no_such", "id": "x"},
    ]
    lines = await _raw_rpc(raw_server, orjson.dumps(batch))
    assert len(lines) == 1
    responses = orjson.loads(lines[0])
    assert isinstance(responses, list) and len(responses) == 3
    by_marker = {r.get("id"): r for r in responses}
    assert set(by_marker) == {1, None, "x"}
    assert by_marker[1]["result"] == "pong"
    assert by_marker[None]["error"]["code"] == INVALID_REQUEST
    assert by_marker["x"]["error"]["code"] == METHOD_NOT_FOUND


async def test_raw_large_payload_roundtrip_tcp(raw_server: dict[str, Any]) -> None:
    """>64 KiB frame round-trips (proves TCP_STREAM_LIMIT, fix 3)."""
    text = "x" * 100_000  # far above the 64 KiB asyncio default
    response = await _raw_one(
        raw_server, {"jsonrpc": "2.0", "method": "echo", "params": {"text": text}, "id": 7}
    )
    assert response["id"] == 7
    assert response["result"]["length"] == 100_000
    assert response["result"]["text"] == text


async def test_raw_parse_error_has_null_id(raw_server: dict[str, Any]) -> None:
    lines = await _raw_rpc(raw_server, b"{ not json at all")
    assert len(lines) == 1
    response = orjson.loads(lines[0])
    assert response["id"] is None and "id" in response
    assert response["error"]["code"] == PARSE_ERROR


# ── --root path confinement (fix 7) ────────────────────────────────────────


async def test_root_confines_write_and_read(raw_server: dict[str, Any]) -> None:
    root: Path = raw_server["root"]
    outside = Path(tempfile.mkdtemp(prefix="kimi-outside-"))
    try:
        # relative path inside root: allowed, file lands under root
        ok = await _raw_one(
            raw_server,
            {
                "jsonrpc": "2.0",
                "method": "write_file",
                "params": {"path": "inside.txt", "content": "hello root"},
                "id": 1,
            },
        )
        assert "error" not in ok
        assert (root / "inside.txt").read_text(encoding="utf-8") == "hello root"

        # absolute path outside root: rejected before touching the filesystem
        escape_abs = await _raw_one(
            raw_server,
            {
                "jsonrpc": "2.0",
                "method": "write_file",
                "params": {"path": str(outside / "evil.txt"), "content": "x"},
                "id": 2,
            },
        )
        assert escape_abs["error"]["code"] == INVALID_PARAMS
        assert not (outside / "evil.txt").exists()

        # relative escape via ..: rejected
        escape_rel = await _raw_one(
            raw_server,
            {
                "jsonrpc": "2.0",
                "method": "read_file",
                "params": {"path": "../escape.txt"},
                "id": 3,
            },
        )
        assert escape_rel["error"]["code"] == INVALID_PARAMS

        # reading the inside file back works
        read_back = await _raw_one(
            raw_server,
            {"jsonrpc": "2.0", "method": "read_file", "params": {"path": "inside.txt"}, "id": 4},
        )
        assert read_back["result"]["lines"] == ["hello root"]
    finally:
        shutil.rmtree(outside, ignore_errors=True)


# ── JsonRpcClient TCP id matching (fix 6), faked stale-response stream ─────


class _FakeWriter:
    """Minimal StreamWriter stand-in for the send side."""

    def __init__(self) -> None:
        self.sent = bytearray()

    def write(self, data: bytes) -> None:
        self.sent += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


async def test_tcp_client_skips_stale_response_lines() -> None:
    reader = asyncio.StreamReader(limit=TCP_STREAM_LIMIT)
    reader.feed_data(orjson.dumps({"jsonrpc": "2.0", "id": 99, "result": "stale"}) + b"\n")
    reader.feed_data(orjson.dumps({"jsonrpc": "2.0", "id": 1, "result": "fresh"}) + b"\n")
    reader.feed_eof()
    client = JsonRpcClient()
    client._reader = reader
    client._writer = _FakeWriter()  # type: ignore[assignment]
    try:
        assert await client.call("ping", timeout=5) == "fresh"
    finally:
        await client.close()


# ── in-process protocol units: to_dict shape + poison batch + null result ──


def test_response_to_dict_field_presence() -> None:
    success = JsonRpcResponse(id=1, result=None).to_dict()
    assert success == {"jsonrpc": "2.0", "id": 1, "result": None}

    error_no_data = JsonRpcResponse(
        id=None, error=JsonRpcErrorObject(code=METHOD_NOT_FOUND, message="m")
    ).to_dict()
    assert error_no_data == {"jsonrpc": "2.0", "id": None, "error": {"code": -32601, "message": "m"}}
    assert "data" not in error_no_data["error"]
    assert "result" not in error_no_data

    error_with_data = JsonRpcResponse(
        id="x", error=JsonRpcErrorObject(code=INTERNAL_ERROR, message="m", data="detail")
    ).to_dict()
    assert error_with_data["error"]["data"] == "detail"


async def test_handle_message_null_result_keeps_result_key() -> None:
    registry = MethodRegistry()

    @registry.method("none_maker")
    def _none_maker(_params: Any) -> None:
        return None

    out = await handle_message(registry, {"jsonrpc": "2.0", "method": "none_maker", "id": 5})
    assert out is not None
    assert out["jsonrpc"] == "2.0" and out["id"] == 5
    assert "result" in out and out["result"] is None
    assert "error" not in out


async def test_handle_message_batch_poison_item_becomes_internal_error() -> None:
    registry = MethodRegistry()

    @registry.method("ping")
    def _ping(_params: Any) -> str:
        return "pong"

    async def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("kaboom")

    registry.dispatch = explode  # type: ignore[method-assign]
    out = await handle_message(
        registry,
        [
            {"jsonrpc": "2.0", "method": "ping", "id": 1},
            {"jsonrpc": "2.0", "method": "ping", "id": [2]},
            {"jsonrpc": "2.0", "method": "ping"},  # notification: no response
        ],
    )
    assert isinstance(out, list) and len(out) == 2  # poison items must not drop the batch
    by_id = {r["id"]: r for r in out}
    assert set(by_id) == {1, None}
    # id=1: dispatch blew up -> INTERNAL_ERROR (was: the exception escaped
    # gather and killed the whole batch). id=[2]: bad id -> INVALID_REQUEST
    # with null echo; the notification (no id) produced no response.
    assert by_id[1]["error"]["code"] == INTERNAL_ERROR
    assert by_id[None]["error"]["code"] == INVALID_REQUEST
