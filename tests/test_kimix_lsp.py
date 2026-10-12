"""Tests for ``tools/kimix_lsp.py`` — the syntax-check LSP-bridge session server.

Covers:

* unit: ``syntax_check_target`` / ``syntax_check_file`` / ``syntax_before_tool``
  (syntax errors are reported as ``path:line:offset: msg``; clean files,
  non-Python files, missing files and read actions return nothing);
* unit: ``build_registry`` keeps every builtin except the echo ``before_tool``
  and adds the syntax-check handler, without mutating ``DEFAULT_REGISTRY``;
* e2e: the script as a real sub-process on TCP, discovered through the atomic
  ``--config-out`` handshake — ``before_tool`` over the wire returns the
  diagnostic for a bad ``*.py`` target and an empty message for a clean one,
  builtins (``ping``) are intact, and ``--stop-file`` exits gracefully;
* e2e: the full SDK lifecycle loop (docs/session_server_rpc.md §8.2) with the
  real ``.kimix/config.json`` server entry — placeholder substitution, live
  registry, ``JsonRpcClient`` round trip, ``stop_server_processes`` teardown.

The e2e spawn mirrors ``tests/test_dummy_rpc_server_e2e.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from kimi_cli.session_server import JsonRpcClient

REPO_ROOT = Path(__file__).parent.parent
KIMIX_LSP = REPO_ROOT / "tools" / "kimix_lsp.py"

CONFIG_TIMEOUT = 20.0  # child bind + config-out handshake
CALL_TIMEOUT = 10.0  # per RPC call


def _load_module():
    spec = importlib.util.spec_from_file_location("kimix_lsp", KIMIX_LSP)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


kimix_lsp = _load_module()


BAD_PY = "def broken(:\n    pass\n"
GOOD_PY = "def fine():\n    return 42\n"


@pytest.fixture
def bad_py(tmp_path: Path) -> Path:
    p = tmp_path / "bad.py"
    p.write_text(BAD_PY, encoding="utf-8")
    return p


@pytest.fixture
def good_py(tmp_path: Path) -> Path:
    p = tmp_path / "good.py"
    p.write_text(GOOD_PY, encoding="utf-8")
    return p


# ── Unit: syntax_check_target ────────────────────────────────────────────────


def test_syntax_error_is_reported(bad_py: Path) -> None:
    message = kimix_lsp.syntax_check_target(str(bad_py), None)
    assert message is not None
    assert message.startswith(f"{bad_py}:1:")  # path:line:offset prefix
    assert "invalid syntax" in message


def test_clean_file_returns_nothing(good_py: Path) -> None:
    assert kimix_lsp.syntax_check_target(str(good_py), None) is None


def test_non_python_file_returns_nothing(tmp_path: Path) -> None:
    md = tmp_path / "notes.md"
    md.write_text("# not python\n", encoding="utf-8")
    assert kimix_lsp.syntax_check_target(str(md), None) is None


def test_missing_file_returns_nothing(tmp_path: Path) -> None:
    # A write creating a brand-new file: nothing on disk yet → nothing to say.
    assert kimix_lsp.syntax_check_target(str(tmp_path / "new.py"), None) is None


def test_relative_path_resolves_against_cwd(tmp_path: Path) -> None:
    bad = tmp_path / "rel_bad.py"
    bad.write_text(BAD_PY, encoding="utf-8")
    message = kimix_lsp.syntax_check_target("rel_bad.py", str(tmp_path))
    assert message is not None and "rel_bad.py" in message


def test_result_cached_by_mtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "cached.py"
    f.write_text(BAD_PY, encoding="utf-8")
    calls: list[Path] = []
    real_compile = kimix_lsp._compile_source

    def spy(path: Path) -> str | None:
        calls.append(path)
        return real_compile(path)

    monkeypatch.setattr(kimix_lsp, "_compile_source", spy)
    assert kimix_lsp.syntax_check_file(f) is not None
    assert kimix_lsp.syntax_check_file(f) is not None  # cache hit
    assert len(calls) == 1
    f.write_text(GOOD_PY, encoding="utf-8")  # mtime/size change → recheck
    assert kimix_lsp.syntax_check_file(f) is None
    assert len(calls) == 2


# ── Unit: syntax_before_tool (the before_tool handler) ───────────────────────


def _call_handler(tool: str, action: str, path: str | None, cwd: str | None = None):
    return kimix_lsp.syntax_before_tool(
        {"tool": tool, "action": action, "path": path, "paths": None, "cwd": cwd}
    )


def test_handler_reports_on_write(bad_py: Path) -> None:
    result = _call_handler("write", "write", str(bad_py))
    assert result["message"]
    assert "bad.py" in result["message"]
    assert result["tool"] == "write" and result["action"] == "write"
    assert result["handled"] is True


def test_handler_reports_on_edit(bad_py: Path) -> None:
    result = _call_handler("edit", "edit", str(bad_py))
    assert result["message"] and "bad.py" in result["message"]


def test_handler_silent_when_clean(good_py: Path) -> None:
    assert _call_handler("write", "write", str(good_py))["message"] == ""


def test_handler_silent_on_read_action(bad_py: Path) -> None:
    # read / read_image also carry connect — they must not trigger a check.
    assert _call_handler("read", "read", str(bad_py))["message"] == ""


def test_handler_silent_without_path() -> None:
    assert _call_handler("write", "write", None)["message"] == ""


def test_handler_defaults_action() -> None:
    # No action member → server default "pre" → not a check action.
    result = kimix_lsp.syntax_before_tool({"tool": "write"})
    assert result["action"] == "pre" and result["message"] == ""


# ── Unit: method registry ────────────────────────────────────────────────────


def test_method_set_matches_builtin_contract() -> None:
    """Same method names as the session-server DEFAULT_REGISTRY (docs §5)."""
    assert set(kimix_lsp.METHODS) == {
        "ping", "echo", "read_file", "write_file", "add", "before_tool",
    }
    assert kimix_lsp.METHODS["before_tool"] is kimix_lsp.syntax_before_tool


# ── E2E: real sub-process over TCP via --config-out ──────────────────────────


async def _spawn(tmp_path: Path) -> tuple[asyncio.subprocess.Process, dict[str, Any], Path]:
    cfg_path = tmp_path / "kimix_lsp.json"
    stop_file = tmp_path / "stop.flag"
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(KIMIX_LSP),
        "--network", "tcp",
        "--host", "127.0.0.1",
        "--port", "0",
        "--config-out", str(cfg_path),
        "--stop-file", str(stop_file),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    info: dict[str, Any] | None = None
    deadline = asyncio.get_running_loop().time() + CONFIG_TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        if cfg_path.exists():
            import json

            info = json.loads(cfg_path.read_text(encoding="utf-8"))
            break
        await asyncio.sleep(0.05)
    if info is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise TimeoutError("kimix_lsp did not write its --config-out handshake")
    return proc, info, stop_file


@pytest_asyncio.fixture
async def server(tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    proc, info, stop_file = await _spawn(tmp_path)
    try:
        yield info
    finally:
        stop_file.touch()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def test_e2e_before_tool_wire(server: dict[str, Any], tmp_path: Path) -> None:
    bad = tmp_path / "wire_bad.py"
    bad.write_text(BAD_PY, encoding="utf-8")
    good = tmp_path / "wire_good.py"
    good.write_text(GOOD_PY, encoding="utf-8")

    client = await JsonRpcClient.connect_tcp(server["address"], server["port"])
    try:
        # Builtin survives alongside the replaced before_tool.
        assert await client.call("ping", timeout=CALL_TIMEOUT) == "pong"

        result = await client.call(
            "before_tool",
            {
                "tool": "write",
                "action": "write",
                "path": str(bad),
                "paths": None,
                "cwd": None,
            },
            timeout=CALL_TIMEOUT,
        )
        assert isinstance(result, dict)
        assert "wire_bad.py" in result["message"]

        result = await client.call(
            "before_tool",
            {
                "tool": "edit",
                "action": "edit",
                "path": str(good),
                "paths": None,
                "cwd": None,
            },
            timeout=CALL_TIMEOUT,
        )
        assert isinstance(result, dict)
        assert result["message"] == ""  # no syntax error → nothing returned
    finally:
        await client.close()


# ── E2E: full SDK lifecycle loop with the real .kimix/config.json entry ──────


def test_config_entry_is_registered() -> None:
    """The workspace .kimix/config.json must declare exactly our kimix_lsp entry."""
    import json

    cfg = json.loads((REPO_ROOT / ".kimix" / "config.json").read_text(encoding="utf-8"))
    server = cfg.get("server")
    assert isinstance(server, list) and len(server) == 1
    entry = server[0]
    assert entry["cmd"] == "python"
    assert entry["args"][0] == "tools/kimix_lsp.py"
    assert "<address>" in entry["args"] and "<port>" in entry["args"]
    assert entry["network"] == "tcp"


async def test_e2e_sdk_lifecycle_with_config_entry(tmp_path: Path) -> None:
    """Spawn via start_server_processes exactly as the SDK session would."""
    import json

    from kimi_agent_sdk._server_process import (
        get_live_server_processes,
        start_server_processes,
        stop_server_processes,
    )

    cfg = json.loads((REPO_ROOT / ".kimix" / "config.json").read_text(encoding="utf-8"))
    servers = await start_server_processes(
        REPO_ROOT, cfg["server"], session_id="kimix-lsp-test"
    )
    assert len(servers) == 1
    srv = servers[0]
    try:
        assert srv.ready, f"server failed to start: {srv.error}"
        assert srv.network == "tcp" and srv.port > 0
        # Registered in the live registry (bridge discovery contract §9).
        assert any(s is srv for s in get_live_server_processes())

        bad = tmp_path / "sdk_bad.py"
        bad.write_text(BAD_PY, encoding="utf-8")
        # ready is set at spawn time, before the child binds its listener
        # (docs §8.2), so retry the connect until the server is accepting.
        client: JsonRpcClient | None = None
        deadline = asyncio.get_running_loop().time() + CONFIG_TIMEOUT
        while client is None:
            try:
                client = await JsonRpcClient.connect_tcp(srv.address, srv.port)
            except OSError:
                if srv.process is not None and srv.process.returncode is not None:
                    pytest.fail(
                        f"kimix_lsp child exited early rc={srv.process.returncode}"
                    )
                if asyncio.get_running_loop().time() > deadline:
                    pytest.fail("kimix_lsp did not accept connections in time")
                await asyncio.sleep(0.1)
        try:
            result = await client.call(
                "before_tool",
                {
                    "tool": "write",
                    "action": "write",
                    "path": str(bad),
                    "paths": None,
                    "cwd": None,
                },
                timeout=CALL_TIMEOUT,
            )
        finally:
            await client.close()
        assert isinstance(result, dict)
        assert "sdk_bad.py" in result["message"]
    finally:
        await stop_server_processes(servers)
    assert not any(s is srv for s in get_live_server_processes())
