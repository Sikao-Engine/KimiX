"""Unit tests for kimi_agent_sdk._server_process (nested sub-process servers).

Covers: entry parsing/validation, conflicting-key handling with the dynamic
``config`` mode, real sub-process spawn + config-file handshake, the timeout
path (with monkeypatched back-off delays), process termination on stop, and the
module-level live-server registry (register on start, unregister on
stop/sync-stop/cancellation, snapshot copies, never-raising accessors).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

import pytest

import kimi_agent_sdk._server_process as server_process
from kimi_agent_sdk._server_process import (
    CONFIG_WAIT_DELAYS,
    ServerProcess,
    ServerSpec,
    get_live_server_processes,
    parse_server_entry,
    start_server_processes,
    stop_server_processes,
    stop_server_processes_sync,
)

# Child scripts: write-then-sleep / sleep-only. Kept tiny and dependency-free
# (they run under sys.executable as plain standalone scripts).
_WRITE_CFG_THEN_SLEEP = """\
import pathlib, sys, time
p = pathlib.Path(sys.argv[1])
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(sys.argv[2], encoding="utf-8")
time.sleep(float(sys.argv[3]))
"""

_SLEEP = """\
import sys, time
time.sleep(float(sys.argv[1]))
"""


def _write_script(tmp_path: Path, name: str, body: str) -> str:
    script = tmp_path / name
    script.write_text(body, encoding="utf-8")
    return str(script)


@pytest.fixture(autouse=True)
def _isolated_live_registry() -> Any:
    """Keep the module-level live-server registry from leaking across tests.

    start/stop already add/remove entries, but a failing assertion could leave
    one behind; clear unconditionally around every test in this module.
    """
    server_process._clear_live_registry_for_tests()
    yield None
    server_process._clear_live_registry_for_tests()


# ── parse_server_entry: valid entries ─────────────────────────────────────


def test_parse_static_tcp_defaults() -> None:
    spec = parse_server_entry({"cmd": "python", "args": ["--flag"], "port": 12345}, 0)
    assert isinstance(spec, ServerSpec)
    assert spec.cmd == "python"
    assert spec.args == ["--flag"]
    assert spec.cwd == "."
    assert spec.network == "tcp"
    assert spec.address == "127.0.0.1"
    assert spec.port == 12345
    assert spec.config is None


def test_parse_static_http() -> None:
    spec = parse_server_entry(
        {"cmd": "srv", "network": "http", "address": "0.0.0.0", "port": 80, "cwd": "sub"},
        1,
    )
    assert spec is not None
    assert spec.network == "http"
    assert spec.address == "0.0.0.0"
    assert spec.port == 80
    assert spec.cwd == "sub"


def test_parse_dynamic_config_mode() -> None:
    spec = parse_server_entry({"cmd": "srv", "config": ".kimix/network.json"}, 0)
    assert spec is not None
    assert spec.config == ".kimix/network.json"
    # dynamic mode: port has no static default
    assert spec.port == 0
    assert spec.network == "tcp"
    assert spec.address == "127.0.0.1"


# ── parse_server_entry: config + conflicting keys ────────────────────────


def test_parse_conflicting_keys_ignored_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    entry: dict[str, Any] = {
        "cmd": "srv",
        "config": "net.json",
        "network": "http",
        "address": "1.2.3.4",
        "port": 9999,
    }
    with caplog.at_level(logging.ERROR):
        spec = parse_server_entry(entry, 2)
    assert spec is not None
    assert spec.config == "net.json"
    # conflicting keys were dropped: the static defaults survive (file wins)
    assert spec.network == "tcp"
    assert spec.address == "127.0.0.1"
    assert spec.port == 0
    assert "server[2]" in caplog.text
    assert "ignoring conflicting keys" in caplog.text
    for key in ("network", "address", "port"):
        assert key in caplog.text


# ── parse_server_entry: invalid entries ──────────────────────────────────


@pytest.mark.parametrize(
    "entry",
    [
        None,
        "not-a-dict",
        [],
        {"port": 5},  # missing cmd
        {"cmd": "   ", "port": 5},  # empty cmd
        {"cmd": "srv"},  # static mode, missing port
        {"cmd": "srv", "port": 0},  # static mode, port <= 0
        {"cmd": "srv", "port": -1},  # static mode, port <= 0
        {"cmd": "srv", "port": "not-an-int"},  # bad port type
        {"cmd": "srv", "port": 5, "network": "udp"},  # unknown network
        {"cmd": "srv", "config": 5},  # config must be a string
    ],
)
def test_parse_invalid_entries(entry: Any, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR):
        assert parse_server_entry(entry, 7) is None
    assert "server[7]" in caplog.text


# ── start_server_processes: input validation ─────────────────────────────


async def test_start_non_list_config_logs_and_skips(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR):
        servers = await start_server_processes(tmp_path, {"not": "a list"})
    assert servers == []
    assert "must be a list" in caplog.text


async def test_start_invalid_entries_are_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR):
        servers = await start_server_processes(
            tmp_path, ["oops", {"no_cmd": True}, {"cmd": "srv"}]
        )
    assert servers == []
    assert "server[0]" in caplog.text and "server[2]" in caplog.text


# ── dynamic mode: real subprocess writing the config file ────────────────


async def test_start_dynamic_mode_ready(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    cfg_path = work / ".kimix" / "network.json"
    script = _write_script(tmp_path, "write_cfg.py", _WRITE_CFG_THEN_SLEEP)
    servers = None
    try:
        servers = await start_server_processes(
            work,
            [
                {
                    "cmd": sys.executable,
                    "args": [script, str(cfg_path), '{"network": "tcp", "address": "127.0.0.1", "port": 23456}', "30"],
                    "config": ".kimix/network.json",
                }
            ],
        )
        assert len(servers) == 1
        server = servers[0]
        assert server.ready is True
        assert server.error is None
        assert server.network == "tcp"
        assert server.address == "127.0.0.1"
        assert server.port == 23456  # resolved from the written file, not the spec
        assert server.process is not None and server.process.returncode is None
    finally:
        if servers is not None:
            await stop_server_processes(servers)


# ── dynamic mode: timeout path (monkeypatched delays) ───────────────────


async def test_start_dynamic_timeout_terminates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(server_process, "CONFIG_WAIT_DELAYS", (0.01,) * 5)
    work = tmp_path / "work"
    work.mkdir()
    script = _write_script(tmp_path, "sleeper.py", _SLEEP)
    with caplog.at_level(logging.ERROR):
        servers = await start_server_processes(
            work,
            [
                {
                    "cmd": sys.executable,
                    "args": [script, "30"],
                    "config": ".kimix/never.json",
                }
            ],
        )
    assert len(servers) == 1
    server = servers[0]
    assert server.ready is False
    assert server.error is not None and "timeout" in server.error
    assert "timeout" in caplog.text
    # the child was terminated and reaped
    assert server.process is not None
    assert server.process.returncode is not None
    await stop_server_processes(servers)  # idempotent, must not raise


async def test_start_dynamic_invalid_content_terminates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(server_process, "CONFIG_WAIT_DELAYS", (0.01,) * 5)
    work = tmp_path / "work"
    work.mkdir()
    script = _write_script(tmp_path, "bad_cfg.py", _WRITE_CFG_THEN_SLEEP)
    with caplog.at_level(logging.ERROR):
        servers = await start_server_processes(
            work,
            [
                {
                    "cmd": sys.executable,
                    "args": [script, str(work / ".kimix" / "network.json"), "[1, 2, 3]", "30"],
                    "config": ".kimix/network.json",
                }
            ],
        )
    assert len(servers) == 1
    server = servers[0]
    assert server.ready is False
    assert server.error is not None
    assert "must contain a JSON object" in server.error
    assert server.process is not None and server.process.returncode is not None
    await stop_server_processes(servers)


# ── static mode: spawn + stop terminates the process ─────────────────────


async def test_static_start_ready_and_stop_terminates(tmp_path: Path) -> None:
    script = _write_script(tmp_path, "sleeper.py", _SLEEP)
    servers = await start_server_processes(
        tmp_path,
        [{"cmd": sys.executable, "args": [script, "60"], "port": 40001}],
    )
    assert len(servers) == 1
    server = servers[0]
    assert server.ready is True
    assert server.port == 40001
    assert server.process is not None
    assert server.process.returncode is None  # still running
    await stop_server_processes([server])
    assert server.process.returncode is not None  # terminated


async def test_stop_server_processes_sync_terminates(tmp_path: Path) -> None:
    script = _write_script(tmp_path, "sleeper.py", _SLEEP)
    servers = await start_server_processes(
        tmp_path,
        [{"cmd": sys.executable, "args": [script, "60"], "port": 40002}],
    )
    server = servers[0]
    assert server.ready and server.process is not None
    # sync variant: best-effort terminate (no loop, no reap possible here)
    stop_server_processes_sync(servers)
    # reap on the running loop to observe the exit
    await asyncio.wait_for(server.process.wait(), 10)
    assert server.process.returncode is not None
    # subsequent async stop is a no-op and must not raise
    await stop_server_processes(servers)


def test_stop_helpers_tolerate_none_and_empty() -> None:
    stop_server_processes_sync(None)
    stop_server_processes_sync([])
    stop_server_processes_sync([ServerProcess(ServerSpec(cmd="x", port=1), Path("."))])


# ── _session.py integration surface ──────────────────────────────────────


def test_config_wait_delays_pinned() -> None:
    """The documented dynamic-mode back-off schedule is part of the contract."""
    assert CONFIG_WAIT_DELAYS == (1, 2, 4, 8, 16)
    assert server_process.CONFIG_WAIT_DELAYS is CONFIG_WAIT_DELAYS


# ── fix 4: cancellation mid-startup must not orphan children ─────────────


async def test_start_appends_to_live_out_list(tmp_path: Path) -> None:
    script = _write_script(tmp_path, "sleeper.py", _SLEEP)
    out: list[ServerProcess] = []
    servers = await start_server_processes(
        tmp_path,
        [{"cmd": sys.executable, "args": [script, "60"], "port": 40003}],
        out=out,
    )
    assert servers is out  # the caller's list is returned, not replaced
    assert len(out) == 1
    assert out[0].ready is True
    await stop_server_processes(out)


async def test_start_cancellation_terminates_spawned_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_process, "CONFIG_WAIT_DELAYS", (30,))
    work = tmp_path / "work"
    work.mkdir()
    script = _write_script(tmp_path, "sleeper.py", _SLEEP)
    out: list[ServerProcess] = []
    task = asyncio.create_task(
        start_server_processes(
            work,
            [{"cmd": sys.executable, "args": [script, "120"], "config": ".kimix/never.json"}],
            out=out,
        )
    )
    # wait until the child exists and the config poll is in progress
    deadline = asyncio.get_running_loop().time() + 10
    while not out or out[0].process is None:
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("child process was never spawned")
        await asyncio.sleep(0.02)
    proc = out[0].process
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # the live list still holds the server, and its child was terminated
    assert len(out) == 1
    assert out[0].process is proc
    rc = await asyncio.wait_for(proc.wait(), 15)
    assert rc is not None


# ── fix 5: transient config-file read failures are retried ───────────────


async def test_config_transient_read_failure_is_retried(tmp_path: Path) -> None:
    work = tmp_path / "work"
    cfg = work / ".kimix" / "network.json"
    cfg.parent.mkdir(parents=True)
    # simulate a non-atomic writer caught mid-write (invalid JSON)
    cfg.write_text('{"network": "tcp", "address": ', encoding="utf-8")

    async def finish_the_write() -> None:
        await asyncio.sleep(0.1)  # lands between retry 1 and retry 2
        cfg.write_text(
            '{"network": "tcp", "address": "127.0.0.1", "port": 41234}', encoding="utf-8"
        )

    spec = ServerSpec(cmd="x", config=".kimix/network.json")
    server = ServerProcess(spec, work)
    fixer = asyncio.create_task(finish_the_write())
    try:
        assert await server._wait_for_config() is True
    finally:
        await fixer
    assert server.error is None
    assert server.port == 41234


async def test_config_persistent_read_failure_still_fails(tmp_path: Path) -> None:
    work = tmp_path / "work"
    cfg = work / ".kimix" / "network.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{ permanently broken", encoding="utf-8")
    spec = ServerSpec(cmd="x", config=".kimix/network.json")
    server = ServerProcess(spec, work)
    assert await server._wait_for_config() is False
    assert server.error is not None
    assert "failed to read server config file" in server.error


async def test_session_accessor_and_startup_never_raises(tmp_path: Path) -> None:
    from kaos.path import KaosPath

    from kimi_agent_sdk._session import Session, _start_configured_servers

    session = Session.__new__(Session)
    session._server_processes = []
    assert session.get_server_processes() is session._server_processes

    # no server config: nothing happens
    await _start_configured_servers(session, KaosPath(str(tmp_path)), {"other": 1})
    assert session._server_processes == []

    # a spawn failure (nonexistent cmd) must not raise out of the lifecycle helper
    await _start_configured_servers(
        session,
        KaosPath(str(tmp_path)),
        {"server": [{"cmd": "definitely-not-a-real-binary-xyzzy", "port": 5}]},
    )
    assert len(session._server_processes) == 1
    assert session._server_processes[0].ready is False
    await stop_server_processes(session._server_processes)


# ── live server registry (get_live_server_processes) ───────────────────


def test_live_registry_is_empty_to_start() -> None:
    assert get_live_server_processes() == []
    assert server_process.get_live_server_processes() == []


async def test_live_registry_start_then_stop(tmp_path: Path) -> None:
    """A started server is registered; stopping it removes it again."""
    script = _write_script(tmp_path, "sleeper.py", _SLEEP)
    servers = await start_server_processes(
        tmp_path,
        [{"cmd": sys.executable, "args": [script, "60"], "port": 40011}],
    )
    assert len(servers) == 1
    server = servers[0]
    try:
        live = get_live_server_processes()
        assert any(s is server for s in live)
        # the accessor returns a snapshot copy, not the registry itself
        assert live is not server_process._LIVE_SERVERS
        live.clear()
        assert any(s is server for s in get_live_server_processes())
    finally:
        await stop_server_processes(servers)
    assert not any(s is server for s in get_live_server_processes())
    assert get_live_server_processes() == []


async def test_live_registry_registers_via_out_list(tmp_path: Path) -> None:
    """The caller-owned ``out`` buffer registers identically to the return list."""
    script = _write_script(tmp_path, "sleeper.py", _SLEEP)
    out: list[ServerProcess] = []
    servers = await start_server_processes(
        tmp_path,
        [{"cmd": sys.executable, "args": [script, "60"], "port": 40012}],
        out=out,
    )
    assert servers is out and len(out) == 1
    assert any(s is out[0] for s in get_live_server_processes())
    await stop_server_processes(out)
    assert get_live_server_processes() == []


async def test_live_registry_sync_stop_removes(tmp_path: Path) -> None:
    script = _write_script(tmp_path, "sleeper.py", _SLEEP)
    servers = await start_server_processes(
        tmp_path,
        [{"cmd": sys.executable, "args": [script, "60"], "port": 40013}],
    )
    server = servers[0]
    assert any(s is server for s in get_live_server_processes())
    stop_server_processes_sync(servers)
    assert not any(s is server for s in get_live_server_processes())
    assert get_live_server_processes() == []
    if server.process is not None:
        await asyncio.wait_for(server.process.wait(), 10)


async def test_live_registry_cancellation_removes_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancel mid-start: registered before start(), removed by the cleanup."""
    monkeypatch.setattr(server_process, "CONFIG_WAIT_DELAYS", (30,))
    work = tmp_path / "work"
    work.mkdir()
    script = _write_script(tmp_path, "sleeper.py", _SLEEP)
    out: list[ServerProcess] = []
    task = asyncio.create_task(
        start_server_processes(
            work,
            [{"cmd": sys.executable, "args": [script, "120"], "config": ".kimix/never.json"}],
            out=out,
        )
    )
    deadline = asyncio.get_running_loop().time() + 10
    while not out or out[0].process is None:
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("child process was never spawned")
        await asyncio.sleep(0.02)
    server = out[0]
    # registration happens *before* start() completes: live while still polling
    assert any(s is server for s in get_live_server_processes())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not any(s is server for s in get_live_server_processes())
    assert server.process is not None
    assert await asyncio.wait_for(server.process.wait(), 15) is not None


def test_live_registry_removal_tolerates_unregistered_servers() -> None:
    """stop() on a never-started server must not raise and must not add entries."""
    server = ServerProcess(ServerSpec(cmd="x", port=1), Path("."))
    stop_server_processes_sync([server])
    stop_server_processes_sync(None)
    assert get_live_server_processes() == []


class _ExplodingLock:
    """threading.Lock stand-in whose acquisition always fails."""

    def __enter__(self) -> None:
        raise RuntimeError("lock boom")

    def __exit__(self, *args: object) -> None:
        return None


def test_get_live_server_processes_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_process, "_LIVE_LOCK", _ExplodingLock())
    assert get_live_server_processes() == []


def test_live_registry_registration_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_process, "_LIVE_LOCK", _ExplodingLock())
    server = ServerProcess(ServerSpec(cmd="x", port=1), Path("."))
    server_process._register_live_server(server)
    server_process._unregister_live_servers([server])
async def test_live_registry_drops_server_whose_start_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix for review finding #4: a server whose start() raises a normal
    Exception must not linger in the live registry (the caller may discard the
    returned list, so nothing else would ever unregister it).
    """

    async def _boom(self: Any) -> None:  # noqa: ANN401
        raise RuntimeError("start exploded")

    monkeypatch.setattr(ServerProcess, "start", _boom)
    entry = {"cmd": sys.executable, "args": ["-c", "pass"], "port": 1}
    servers = await start_server_processes(tmp_path, [entry])
    assert len(servers) == 1  # the entry is still reported to the caller
    assert get_live_server_processes() == []  # but not left in the registry


async def test_live_registry_keeps_server_when_later_entry_start_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the server that was starting is dropped; earlier good ones stay."""
    calls = {"n": 0}
    real_start = ServerProcess.start

    async def _second_booms(self: Any) -> None:  # noqa: ANN401
        calls["n"] += 1
        if calls["n"] == 1:
            await real_start(self)
            return
        raise RuntimeError("start exploded")

    monkeypatch.setattr(ServerProcess, "start", _second_booms)
    entries = [
        {"cmd": sys.executable, "args": ["-c", "pass"], "port": 1},
        {"cmd": sys.executable, "args": ["-c", "pass"], "port": 2},
    ]
    servers = await start_server_processes(tmp_path, entries)
    try:
        assert len(servers) == 2
        assert get_live_server_processes() == [servers[0]]
    finally:
        await stop_server_processes(servers)
    assert get_live_server_processes() == []
