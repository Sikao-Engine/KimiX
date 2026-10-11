"""E2E tests for ``tests/dummy_rpc_server.py`` — the documented example server.

Spawns the standalone stdlib-only dummy server as a real sub-process on both
transports (TCP + HTTP), discovers it through the atomic ``--config-out``
handshake and asserts the example return value of **every builtin method**
(``ping``, ``echo``, ``read_file``, ``write_file``, ``add``, ``before_tool``)
over the wire, exactly as documented in ``docs/session_server_rpc.md`` §5.

Also covers:

* error paths (``-32601`` unknown method, ``-32602`` invalid params,
  ``-32700`` parse error) with real bytes on the socket;
* notification silence (a request without ``id`` is never answered);
* ``--root`` confinement for ``read_file``/``write_file``;
* the graceful ``--stop-file`` exit;
* the full SDK lifecycle loop: the SDK manager resolves a free port and
  substitutes ``<address>``/``<port>``/``<session_id>`` into the child's
  command line (— the legacy config-file handshake is only exercised
  directly above, via ``--config-out``, not by the SDK entry) → the server
  lands in the live registry → ``JsonRpcClient`` (via ``kimi_cli.session_server``)
  calls it → ``stop`` terminates the child.

The same dummy server script backs the connect-bridge tool e2e test
(``kimi-cli/tests/tools/test_session_bridge_e2e.py``).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from kimi_cli.session_server import JsonRpcClient, JsonRpcError

DUMMY_SERVER = Path(__file__).parent / "dummy_rpc_server.py"

CONFIG_TIMEOUT = 20.0  # child bind + config-out handshake
CALL_TIMEOUT = 10.0  # per RPC call
NETS: tuple[str, ...] = ("tcp", "http")


# ── spawn / discovery / cleanup ──────────────────────────────────────────────

async def _spawn_server(
    net: str, tmp_path: Path, *, root: Path | None = None
) -> tuple[asyncio.subprocess.Process, dict[str, Any], Path]:
    """Start the dummy server child; return (proc, connection info, stop file)."""
    cfg_path = tmp_path / f"network_{net}.json"
    stop_file = tmp_path / f"stop_{net}.flag"
    argv = [
        sys.executable,
        str(DUMMY_SERVER),
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
    ]
    if root is not None:
        argv += ["--root", str(root)]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    info: dict[str, Any] | None = None
    deadline = asyncio.get_running_loop().time() + CONFIG_TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        if cfg_path.exists():
            with contextlib.suppress(json.JSONDecodeError):
                candidate = json.loads(cfg_path.read_bytes())
                if isinstance(candidate, dict):
                    info = candidate
                    break
        if proc.returncode is not None:
            raise RuntimeError(f"dummy server exited early (rc={proc.returncode})")
        await asyncio.sleep(0.05)
    if info is None:
        await _terminate(proc, stop_file)
        raise RuntimeError(f"timed out waiting for {cfg_path}")
    assert info["network"] == net
    assert info["address"] == "127.0.0.1"
    assert isinstance(info["port"], int) and info["port"] > 0
    return proc, info, stop_file


async def _terminate(proc: asyncio.subprocess.Process, stop_file: Path) -> None:
    """Stop-file first, then terminate(), then kill() — never by image name."""
    if proc.returncode is not None:
        return
    with contextlib.suppress(OSError):
        stop_file.touch()
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), 10)
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), 5)
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
        await asyncio.wait_for(proc.wait(), 5)


async def _connect(net: str, info: dict[str, Any]) -> JsonRpcClient:
    if net == "tcp":
        return await JsonRpcClient.connect_tcp(str(info["address"]), int(info["port"]))
    return await JsonRpcClient.connect_http(str(info["address"]), int(info["port"]))


async def _wait_until_listening(address: str, port: int, timeout: float = 20.0) -> None:
    """Poll-connect until the freshly spawned child has bound and is listening.

    The SDK manager marks a server ``ready`` at spawn time; the port is
    resolved *before* the child runs, so the child's bind may lag slightly
    behind ``ready`` (it replaces the old config-file handshake barrier).
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(address, port), 2.0
            )
        except (OSError, asyncio.TimeoutError):
            await asyncio.sleep(0.05)
            continue
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return
    raise AssertionError(f"timed out waiting for {address}:{port} to accept connections")


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(params=NETS)
def net(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest_asyncio.fixture
async def dummy_server(net: str, tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    proc, info, stop_file = await _spawn_server(net, tmp_path)
    try:
        yield info
    finally:
        await _terminate(proc, stop_file)


@pytest_asyncio.fixture
async def client(net: str, dummy_server: dict[str, Any]) -> AsyncIterator[JsonRpcClient]:
    c = await _connect(net, dummy_server)
    try:
        yield c
    finally:
        await c.close()


# ── builtin methods: example request → example return value (docs §5) ────────

async def test_ping_example(client: JsonRpcClient) -> None:
    assert await client.call("ping", timeout=CALL_TIMEOUT) == "pong"


async def test_echo_example(client: JsonRpcClient) -> None:
    result = await client.call("echo", {"text": "hi"}, timeout=CALL_TIMEOUT)
    assert result == {"text": "hi", "length": 2}


async def test_add_example(client: JsonRpcClient) -> None:
    result = await client.call("add", {"a": 1.5, "b": 2.25}, timeout=CALL_TIMEOUT)
    assert result == {"sum": 3.75}


async def test_write_then_read_file_example(client: JsonRpcClient, tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    written = await client.call(
        "write_file", {"path": str(target), "content": "hello root"}, timeout=CALL_TIMEOUT
    )
    assert written == {"path": str(target), "bytes_written": 10}
    assert target.read_text(encoding="utf-8") == "hello root"

    read = await client.call(
        "read_file", {"path": str(target), "offset": 0, "limit": 10}, timeout=CALL_TIMEOUT
    )
    assert read == {"path": str(target), "lines": ["hello root"], "total": 1}

    appended = await client.call(
        "write_file",
        {"path": str(target), "content": "\nmore", "append": True},
        timeout=CALL_TIMEOUT,
    )
    assert appended["bytes_written"] == 5
    read2 = await client.call("read_file", {"path": str(target)}, timeout=CALL_TIMEOUT)
    assert read2["lines"] == ["hello root", "more"]
    assert read2["total"] == 2


async def test_before_tool_full_params_example(client: JsonRpcClient) -> None:
    result = await client.call(
        "before_tool",
        {
            "tool": "read",
            "action": "read",
            "path": "src/main.py",
            "paths": ["src/main.py", "src/util.py"],
            "cwd": "C:\\work",
        },
        timeout=CALL_TIMEOUT,
    )
    assert result == {
        "message": (
            "ack before_tool: tool=read action=read path=src/main.py "
            "paths=['src/main.py', 'src/util.py']"
        ),
        "tool": "read",
        "action": "read",
        "handled": True,
    }


async def test_before_tool_minimal_defaults(client: JsonRpcClient) -> None:
    result = await client.call("before_tool", {"tool": "write"}, timeout=CALL_TIMEOUT)
    assert result == {
        "message": "ack before_tool: tool=write action=pre",
        "tool": "write",
        "action": "pre",
        "handled": True,
    }


async def test_before_tool_missing_tool_is_invalid_params(client: JsonRpcClient) -> None:
    with pytest.raises(JsonRpcError) as exc_info:
        await client.call("before_tool", {"path": "x.py"}, timeout=CALL_TIMEOUT)
    assert exc_info.value.code == -32602


# ── error paths and notifications ────────────────────────────────────────────

async def test_unknown_method_is_method_not_found(client: JsonRpcClient) -> None:
    with pytest.raises(JsonRpcError) as exc_info:
        await client.call("no_such_method", {}, timeout=CALL_TIMEOUT)
    assert exc_info.value.code == -32601


async def test_invalid_params_are_rejected(client: JsonRpcClient) -> None:
    with pytest.raises(JsonRpcError) as exc_info:
        await client.call("echo", {"text": 123}, timeout=CALL_TIMEOUT)
    assert exc_info.value.code == -32602


async def test_parse_error_and_notification_silence_tcp(net: str, dummy_server: dict[str, Any]) -> None:
    """Raw-socket checks: garbage bytes → -32700; a notification is never
    answered, proven by the ping that follows on the same connection."""
    if net != "tcp":
        pytest.skip("raw newline framing is TCP-only")
    reader, writer = await asyncio.open_connection(
        str(dummy_server["address"]), int(dummy_server["port"])
    )
    try:
        writer.write(b"{not json\n")
        line = await asyncio.wait_for(reader.readline(), CALL_TIMEOUT)
        response = json.loads(line)
        assert response["jsonrpc"] == "2.0"
        assert response["id"] is None
        assert response["error"]["code"] == -32700

        writer.write(
            json.dumps({"jsonrpc": "2.0", "method": "echo", "params": {"text": "nf"}}).encode()
            + b"\n"
        )
        writer.write(json.dumps({"jsonrpc": "2.0", "method": "ping", "id": 99}).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), CALL_TIMEOUT)
        response = json.loads(line)
        # The very next line is the ping answer — the notification wrote nothing.
        assert response == {"jsonrpc": "2.0", "id": 99, "result": "pong"}
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


# ── --root confinement (§6) ──────────────────────────────────────────────────

async def test_root_confines_file_methods(tmp_path: Path, net: str) -> None:
    root = tmp_path / "root"
    root.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    proc, info, stop_file = await _spawn_server(net, work, root=root)
    try:
        c = await _connect(net, info)
        try:
            inside = await c.call(
                "write_file",
                {"path": "inside.txt", "content": "ok"},
                timeout=CALL_TIMEOUT,
            )
            assert Path(inside["path"]).name == "inside.txt"
            assert (root / "inside.txt").read_text(encoding="utf-8") == "ok"

            with pytest.raises(JsonRpcError) as exc_info:
                await c.call(
                    "write_file",
                    {"path": "../escape.txt", "content": "no"},
                    timeout=CALL_TIMEOUT,
                )
            assert exc_info.value.code == -32602
            assert not (tmp_path / "escape.txt").exists()

            with pytest.raises(JsonRpcError) as exc_info:
                await c.call("read_file", {"path": "../escape.txt"}, timeout=CALL_TIMEOUT)
            assert exc_info.value.code == -32602
        finally:
            await c.close()
    finally:
        await _terminate(proc, stop_file)


# ── graceful stop-file exit (§7) ─────────────────────────────────────────────

async def test_stop_file_triggers_clean_exit(net: str, tmp_path: Path) -> None:
    proc, _info, stop_file = await _spawn_server(net, tmp_path)
    assert proc.returncode is None
    stop_file.touch()
    try:
        rc = await asyncio.wait_for(proc.wait(), 10)
    finally:
        await _terminate(proc, stop_file)
    assert rc == 0


# ── SDK lifecycle: client-resolved port → live registry → call → stop ─────

async def test_sdk_full_loop_with_dummy_server(tmp_path: Path) -> None:
    """The dummy server works as a placeholder-mode entry of the SDK server
    process manager, exactly like the real ``python -m kimi_cli.session_server``:
    the client resolves the free port and substitutes ``<address>``/``<port>``
    into the child's argv (no ``config`` handshake, no ``--config-out``).
    """
    import kimi_agent_sdk._server_process as server_process

    entry: dict[str, Any] = {
        "cmd": sys.executable,
        "args": [
            str(DUMMY_SERVER),
            "--network",
            "tcp",
            "--host",
            "<address>",
            "--port",
            "<port>",
        ],
        "cwd": ".",
    }
    servers = await server_process.start_server_processes(
        tmp_path, [entry], session_id="e2e-session"
    )
    client = None
    try:
        assert len(servers) == 1
        server = servers[0]
        assert server.ready is True
        assert server.error is None
        assert server.network == "tcp"
        assert server.address == "127.0.0.1"
        assert server.port > 0

        # registered in the module-level live registry the bridge consults
        live = server_process.get_live_server_processes()
        assert server in live

        # the child binds the substituted port a moment after spawn
        await _wait_until_listening(server.address, server.port)
        client = await JsonRpcClient.connect_tcp(server.address, server.port)
        assert await client.call("ping", timeout=CALL_TIMEOUT) == "pong"
        assert await client.call("echo", {"text": "sdk loop"}, timeout=CALL_TIMEOUT) == {
            "text": "sdk loop",
            "length": 8,
        }
        assert await client.call("add", {"a": 1, "b": 2}, timeout=CALL_TIMEOUT) == {
            "sum": 3.0
        }
    finally:
        if client is not None:
            await client.close()
        await server_process.stop_server_processes(servers)

    assert server.process is not None
    assert server.process.returncode is not None  # child terminated by stop
    assert server not in server_process.get_live_server_processes()
