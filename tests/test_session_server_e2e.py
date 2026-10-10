"""End-to-end tests for kimi_cli.session_server over real sub-processes.

Every test spawns the server as an independent child process
(``sys.executable -m kimi_cli.session_server``) bound to an ephemeral port,
discovers its address through the atomic ``--config-out`` handshake, and talks
to it with :class:`JsonRpcClient` over the matching transport (TCP or HTTP).

Covers: ping / echo (pydantic params) / add (floats), the -32601 and -32602
error paths, ordered batch results, notification-then-request on a live TCP
connection, a read_file/write_file round-trip, graceful ``--stop-file`` exit,
and one full lifecycle loop through the SDK side
(``kimi_agent_sdk._server_process``: dynamic config mode, ``ready`` resolution,
connect + call, and terminate on stop).

Cleanup never kills by image name: the child is asked to exit via its stop
file first, then ``terminate()``/``wait()`` with a timeout, ``kill()`` only as
a last resort.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import orjson
import pytest
import pytest_asyncio
from kimi_cli.session_server import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    JsonRpcClient,
    JsonRpcError,
)

from kimi_agent_sdk._server_process import (
    start_server_processes,
    stop_server_processes,
)

# Seconds to wait for the child to bind and write its --config-out file.
CONFIG_TIMEOUT = 15.0
# Per-RPC-call timeout used everywhere so a broken server fails fast
# instead of hanging the suite.
CALL_TIMEOUT = 10.0

NETS: tuple[str, ...] = ("tcp", "http")


# ── spawn / discovery / cleanup helpers ─────────────────────────────────────


async def _spawn_server(net: str, tmp_path: Path) -> tuple[asyncio.subprocess.Process, dict[str, Any], Path]:
    """Start the server sub-process and return (proc, connection-info, stop-file)."""
    cfg_path = tmp_path / f"network_{net}.json"
    stop_file = tmp_path / f"stop_{net}.flag"
    assert not cfg_path.exists() and not stop_file.exists()
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
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    info: dict[str, Any] | None = None
    deadline = asyncio.get_running_loop().time() + CONFIG_TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        if cfg_path.exists():
            try:
                # The server writes atomically (tmp + os.replace), so once the
                # file exists its content is final.
                info = orjson.loads(cfg_path.read_bytes())
            except orjson.JSONDecodeError:
                info = None  # defensive: retry until the timeout
            else:
                if not isinstance(info, dict):
                    info = None
                else:
                    break
        if proc.returncode is not None:
            raise RuntimeError(
                f"server sub-process exited early (rc={proc.returncode}) "
                f"before writing {cfg_path}"
            )
        await asyncio.sleep(0.05)
    if info is None:
        await _terminate(proc, stop_file)
        raise RuntimeError(f"timed out after {CONFIG_TIMEOUT}s waiting for {cfg_path}")
    assert info.get("network") == net
    assert info.get("address") == "127.0.0.1"
    assert isinstance(info.get("port"), int) and info["port"] > 0
    return proc, info, stop_file


async def _terminate(proc: asyncio.subprocess.Process, stop_file: Path) -> None:
    """Always-safe cleanup: stop-file first, then terminate(), then kill()."""
    if proc.returncode is not None:
        return
    with contextlib.suppress(OSError):
        stop_file.touch()  # ask the server to exit gracefully
    try:
        await asyncio.wait_for(proc.wait(), 10)
        return
    except asyncio.TimeoutError:
        pass
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), 5)
        return
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            return
        await asyncio.wait_for(proc.wait(), 5)


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(params=NETS)
def net(request: pytest.FixtureRequest) -> str:
    """Transport selector: every server-facing test runs once per transport."""
    return str(request.param)


@pytest_asyncio.fixture
async def rpc_server(net: str, tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    """Spawn one server per test; yield its discovered connection info."""
    proc, info, stop_file = await _spawn_server(net, tmp_path)
    try:
        yield info
    finally:
        await _terminate(proc, stop_file)


@pytest_asyncio.fixture
async def client(net: str, rpc_server: dict[str, Any]) -> AsyncIterator[JsonRpcClient]:
    """A connected JsonRpcClient for the fixture server of the current transport."""
    c = await _connect(net, rpc_server)
    try:
        yield c
    finally:
        await c.close()


async def _connect(net: str, info: dict[str, Any]) -> JsonRpcClient:
    if net == "tcp":
        return await JsonRpcClient.connect_tcp(str(info["address"]), int(info["port"]))
    return await JsonRpcClient.connect_http(str(info["address"]), int(info["port"]))


# ── per-transport behaviour tests ───────────────────────────────────────────


async def test_ping(client: JsonRpcClient) -> None:
    assert await client.call("ping", timeout=CALL_TIMEOUT) == "pong"


async def test_echo_validates_params(client: JsonRpcClient) -> None:
    result = await client.call("echo", {"text": "hello rpc"}, timeout=CALL_TIMEOUT)
    assert result == {"text": "hello rpc", "length": len("hello rpc")}


async def test_add_with_float_params(client: JsonRpcClient) -> None:
    result = await client.call("add", {"a": 0.1, "b": 0.2}, timeout=CALL_TIMEOUT)
    assert result["sum"] == pytest.approx(0.3)


async def test_unknown_method_raises_method_not_found(client: JsonRpcClient) -> None:
    with pytest.raises(JsonRpcError) as excinfo:
        await client.call("no_such_method", {}, timeout=CALL_TIMEOUT)
    assert excinfo.value.code == METHOD_NOT_FOUND == -32601
    assert "no_such_method" in excinfo.value.message


@pytest.mark.parametrize("params", [{}, {"text": 42}, None], ids=["empty", "wrong-type", "absent"])
async def test_invalid_params_raises_invalid_params(client: JsonRpcClient, params: Any) -> None:
    with pytest.raises(JsonRpcError) as excinfo:
        await client.call("echo", params, timeout=CALL_TIMEOUT)
    assert excinfo.value.code == INVALID_PARAMS == -32602


async def test_call_batch_returns_ordered_results(client: JsonRpcClient) -> None:
    results = await client.call_batch(
        [
            ("echo", {"text": "second"}),
            ("ping", None),
            ("add", {"a": 1.5, "b": 2.25}),
        ],
        timeout=CALL_TIMEOUT,
    )
    assert results == [
        {"text": "second", "length": 6},
        "pong",
        {"sum": 3.75},
    ]


async def test_notify_does_not_hang_and_connection_still_works(client: JsonRpcClient) -> None:
    # The server must not write anything back for a notification; the next
    # request on the same connection still gets the right answer (on TCP a
    # spurious notification response would desynchronise the line stream).
    await client.notify("echo", {"text": "fire and forget"})
    assert await client.call("ping", timeout=CALL_TIMEOUT) == "pong"
    assert await client.call("echo", {"text": "x"}, timeout=CALL_TIMEOUT) == {
        "text": "x",
        "length": 1,
    }


async def test_write_read_file_roundtrip(client: JsonRpcClient, tmp_path: Path) -> None:
    target = tmp_path / "roundtrip.txt"
    written = await client.call(
        "write_file", {"path": str(target), "content": "line1\nline2\n"}, timeout=CALL_TIMEOUT
    )
    assert written["bytes_written"] == len(b"line1\nline2\n")
    assert written["path"] == str(target)
    assert target.read_text(encoding="utf-8") == "line1\nline2\n"

    appended = await client.call(
        "write_file", {"path": str(target), "content": "line3\n", "append": True},
        timeout=CALL_TIMEOUT,
    )
    assert appended["bytes_written"] == len(b"line3\n")

    read = await client.call(
        "read_file", {"path": str(target), "offset": 0, "limit": 100}, timeout=CALL_TIMEOUT
    )
    assert read["lines"] == ["line1", "line2", "line3"]
    assert read["total"] == 3

    windowed = await client.call(
        "read_file", {"path": str(target), "offset": 1, "limit": 1}, timeout=CALL_TIMEOUT
    )
    assert windowed["lines"] == ["line2"]


# ── lifecycle: --config-out content + --stop-file graceful exit ────────────


async def test_stop_file_triggers_clean_exit(net: str, tmp_path: Path) -> None:
    proc, _info, stop_file = await _spawn_server(net, tmp_path)
    assert proc.returncode is None
    stop_file.touch()
    try:
        rc = await asyncio.wait_for(proc.wait(), 10)
    finally:
        await _terminate(proc, stop_file)  # idempotent safety net
    assert rc == 0  # server exited on its own via the stop-file watcher


# ── full loop through the SDK side (_server_process dynamic mode) ──────────


async def test_sdk_full_loop_spawn_connect_call_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kimi_agent_sdk._server_process as server_process

    # Fast back-off so the test does not wait on the default (1,2,4,8,16)s.
    monkeypatch.setattr(
        server_process, "CONFIG_WAIT_DELAYS", (0.1, 0.2, 0.4, 0.8, 1.6, 3.2)
    )

    entry: dict[str, Any] = {
        "cmd": sys.executable,
        "args": [
            "-m",
            "kimi_cli.session_server",
            "--network",
            "tcp",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            # relative to the subprocess cwd, which cwd="." resolves to tmp_path
            "--config-out",
            ".kimix/network.json",
        ],
        "cwd": ".",
        "config": ".kimix/network.json",
    }

    servers = await start_server_processes(tmp_path, [entry])
    client = None
    try:
        assert len(servers) == 1
        server = servers[0]
        assert server.ready is True
        assert server.error is None
        assert server.network == "tcp"
        assert server.address == "127.0.0.1"
        assert server.port > 0  # ephemeral port resolved from the config file
        # the handshake file really landed under the work dir
        assert (tmp_path / ".kimix" / "network.json").exists()

        client = await JsonRpcClient.connect_tcp(server.address, server.port)
        assert await client.call("ping", timeout=CALL_TIMEOUT) == "pong"
        echo = await client.call("echo", {"text": "sdk loop"}, timeout=CALL_TIMEOUT)
        assert echo == {"text": "sdk loop", "length": 8}
    finally:
        if client is not None:
            await client.close()
        await stop_server_processes(servers)

    assert server.process is not None
    assert server.process.returncode is not None  # child terminated by stop
