from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import orjson
import pytest

from kimix.cli_impl import args as args_mod
from kimix.cli_impl import core, ipc, utils


def _write_registration(
    cache_dir: Path,
    project_dir: Path,
    pid: int,
    *,
    port: int = 4099,
) -> Path:
    path = cache_dir / f"pid_{pid}"
    path.write_bytes(
        orjson.dumps(
            {
                "pid": pid,
                "port": port,
                "token": f"token-{pid}",
                "cwd": str(project_dir),
            }
        )
    )
    return path


def test_server_receives_input_list_and_cleans_pid_file(tmp_path: Path) -> None:
    cache_dir = tmp_path / ".kimix_cache"
    with ipc.CLIIPCServer(cache_dir, tmp_path) as server:
        assert server.path == cache_dir / f"pid_{server.pid}"
        assert server.path.is_file()
        registration = orjson.loads(server.path.read_bytes())
        endpoint = ipc.ServerEndpoint(
            pid=server.pid,
            port=server.port,
            token=registration["token"],
            path=server.path,
        )

        ipc.send_messages(endpoint, ["first\nsecond\r\nthird"])

        assert server.read_input() == "first"
        assert server.read_input() == "second"
        assert server.read_input() == "third"

    assert not server.path.exists()


def test_input_delegates_to_ipc_server() -> None:
    class FakeServer:
        def read_input(self) -> str:
            return "remote"

    assert utils._input("prompt", [], FakeServer()) == "remote"  # type: ignore[arg-type]


def test_discover_servers_filters_process_and_project(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / ".kimix_cache"
    cache_dir.mkdir()
    valid = _write_registration(cache_dir, tmp_path, 101, port=4101)
    stale = _write_registration(cache_dir, tmp_path, 102, port=4102)
    other_project = cache_dir / "pid_103"
    other_project.write_bytes(
        orjson.dumps(
            {"pid": 103, "port": 4103, "token": "token", "cwd": str(tmp_path / "other")}
        )
    )
    (cache_dir / "pid_not-a-number").write_text("ignored", encoding="utf-8")
    monkeypatch.setattr(ipc, "_is_project_python_process", lambda pid, _: pid == 101)
    monkeypatch.setattr(ipc, "_pid_alive", lambda _: False)

    endpoints = ipc.discover_servers(cache_dir, tmp_path)

    assert [(endpoint.pid, endpoint.port) for endpoint in endpoints] == [(101, 4101)]
    assert valid.exists()
    assert not stale.exists()
    assert not other_project.exists()
    assert (cache_dir / "pid_not-a-number").exists()


def test_discovery_preserves_live_registration_after_validation_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / ".kimix_cache"
    cache_dir.mkdir()
    registration = _write_registration(cache_dir, tmp_path, 101)
    monkeypatch.setattr(ipc, "_is_project_python_process", lambda *_: False)
    monkeypatch.setattr(ipc, "_pid_alive", lambda _: True)

    assert ipc.discover_servers(cache_dir, tmp_path) == []
    assert registration.exists()


def test_pid_alive_accepts_live_child_process() -> None:
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert ipc._pid_alive(process.pid)
    finally:
        process.terminate()
        process.wait(timeout=5)
    assert not ipc._pid_alive(process.pid)


def test_discover_servers_honors_requested_pid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / ".kimix_cache"
    cache_dir.mkdir()
    _write_registration(cache_dir, tmp_path, 101)
    _write_registration(cache_dir, tmp_path, 102)
    monkeypatch.setattr(ipc, "_is_project_python_process", lambda *_: True)

    endpoints = ipc.discover_servers(cache_dir, tmp_path, requested_pid=102)

    assert [endpoint.pid for endpoint in endpoints] == [102]


def test_project_process_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(ipc, "_pid_alive", lambda _: True)
    monkeypatch.setattr(
        ipc,
        "_process_details",
        lambda _: ("/usr/bin/python3", "python -m kimix.cli", tmp_path),
    )
    assert ipc._is_project_python_process(123, tmp_path)

    monkeypatch.setattr(
        ipc,
        "_process_details",
        lambda _: ("/usr/bin/python3", "python -m kimix.cli", tmp_path / "elsewhere"),
    )
    assert not ipc._is_project_python_process(123, tmp_path)

    monkeypatch.setattr(
        ipc,
        "_process_details",
        lambda _: ("/usr/bin/node", "node kimix.js", tmp_path),
    )
    assert not ipc._is_project_python_process(123, tmp_path)


@pytest.mark.parametrize("option", ["--connect", "--connect=123"])
def test_connect_argument_skips_agent_initialization(
    monkeypatch: pytest.MonkeyPatch,
    option: str,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["kimix", option, "--config=must-not-be-loaded.json"],
    )
    monkeypatch.setattr(
        args_mod,
        "_load_project_mcp_config",
        lambda: pytest.fail("client loaded MCP config"),
    )
    from kimix.utils import config as config_mod

    monkeypatch.setattr(
        config_mod,
        "init",
        lambda **_: pytest.fail("client initialized agent config"),
    )

    subcommand, parsed = args_mod.set_arg()

    assert subcommand == "connect"
    assert parsed.connect == ("" if option == "--connect" else "123")


def test_run_cli_dispatches_connect_without_starting_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected: list[str] = []
    monkeypatch.setattr(core, "_check_native", lambda: None)
    monkeypatch.setattr(
        core,
        "set_arg",
        lambda: ("connect", SimpleNamespace(connect="123")),
    )
    monkeypatch.setattr(core, "_connect_cli", connected.append)
    monkeypatch.setattr(
        core,
        "_client_cli",
        lambda: pytest.fail("connected client started the agent loop"),
    )

    core._run_cli()

    assert connected == ["123"]


def test_connect_cli_forwards_stdin_as_one_message_list(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    endpoint = ipc.ServerEndpoint(123, 4099, "token", tmp_path / "pid_123")
    monkeypatch.setattr(core, "discover_servers", lambda *_, **__: [endpoint])
    monkeypatch.setattr(core.constants, "curr_dir", tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO("first\nsecond\r\n"))
    sent: list[tuple[ipc.ServerEndpoint, list[str]]] = []
    monkeypatch.setattr(
        core,
        "send_messages",
        lambda target, messages: sent.append((target, messages)),
    )

    core._connect_cli("")

    assert sent == [(endpoint, ["first\nsecond\r\n"])]


def test_server_wrapper_always_closes_registration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class FakeServer:
        pid = 123

        def __init__(self, cache_dir: Path, project_dir: Path) -> None:
            assert cache_dir == tmp_path / ".kimix_cache"
            assert project_dir == tmp_path

        def __enter__(self) -> FakeServer:
            events.append("start")
            return self

        def __exit__(self, *_: object) -> None:
            events.append("close")

    monkeypatch.setattr(core.constants, "curr_dir", tmp_path)
    monkeypatch.setattr(core, "CLIIPCServer", FakeServer)
    monkeypatch.setattr(core, "discover_servers", lambda *_, **__: events.append("discover"))

    def run_loop(server: FakeServer) -> None:
        assert server.pid == 123
        raise RuntimeError("stop loop")

    monkeypatch.setattr(core, "_client_cli_loop", run_loop)
    with pytest.raises(RuntimeError, match="stop loop"):
        core._client_cli()

    assert events == ["start", "discover", "close"]


def test_active_ipc_server_also_handles_command_prompts() -> None:
    class FakeServer:
        def read_input(self) -> str:
            return "command answer"

    server = FakeServer()
    utils._set_active_ipc_server(server)  # type: ignore[arg-type]
    try:
        assert utils._input("", []) == "command answer"
    finally:
        utils._set_active_ipc_server(None)
