"""E2E: the four ``connect``-enabled file tools against a REAL sub-process server.

The unit tests in ``test_session_bridge.py`` cover the bridge with in-process
``JsonRpcServer`` instances and stubbed ``before_tool`` functions. This module
closes the remaining gap: it spawns the standalone example server
``tests/dummy_rpc_server.py`` (repo root) as an independent child process via
``kimi_agent_sdk._server_process.start_server_processes`` — the exact path a
real session takes — so the child lands in the SDK live registry, and then
drives ``ReadFile`` / ``WriteFile`` / ``EditFile`` / ``ReadMediaFile`` with
``connect=True`` (the default). No monkeypatching of the bridge anywhere: the
``[server] ...`` note in each tool result proves the full round trip
tool → session_bridge.before_tool → live registry → JSON-RPC sub-process →
merge_message → tool result.

Contract: docs/tool_connect_bridge.md; server example: docs/session_server_rpc.md §12.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from PIL import Image
from kaos.path import KaosPath

import kimi_agent_sdk._server_process as sdk_registry
from kimi_cli.tools.file.edit import EditFile
from kimi_cli.tools.file.edit.params import EditParams
from kimi_cli.tools.file.read import Params as ReadParams
from kimi_cli.tools.file.read import ReadFile
from kimi_cli.tools.file.read_media import Params as MediaParams
from kimi_cli.tools.file.read_media import ReadMediaFile
from kimi_cli.tools.file.write import Params as WriteParams
from kimi_cli.tools.file.write import WriteFile

# <repo root>/tests/dummy_rpc_server.py from kimi-cli/tests/tools/
DUMMY_SERVER = Path(__file__).resolve().parents[3] / "tests" / "dummy_rpc_server.py"

pytestmark = pytest.mark.skipif(
    not DUMMY_SERVER.exists(),
    reason=f"dummy server example not found at {DUMMY_SERVER}",
)


@pytest_asyncio.fixture
async def live_dummy_server(tmp_path: Path) -> AsyncIterator[sdk_registry.ServerProcess]:
    """Spawn the dummy server child through the SDK lifecycle (placeholder
    mode: the client resolves the port and substitutes ``<address>``/``<port>``
    into the child argv), registered in the module-level live registry the
    bridge consults."""
    assert DUMMY_SERVER.exists()
    entry = {
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
    servers = await sdk_registry.start_server_processes(
        tmp_path, [entry], session_id="bridge-e2e"
    )
    assert len(servers) == 1
    server = servers[0]
    assert server.ready is True, f"dummy server failed to start: {server.error}"
    # the child binds the substituted port a moment after spawn; wait until it
    # accepts connections so the first bridged tool call is never raced
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 20.0
    while loop.time() < deadline:
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(server.address, server.port), 2.0
            )
        except (OSError, asyncio.TimeoutError):
            await asyncio.sleep(0.05)
            continue
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        break
    else:
        pytest.fail(
            f"dummy server never started listening on {server.address}:{server.port}"
        )
    try:
        yield server
    finally:
        await sdk_registry.stop_server_processes(servers)


def _make_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 6), (51, 102, 204)).save(buf, format="PNG")
    return buf.getvalue()


# ── read tool ─────────────────────────────────────────────────────────────────

async def test_read_tool_e2e_real_subprocess(
    live_dummy_server: sdk_registry.ServerProcess,
    read_file_tool: ReadFile,
    temp_work_dir: KaosPath,
) -> None:
    file = temp_work_dir / "e2e_read.txt"
    await file.write_text("hello\nworld\n")

    result = await read_file_tool(ReadParams(path=str(file)))
    assert not result.is_error
    assert "hello" in result.output
    # The default before_tool handler echoes tool/action/path back.
    assert f"[server] ack before_tool: tool=read action=read path={file}" in result.message

    # multi-target read: paths is echoed as a python list repr (backslashes
    # escaped on Windows, hence repr of the list)
    result2 = await read_file_tool(ReadParams(file_path=[str(file), str(file)]))
    assert not result2.is_error
    assert f"paths={[str(file), str(file)]!r}" in result2.message

    # opt-out: connect=False performs no RPC, no [server] note
    result3 = await read_file_tool(ReadParams(path=str(file), connect=False))
    assert "[server]" not in result3.message


async def test_read_tool_error_result_still_merges_server_note(
    live_dummy_server: sdk_registry.ServerProcess,
    read_file_tool: ReadFile,
) -> None:
    """Even a ToolError result carries the [server] line (single merge point)."""
    result = await read_file_tool(ReadParams(path="definitely-missing-e2e.txt"))
    assert result.is_error
    assert "[server] ack before_tool: tool=read action=read" in result.message


# ── write tool ────────────────────────────────────────────────────────────────

async def test_write_tool_e2e_real_subprocess(
    live_dummy_server: sdk_registry.ServerProcess,
    write_file_tool: WriteFile,
    temp_work_dir: KaosPath,
) -> None:
    file = temp_work_dir / "e2e_write.txt"

    result = await write_file_tool(WriteParams(path=str(file), content="data"))
    assert not result.is_error
    assert f"[server] ack before_tool: tool=write action=write path={file}" in result.message

    result2 = await write_file_tool(
        WriteParams(path=str(file), content="data2", connect=False)
    )
    assert "[server]" not in result2.message


# ── edit tool ─────────────────────────────────────────────────────────────────

async def test_edit_tool_e2e_real_subprocess(
    live_dummy_server: sdk_registry.ServerProcess,
    edit_file_tool: EditFile,
    temp_work_dir: KaosPath,
) -> None:
    file = temp_work_dir / "e2e_edit.txt"
    await file.write_text("alpha\n")

    result = await edit_file_tool(
        EditParams(path=str(file), old_string="alpha", new_string="omega")
    )
    assert not result.is_error
    assert f"[server] ack before_tool: tool=edit action=edit path={file}" in result.message

    result2 = await edit_file_tool(
        EditParams(
            path=str(file), old_string="omega", new_string="beta", connect=False
        )
    )
    assert "[server]" not in result2.message


# ── read_image tool ───────────────────────────────────────────────────────────

async def test_read_image_tool_e2e_real_subprocess(
    live_dummy_server: sdk_registry.ServerProcess,
    read_media_file_tool: ReadMediaFile,
    temp_work_dir: KaosPath,
) -> None:
    file = temp_work_dir / "e2e_bridge.png"
    await file.write_bytes(_make_png())

    result = await read_media_file_tool(MediaParams(path=str(file)))
    assert not result.is_error
    assert f"[server] ack before_tool: tool=read_image action=read path={file}" in result.message

    result2 = await read_media_file_tool(MediaParams(path=str(file), connect=False))
    assert "[server]" not in result2.message


# ── registry hygiene ──────────────────────────────────────────────────────────

async def test_server_unregistered_after_stop(tmp_path: Path) -> None:
    """The child leaves the live registry on stop, so later tool calls make no
    RPC (the bridge degrades to 'no extra information')."""
    entry = {
        "cmd": sys.executable,
        "args": [
            str(DUMMY_SERVER),
            "--network", "tcp",
            "--host", "<address>",
            "--port", "<port>",
        ],
        "cwd": ".",
    }
    servers = await sdk_registry.start_server_processes(
        tmp_path, [entry], session_id="bridge-e2e"
    )
    server = servers[0]
    assert server.ready is True
    assert server in sdk_registry.get_live_server_processes()

    await sdk_registry.stop_server_processes(servers)

    assert server not in sdk_registry.get_live_server_processes()
    assert server.process is not None
    assert server.process.returncode is not None
