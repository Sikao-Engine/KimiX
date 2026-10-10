"""Tests for the tool ↔ session-server bridge (``connect: bool`` contract).

Covers kimi_cli/tools/file/session_bridge.py (unit) and the ``connect``
param wiring on read / write / edit / read_image (integration).
Contract: docs/tool_connect_bridge.md.
"""

from __future__ import annotations

import io
import socket
import time
from types import SimpleNamespace

import kimi_agent_sdk._server_process as sdk_registry
import pytest
from PIL import Image

from kaos.path import KaosPath
from kimi_cli.session_server import (
    DEFAULT_REGISTRY,
    BeforeToolParams,
    JsonRpcClient,
    JsonRpcError,
    JsonRpcServer,
    MethodRegistry,
)
from kimi_cli.tools.file.edit import EditFile
from kimi_cli.tools.file.edit.params import EditParams
from kimi_cli.tools.file.read import Params as ReadParams
from kimi_cli.tools.file.read import ReadFile
from kimi_cli.tools.file.read_media import Params as MediaParams
from kimi_cli.tools.file.read_media import ReadMediaFile
from kimi_cli.tools.file.session_bridge import (
    MAX_APPEND_CHARS,
    before_tool,
    merge_message,
)
from kimi_cli.tools.file.write import Params as WriteParams
from kimi_cli.tools.file.write import WriteFile
from kosong.tooling import ToolOk


def _live_entry(
    network: str, address: str, port: int, *, ready: bool = True
) -> SimpleNamespace:
    """ServerProcess-shaped fake matching the registry contract (§4)."""
    return SimpleNamespace(
        ready=ready,
        process=SimpleNamespace(returncode=None),
        network=network,
        address=address,
        port=port,
    )


# ---------------------------------------------------------------------------
# merge_message
# ---------------------------------------------------------------------------


def test_merge_message_none_info_is_identity() -> None:
    assert merge_message("A", None) == "A"
    assert merge_message("A", "") == "A"


def test_merge_message_empty_existing_gets_marker_only() -> None:
    assert merge_message("", "X") == "[server] X"


def test_merge_message_appends_after_blank_line() -> None:
    assert merge_message("A", "X") == "A\n\n[server] X"


# ---------------------------------------------------------------------------
# before_tool: registry / import failure isolation
# ---------------------------------------------------------------------------


async def test_before_tool_empty_registry_returns_none(monkeypatch) -> None:
    sdk_registry._clear_live_registry_for_tests()
    monkeypatch.setattr(sdk_registry, "get_live_server_processes", lambda: [])
    assert await before_tool("read", "read", path="a.txt") is None


async def test_before_tool_import_failure_is_swallowed(monkeypatch) -> None:
    def _boom() -> list:
        raise ImportError("simulated import-cycle failure")

    monkeypatch.setattr(sdk_registry, "get_live_server_processes", _boom)
    # The lazy `from ... import get_live_server_processes` succeeds (we only
    # replaced the attribute), so this exercises the registry-call guard;
    # a failing attribute *lookup* (delattr) exercises the import guard.
    assert await before_tool("read", "read", path="a.txt") is None
    monkeypatch.delattr(sdk_registry, "get_live_server_processes")
    assert await before_tool("write", "write", path="a.txt") is None


async def test_before_tool_skips_non_qualifying_entries(monkeypatch) -> None:
    entries = [
        _live_entry("tcp", "127.0.0.1", 1, ready=False),
        SimpleNamespace(ready=True, process=None, network="tcp", address="h", port=1),
        SimpleNamespace(
            ready=True,
            process=SimpleNamespace(returncode=0),  # exited
            network="tcp",
            address="127.0.0.1",
            port=1,
        ),
        _live_entry("unix", "\\\\.\\pipe\\x", 0),  # transport not supported
        object(),  # malformed entry must be skipped, not crash
    ]
    monkeypatch.setattr(sdk_registry, "get_live_server_processes", lambda: entries)
    t0 = time.monotonic()
    assert await before_tool("read", "read", path="a.txt") is None
    assert time.monotonic() - t0 < 1.0  # fast path, no socket touched


# ---------------------------------------------------------------------------
# before_tool against real in-process JsonRpcServers
# ---------------------------------------------------------------------------


@pytest.fixture
async def tcp_server():
    server = JsonRpcServer(DEFAULT_REGISTRY, host="127.0.0.1", port=0)
    host, port = await server.start_tcp()
    yield server, host, port
    await server.stop()


@pytest.fixture
async def http_server():
    server = JsonRpcServer(DEFAULT_REGISTRY, host="127.0.0.1", port=0)
    host, port = await server.start_http()
    yield server, host, port
    await server.stop()


async def test_before_tool_tcp_roundtrip(tcp_server, monkeypatch) -> None:
    _server, host, port = tcp_server
    monkeypatch.setattr(
        sdk_registry,
        "get_live_server_processes",
        lambda: [_live_entry("tcp", host, port)],
    )
    out = await before_tool("read", "read", path="src/main.py")
    assert out is not None
    assert "ack before_tool" in out
    assert "tool=read" in out
    assert "action=read" in out
    assert "path=src/main.py" in out


async def test_before_tool_http_roundtrip(http_server, monkeypatch) -> None:
    _server, host, port = http_server
    monkeypatch.setattr(
        sdk_registry,
        "get_live_server_processes",
        lambda: [_live_entry("http", host, port)],
    )
    out = await before_tool("read", "read", path="src/main.py")
    assert out is not None
    assert "ack before_tool" in out


async def test_before_tool_multi_server_joins_messages(tcp_server, monkeypatch) -> None:
    _server, host, port = tcp_server
    monkeypatch.setattr(
        sdk_registry,
        "get_live_server_processes",
        lambda: [
            _live_entry("tcp", host, port),
            _live_entry("tcp", host, port),
        ],
    )
    out = await before_tool("read", "read", path="a.py")
    assert out is not None
    assert out.count("ack before_tool") == 2
    assert "\n" in out


async def test_before_tool_payload_shape_reaches_server(tcp_server, monkeypatch) -> None:
    """paths (multi-target) forwarded; defaults are the five documented keys."""
    _server, host, port = tcp_server
    captured: list[dict] = []

    registry = MethodRegistry()

    @registry.method("before_tool", params_model=BeforeToolParams)
    def _capture(params: BeforeToolParams) -> dict:
        captured.append(params.model_dump())
        return {"message": "ok"}

    server = JsonRpcServer(registry, host="127.0.0.1", port=0)
    try:
        rhost, rport = await server.start_tcp()
        monkeypatch.setattr(
            sdk_registry,
            "get_live_server_processes",
            lambda: [_live_entry("tcp", rhost, rport)],
        )
        out = await before_tool(
            "read", "read", path="a.py", paths=["a.py", "b.py"], cwd="C:/work"
        )
        assert out == "ok"
        assert captured == [
            {
                "tool": "read",
                "action": "read",
                "path": "a.py",
                "paths": ["a.py", "b.py"],
                "cwd": "C:/work",
            }
        ]
    finally:
        await server.stop()


async def test_before_tool_caps_appended_text(tcp_server, monkeypatch) -> None:
    _server, host, port = tcp_server

    registry = MethodRegistry()

    @registry.method("before_tool", params_model=BeforeToolParams)
    def _long(_params: BeforeToolParams) -> dict:
        return {"message": "x" * (MAX_APPEND_CHARS + 500)}

    server = JsonRpcServer(registry, host="127.0.0.1", port=0)
    try:
        rhost, rport = await server.start_tcp()
        monkeypatch.setattr(
            sdk_registry,
            "get_live_server_processes",
            lambda: [_live_entry("tcp", rhost, rport)],
        )
        out = await before_tool("read", "read", path="a.py")
        assert out is not None
        assert len(out) == MAX_APPEND_CHARS
    finally:
        await server.stop()


async def test_before_tool_empty_message_means_no_info(tcp_server, monkeypatch) -> None:
    _server, host, port = tcp_server

    registry = MethodRegistry()

    @registry.method("before_tool", params_model=BeforeToolParams)
    def _empty(_params: BeforeToolParams) -> dict:
        return {"message": "   "}

    server = JsonRpcServer(registry, host="127.0.0.1", port=0)
    try:
        rhost, rport = await server.start_tcp()
        monkeypatch.setattr(
            sdk_registry,
            "get_live_server_processes",
            lambda: [_live_entry("tcp", rhost, rport)],
        )
        assert await before_tool("read", "read", path="a.py") is None
    finally:
        await server.stop()


async def test_before_tool_connection_refused_is_swallowed(monkeypatch) -> None:
    # Bind and close to find a port nothing listens on.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = int(probe.getsockname()[1])
    probe.close()
    monkeypatch.setattr(
        sdk_registry,
        "get_live_server_processes",
        lambda: [_live_entry("tcp", "127.0.0.1", dead_port)],
    )
    t0 = time.monotonic()
    assert await before_tool("read", "read", path="a.py", timeout=1.0) is None
    assert time.monotonic() - t0 < 3.0  # fast refusal, no long hang


async def test_before_tool_jsonrpc_error_is_swallowed(tcp_server, monkeypatch) -> None:
    """Server-side validation failure (-32602, missing required `tool`)."""
    _server, host, port = tcp_server
    monkeypatch.setattr(
        sdk_registry,
        "get_live_server_processes",
        lambda: [_live_entry("tcp", host, port)],
    )
    client = await JsonRpcClient.connect_tcp(host, port)
    try:
        with pytest.raises(JsonRpcError):
            await client.call("before_tool", {"action": "read"}, timeout=2.0)
    finally:
        await client.close()
    # The bridge path for the same failure: broken handler -> None.
    registry = MethodRegistry()

    @registry.method("before_tool")
    def _crash(_params) -> dict:
        raise RuntimeError("handler crash")

    server = JsonRpcServer(registry, host="127.0.0.1", port=0)
    try:
        rhost, rport = await server.start_tcp()
        monkeypatch.setattr(
            sdk_registry,
            "get_live_server_processes",
            lambda: [_live_entry("tcp", rhost, rport)],
        )
        assert await before_tool("read", "read", path="a.py", timeout=2.0) is None
    finally:
        await server.stop()


# ---------------------------------------------------------------------------
# Params schema: every tool carries connect with default True
# ---------------------------------------------------------------------------


def test_read_params_connect_default() -> None:
    field = ReadParams.model_fields["connect"]
    assert field.default is True
    assert ReadParams(file_path="a.txt").connect is True


def test_write_params_connect_default() -> None:
    field = WriteParams.model_fields["connect"]
    assert field.default is True
    assert WriteParams(file_path="a.txt", content="x").connect is True


def test_edit_params_connect_default() -> None:
    field = EditParams.model_fields["connect"]
    assert field.default is True
    assert EditParams(path="a.txt", old_string="o", new_string="n").connect is True


def test_read_image_params_connect_default() -> None:
    field = MediaParams.model_fields["connect"]
    assert field.default is True
    assert MediaParams(file_path="a.png").connect is True


# ---------------------------------------------------------------------------
# Tool integration: patched module-level before_tool is called and merged
# ---------------------------------------------------------------------------


def _recording_stub(response: str | None = "LSP: 2 diagnostics"):
    calls: list[tuple] = []

    async def _stub(tool: str, action: str, **kw) -> str | None:
        calls.append((tool, action, kw))
        return response

    return _stub, calls


async def test_read_tool_integration(
    read_file_tool: ReadFile, temp_work_dir: KaosPath, monkeypatch
) -> None:
    stub, calls = _recording_stub()
    monkeypatch.setattr("kimi_cli.tools.file.read.before_tool", stub)
    file = temp_work_dir / "bridge.txt"
    await file.write_text("hello\nworld\n")

    result = await read_file_tool(ReadParams(path=str(file)))
    assert not result.is_error
    assert "[server] LSP: 2 diagnostics" in result.message
    tool, action, kw = calls[0]
    assert (tool, action) == ("read", "read")
    assert kw["path"] == str(file)
    assert kw["paths"] is None
    assert kw["cwd"] is not None

    # multiple targets: paths carries the full list, path the first
    calls.clear()
    result2 = await read_file_tool(ReadParams(file_path=[str(file), str(file)]))
    assert not result2.is_error
    assert calls[0][2]["paths"] == [str(file), str(file)]
    assert calls[0][2]["path"] == str(file)

    # opt-out: no RPC at all
    calls.clear()
    result3 = await read_file_tool(ReadParams(path=str(file), connect=False))
    assert calls == []
    assert "[server]" not in result3.message


async def test_write_tool_integration(
    write_file_tool: WriteFile, temp_work_dir: KaosPath, monkeypatch
) -> None:
    stub, calls = _recording_stub()
    monkeypatch.setattr("kimi_cli.tools.file.write.before_tool", stub)
    file = temp_work_dir / "bridge_out.txt"

    result = await write_file_tool(WriteParams(path=str(file), content="data"))
    assert not result.is_error
    assert "[server] LSP: 2 diagnostics" in result.message
    assert calls[0][0] == "write"
    assert calls[0][1] == "write"
    assert calls[0][2]["path"] == str(file)
    assert calls[0][2]["paths"] is None

    calls.clear()
    result2 = await write_file_tool(
        WriteParams(path=str(file), content="data2", connect=False)
    )
    assert calls == []
    assert "[server]" not in result2.message


async def test_edit_tool_integration(
    edit_file_tool: EditFile, temp_work_dir: KaosPath, monkeypatch
) -> None:
    stub, calls = _recording_stub()
    monkeypatch.setattr("kimi_cli.tools.file.edit.before_tool", stub)
    file = temp_work_dir / "bridge_edit.txt"
    await file.write_text("alpha\n")

    result = await edit_file_tool(
        EditParams(path=str(file), old_string="alpha", new_string="omega")
    )
    assert not result.is_error
    assert "[server] LSP: 2 diagnostics" in result.message
    assert calls[0][0] == "edit"
    assert calls[0][1] == "edit"
    assert calls[0][2]["path"] == str(file)

    calls.clear()
    result2 = await edit_file_tool(
        EditParams(
            path=str(file), old_string="omega", new_string="beta", connect=False
        )
    )
    assert calls == []
    assert "[server]" not in result2.message


def _make_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 6), (51, 102, 204)).save(buf, format="PNG")
    return buf.getvalue()


async def test_read_image_tool_integration(
    read_media_file_tool: ReadMediaFile, temp_work_dir: KaosPath, monkeypatch
) -> None:
    stub, calls = _recording_stub()
    monkeypatch.setattr("kimi_cli.tools.file.read_media.before_tool", stub)
    file = temp_work_dir / "bridge.png"
    await file.write_bytes(_make_png())

    result = await read_media_file_tool(MediaParams(path=str(file)))
    assert not result.is_error
    assert "[server] LSP: 2 diagnostics" in result.message
    assert calls[0][0] == "read_image"
    assert calls[0][1] == "read"
    assert calls[0][2]["path"] == str(file)

    calls.clear()
    result2 = await read_media_file_tool(MediaParams(path=str(file), connect=False))
    assert calls == []
    assert "[server]" not in result2.message


async def test_error_shaped_results_also_get_the_server_note(
    read_file_tool: ReadFile, monkeypatch
) -> None:
    """The wrapper merges at the single __call__ return: even ToolError
    results (missing file) carry the [server] line (contract §6)."""
    stub, _calls = _recording_stub()
    monkeypatch.setattr("kimi_cli.tools.file.read.before_tool", stub)
    result = await read_file_tool(ReadParams(path="definitely-missing-bridge.txt"))
    assert result.is_error
    assert "[server] LSP: 2 diagnostics" in result.message


async def test_edit_wrapper_is_getattr_safe_on_bare_instance(monkeypatch) -> None:
    """object.__new__ EditFile (no _tool/_work_dir): wrapper attributes must
    not be touched; cwd degrades to None and the merge still applies."""
    stub, calls = _recording_stub()
    monkeypatch.setattr("kimi_cli.tools.file.edit.before_tool", stub)

    async def _stub_core(self, params):  # bypass real core on bare instance
        return ToolOk(output="", message="base")

    monkeypatch.setattr(EditFile, "_call_core", _stub_core)
    tool = object.__new__(EditFile)
    ret = await tool(EditParams(path="f.txt", old_string="o", new_string="n"))
    assert ret.message == "base\n\n[server] LSP: 2 diagnostics"
    assert calls[0][2]["cwd"] is None
# ---------------------------------------------------------------------------
# Hard per-server deadline (review finding #1): a misbehaving server that
# dribbles stale/mismatched responses must not stretch the budget, because the
# client's per-read timeouts would otherwise stack up.
async def test_before_tool_enforces_hard_deadline_against_dribbling_server(
    monkeypatch,
) -> None:
    import asyncio
    import json

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readline()  # the request; ignore it
            # Never answer the request: emit endless id-mismatched lines.
            for i in range(200):
                writer.write(
                    json.dumps({"jsonrpc": "2.0", "id": 9000 + i, "result": "stale"}).encode()
                    + b"\n"
                )
                await writer.drain()
                await asyncio.sleep(0.05)
        except Exception:
            pass
        finally:
            writer.close()

    srv = await asyncio.start_server(_handle, "127.0.0.1", 0)
    host, port = srv.sockets[0].getsockname()[:2]
    monkeypatch.setattr(
        sdk_registry,
        "get_live_server_processes",
        lambda: [_live_entry("tcp", host, int(port))],
    )
    try:
        t0 = time.monotonic()
        assert await before_tool("read", "read", path="a.py", timeout=0.5) is None
        elapsed = time.monotonic() - t0
        # Nominal budget 0.5 s; allow generous slack but nowhere near the ~10 s
        # (9 stale skips x per-read timeout) the unbounded path would take.
        assert elapsed < 2.0, f"dribbling server stretched the budget: {elapsed:.2f}s"
    finally:
        srv.close()
        await srv.wait_closed()
