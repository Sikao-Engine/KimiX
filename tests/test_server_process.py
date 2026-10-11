"""Unit tests for kimi_agent_sdk._server_process (nested sub-process servers).

Covers: entry parsing/validation (the legacy ``config`` key is rejected;
``port`` is an optional positive-integer *preferred starting port*), free-port
allocation (:func:`_is_port_free` / :func:`_find_free_port`, including the
increment-on-conflict and exhaustion paths), placeholder substitution
(``<address>`` / ``<port>`` / ``<session_id>``), real sub-process spawn +
stop, an end-to-end port conflict (child actually binds the incremented port),
cancellation cleanup (children terminated, live registry drained), and the
module-level live-server registry (register on start, unregister on
stop/sync-stop, snapshot copies, never-raising accessors).
"""

from __future__ import annotations

import asyncio
import logging
import socket
import sys
from pathlib import Path
from typing import Any

import pytest

import kimi_agent_sdk._server_process as server_process
from kimi_agent_sdk._server_process import (
    MAX_PORT_ATTEMPTS,
    ServerProcess,
    ServerSpec,
    _find_free_port,
    _is_port_free,
    _substitute_placeholders,
    get_live_server_processes,
    parse_server_entry,
    start_server_processes,
    stop_server_processes,
    stop_server_processes_sync,
)

# Child scripts. Kept tiny and dependency-free (they run under sys.executable
# as plain standalone scripts).

# argv[1]=out file, argv[2]=sleep seconds; records argv[3:] one line per arg,
# then sleeps. Used to observe what the parent substituted into the child's
# command line.
_WRITE_ARGV_THEN_SLEEP = """\
import pathlib, sys, time
out = pathlib.Path(sys.argv[1])
sleep_s = float(sys.argv[2])
out.write_text("\\n".join(sys.argv[3:]), encoding="utf-8")
time.sleep(sleep_s)
"""

# argv[1]=host, argv[2]=port, argv[3]=flag file, argv[4]=sleep seconds.
# Binds host:port, listens, writes the bound port into the flag file, sleeps.
_BIND_THEN_SLEEP = """\
import pathlib, socket, sys, time
host = sys.argv[1]
port = int(sys.argv[2])
flag = pathlib.Path(sys.argv[3])
sleep_s = float(sys.argv[4])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.bind((host, port))
sock.listen(1)
flag.write_text(str(port), encoding="utf-8")
time.sleep(sleep_s)
"""

_SLEEP = """\
import sys, time
time.sleep(float(sys.argv[1]))
"""


def _write_script(tmp_path: Path, name: str, body: str) -> str:
    script = tmp_path / name
    script.write_text(body, encoding="utf-8")
    return str(script)


async def _wait_for_file(path: Path, timeout: float = 20.0) -> str:
    """Poll until ``path`` exists and read it (child-side synchronisation)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if path.exists():
            try:
                return path.read_text(encoding="utf-8")
            except OSError:
                pass  # caught mid-write; keep polling
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out after {timeout}s waiting for {path}")


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
    assert not hasattr(spec, "config")  # ServerSpec no longer has a config field


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


def test_parse_static_entry_without_port_is_auto() -> None:
    """``port`` is optional: an absent port means "OS-assigned ephemeral"."""
    spec = parse_server_entry({"cmd": "python", "args": ["--flag"]}, 0)
    assert isinstance(spec, ServerSpec)
    assert spec.port == 0


def test_parse_rejects_config_key(caplog: pytest.LogCaptureFixture) -> None:
    """The legacy dynamic ``config`` mode is gone: such entries are rejected."""
    entry: dict[str, Any] = {"cmd": "srv", "config": ".kimix/network.json"}
    with caplog.at_level(logging.ERROR):
        spec = parse_server_entry(entry, 3)
    assert spec is None
    assert "server[3]" in caplog.text
    assert "'config' key is no longer supported" in caplog.text


# ── parse_server_entry: invalid entries ──────────────────────────────────


@pytest.mark.parametrize(
    "entry",
    [
        None,
        "not-a-dict",
        [],
        {"port": 5},  # missing cmd
        {"cmd": "   ", "port": 5},  # empty cmd
        {"cmd": "srv", "port": 0},  # port must be > 0 (0 means "absent = auto")
        {"cmd": "srv", "port": -1},  # port must be > 0
        {"cmd": "srv", "port": "not-an-int"},  # bad port type
        {"cmd": "srv", "port": True},  # bool is explicitly not an int here
        {"cmd": "srv", "port": False},  # ... ditto
        {"cmd": "srv", "port": 5, "network": "udp"},  # unknown network
        {"cmd": "srv", "config": 5},  # config key (any value) rejected
        {"cmd": "srv", "config": None},  # ... ditto
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
            tmp_path,
            ["oops", {"no_cmd": True}, {"cmd": "srv", "port": 0}],
        )
    assert servers == []
    assert "server[0]" in caplog.text and "server[2]" in caplog.text


# ── _is_port_free ────────────────────────────────────────────────────────


def test_is_port_free_reflects_live_bind() -> None:
    """A currently-bound port is not free; after close it is."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
        assert _is_port_free("127.0.0.1", port) is False
    finally:
        sock.close()
    assert _is_port_free("127.0.0.1", port) is True


# ── _find_free_port ──────────────────────────────────────────────────────


@pytest.mark.parametrize("address", ["127.0.0.1", "0.0.0.0", ""])
def test_find_free_port_auto_returns_usable_port(address: str) -> None:
    """preferred <= 0: the OS assigns an ephemeral port via bind(port 0)."""
    port = _find_free_port(address, 0)
    assert isinstance(port, int)
    assert port > 0


def test_find_free_port_auto_defaults_preferred_to_zero() -> None:
    assert _find_free_port("127.0.0.1") > 0


def test_find_free_port_increments_past_occupied_port() -> None:
    """preferred port in use -> probe returns a *later* free port."""
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocked = int(blocker.getsockname()[1])
    try:
        chosen = _find_free_port("127.0.0.1", blocked)
        assert chosen > blocked  # the occupied preferred port was skipped
        assert chosen <= blocked + MAX_PORT_ATTEMPTS
        assert _is_port_free("127.0.0.1", chosen) is True
    finally:
        blocker.close()


def _free_preferred_port() -> int:
    """A port that is free right now, for use as a *preferred* value.

    Discovered via bind(0) then closed, so it lands in the OS ephemeral range
    and avoids hard-coded numbers that collide with Windows excluded port
    ranges or with TIME_WAIT sockets left by earlier tests.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def test_find_free_port_prefers_the_requested_port_when_free() -> None:
    """A free preferred port is returned unchanged (no needless increment)."""
    preferred = _free_preferred_port()
    port = _find_free_port("127.0.0.1", preferred)
    assert port >= preferred
    assert port <= preferred + MAX_PORT_ATTEMPTS
    # The common case: nothing else grabbed it in between.
    assert port == preferred or not _is_port_free("127.0.0.1", preferred)


def test_find_free_port_raises_when_all_attempts_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exhaustion: MAX_PORT_ATTEMPTS consecutive busy ports -> RuntimeError."""
    probed: list[int] = []

    def never_free(_address: str, port: int) -> bool:
        probed.append(port)
        return False

    monkeypatch.setattr(server_process, "_is_port_free", never_free)
    monkeypatch.setattr(server_process, "MAX_PORT_ATTEMPTS", 5)
    with pytest.raises(RuntimeError, match="no free port"):
        _find_free_port("127.0.0.1", 50000)
    assert probed == [50000, 50001, 50002, 50003, 50004]


def test_find_free_port_stops_at_top_of_port_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A preferred port near 65535 stops probing past the range instead of
    walking MAX_PORT_ATTEMPTS invalid ports."""
    probed: list[int] = []

    def never_free(_address: str, port: int) -> bool:
        probed.append(port)
        return False

    monkeypatch.setattr(server_process, "_is_port_free", never_free)
    with pytest.raises(RuntimeError, match="no free port"):
        _find_free_port("127.0.0.1", 65533)
    assert probed == [65533, 65534, 65535]


@pytest.mark.parametrize(
    ("address", "probe_addr"),
    [("0.0.0.0", ""), ("", ""), ("127.0.0.1", "127.0.0.1")],
)
def test_find_free_port_normalises_wildcard_address(
    monkeypatch: pytest.MonkeyPatch, address: str, probe_addr: str
) -> None:
    """"0.0.0.0" and "" are probed against the wildcard address, others as-is."""
    seen: list[str] = []

    def never_free(seen_addr: str, _port: int) -> bool:
        seen.append(seen_addr)
        return False

    monkeypatch.setattr(server_process, "_is_port_free", never_free)
    monkeypatch.setattr(server_process, "MAX_PORT_ATTEMPTS", 2)
    with pytest.raises(RuntimeError):
        _find_free_port(address, 40000)
    assert seen == [probe_addr, probe_addr]


def test_max_port_attempts_pinned() -> None:
    """The documented probe budget is part of the contract."""
    assert MAX_PORT_ATTEMPTS == 100
    assert server_process.MAX_PORT_ATTEMPTS == 100


# ── _substitute_placeholders ─────────────────────────────────────────────


def test_substitute_all_three_tokens_and_repeats() -> None:
    values = [
        "--host=<address>",
        "<address>:<port>:<address>",
        "--port=<port>",
        "sid=<session_id>",
        "no tokens here",
    ]
    out = _substitute_placeholders(values, "1.2.3.4", 555, "sess-9")
    assert out == [
        "--host=1.2.3.4",
        "1.2.3.4:555:1.2.3.4",
        "--port=555",
        "sid=sess-9",
        "no tokens here",
    ]


@pytest.mark.parametrize("session_id", [None, ""])
def test_substitute_empty_session_id_becomes_default(session_id: str | None) -> None:
    (out,) = _substitute_placeholders(["<session_id>"], "127.0.0.1", 80, session_id)
    assert out == "default"


def test_substitute_returns_new_list_and_handles_empty_input() -> None:
    values: list[str] = ["<port>"]
    out = _substitute_placeholders(values, "h", 1, "s")
    assert out == ["1"]
    assert out is not values  # input list untouched
    assert values == ["<port>"]
    assert _substitute_placeholders([], "h", 1, "s") == []


# ── start: placeholder substitution reaches the child argv ───────────────


async def test_start_substitutes_placeholders_into_child_args(tmp_path: Path) -> None:
    script = _write_script(tmp_path, "argv.py", _WRITE_ARGV_THEN_SLEEP)
    out_file = tmp_path / "argv.txt"
    entry = {
        "cmd": sys.executable,
        "args": [
            script,
            str(out_file),
            "120",
            "--host=<address>",
            "--port=<port>",
            "--sid=<session_id>",
            "--port-again=<port>",
        ],
    }
    servers = await start_server_processes(tmp_path, [entry], session_id="sess-42")
    try:
        assert len(servers) == 1
        server = servers[0]
        assert server.ready is True
        assert server.error is None
        assert server.port > 0
        recorded = (await _wait_for_file(out_file)).splitlines()
        assert f"--host={server.address}" in recorded
        assert f"--port={server.port}" in recorded
        assert f"--port-again={server.port}" in recorded
        assert "--sid=sess-42" in recorded
    finally:
        await stop_server_processes(servers)


async def test_start_without_session_id_substitutes_default(tmp_path: Path) -> None:
    script = _write_script(tmp_path, "argv.py", _WRITE_ARGV_THEN_SLEEP)
    out_file = tmp_path / "argv.txt"
    entry = {
        "cmd": sys.executable,
        "args": [script, str(out_file), "120", "--sid=<session_id>"],
    }
    servers = await start_server_processes(tmp_path, [entry])
    try:
        server = servers[0]
        assert server.ready is True
        recorded = (await _wait_for_file(out_file)).splitlines()
        assert "--sid=default" in recorded
    finally:
        await stop_server_processes(servers)


async def test_start_auto_port_entry_becomes_ready(tmp_path: Path) -> None:
    """No ``port`` in the entry: an ephemeral port is resolved before spawn."""
    script = _write_script(tmp_path, "sleeper.py", _SLEEP)
    servers = await start_server_processes(
        tmp_path, [{"cmd": sys.executable, "args": [script, "60"]}]
    )
    server = servers[0]
    try:
        assert server.ready is True
        assert server.spec.port == 0  # entry asked for auto ...
        assert server.port > 0  # ... and the client resolved a real port
        assert server.address == "127.0.0.1"
        assert server.network == "tcp"
    finally:
        await stop_server_processes(servers)


# ── e2e: preferred-port conflict increments, child really binds ──────────


async def test_port_conflict_increments_and_child_binds(tmp_path: Path) -> None:
    """The client probes the preferred port, skips the occupied one, and the
    child binds the substituted (incremented) port."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    blocked = int(listener.getsockname()[1])
    script = _write_script(tmp_path, "binder.py", _BIND_THEN_SLEEP)
    flag = tmp_path / "bound.flag"
    servers = None
    try:
        entry = {
            "cmd": sys.executable,
            "args": [script, "<address>", "<port>", str(flag), "120"],
            "port": blocked,  # preferred -> in use -> must increment
        }
        servers = await start_server_processes(tmp_path, [entry])
        server = servers[0]
        assert server.ready is True
        assert server.error is None
        assert blocked < server.port <= blocked + MAX_PORT_ATTEMPTS
        # the listener still owns the preferred port
        assert _is_port_free("127.0.0.1", blocked) is False
        # the child actually bound the substituted port (flag holds its value)
        bound = int(await _wait_for_file(flag))
        assert bound == server.port
    finally:
        if servers is not None:
            await stop_server_processes(servers)
        listener.close()


# ── static mode: spawn + stop terminates the process ─────────────────────


async def test_static_start_ready_and_stop_terminates(tmp_path: Path) -> None:
    script = _write_script(tmp_path, "sleeper.py", _SLEEP)
    preferred = _free_preferred_port()
    servers = await start_server_processes(
        tmp_path,
        [{"cmd": sys.executable, "args": [script, "60"], "port": preferred}],
    )
    assert len(servers) == 1
    server = servers[0]
    assert server.ready is True
    # a free preferred port is kept as-is (else the next free one is used)
    assert preferred <= server.port <= preferred + MAX_PORT_ATTEMPTS
    assert server.process is not None
    assert server.process.returncode is None  # still running
    await stop_server_processes([server])
    assert server.process.returncode is not None  # terminated


async def test_stop_server_processes_sync_terminates(tmp_path: Path) -> None:
    script = _write_script(tmp_path, "sleeper.py", _SLEEP)
    servers = await start_server_processes(
        tmp_path,
        [{"cmd": sys.executable, "args": [script, "60"], "port": _free_preferred_port()}],
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


def _install_hanging_start(
    monkeypatch: pytest.MonkeyPatch, child_spawned: asyncio.Event
) -> None:
    """Let the real ``start()`` spawn the child, then hang inside ``start()``.

    The old config mode provided a long await *after* the spawn (the config
    poll) which the cancellation tests used as their window; the new
    placeholder flow completes ``start()`` right after spawn, so the test
    wraps it to re-create that window.
    """
    # bind the *real* start before the monkeypatch replaces it
    real_start = ServerProcess.start

    async def _start_then_hang(self: ServerProcess) -> None:
        await real_start(self)
        child_spawned.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(ServerProcess, "start", _start_then_hang)


async def test_start_cancellation_terminates_spawned_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child_spawned = asyncio.Event()
    _install_hanging_start(monkeypatch, child_spawned)
    script = _write_script(tmp_path, "sleeper.py", _SLEEP)
    out: list[ServerProcess] = []
    task = asyncio.create_task(
        start_server_processes(
            tmp_path,
            [{"cmd": sys.executable, "args": [script, "120"], "port": 40009}],
            out=out,
        )
    )
    # wait until the child exists and start() is hung
    await asyncio.wait_for(child_spawned.wait(), 20)
    assert len(out) == 1
    proc = out[0].process
    assert proc is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # the live list still holds the server, and its child was terminated
    assert len(out) == 1
    assert out[0].process is proc
    rc = await asyncio.wait_for(proc.wait(), 15)
    assert rc is not None


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
    """Cancel mid-start: registered before start() completes, removed by cleanup."""
    child_spawned = asyncio.Event()
    _install_hanging_start(monkeypatch, child_spawned)
    script = _write_script(tmp_path, "sleeper.py", _SLEEP)
    out: list[ServerProcess] = []
    task = asyncio.create_task(
        start_server_processes(
            tmp_path,
            [{"cmd": sys.executable, "args": [script, "120"], "port": 40014}],
            out=out,
        )
    )
    await asyncio.wait_for(child_spawned.wait(), 20)
    server = out[0]
    # registration happens *before* start() completes: live while still hung
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



# ── command rendering + spawn log line ────────────────────────────────────


def test_render_command_plain_args() -> None:
    assert server_process._render_command("python", ["-m", "mod", "--port", "1234"]) == (
        "python -m mod --port 1234"
    )


def test_render_command_quotes_args_with_whitespace() -> None:
    assert server_process._render_command("run", ["a b", "c"]) == 'run "a b" c'
    assert server_process._render_command("run", [""]) == 'run ""'


def test_render_command_escapes_quotes_and_backslashes() -> None:
    assert server_process._render_command("run", ['say "hi"', "C:\\x y"]) == (
        'run "say \\"hi\\"" "C:\\\\x y"'
    )


async def test_start_logs_resolved_command_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A spawned server logs its fully-resolved command as one string at INFO."""
    script = _write_script(tmp_path, "argv.py", _WRITE_ARGV_THEN_SLEEP)
    argv_out = tmp_path / "argv.txt"
    with caplog.at_level(logging.INFO, logger="kimi_agent_sdk._server_process"):
        servers = await start_server_processes(
            tmp_path,
            [
                {
                    "cmd": sys.executable,
                    "args": [
                        script,
                        str(argv_out),
                        "5",
                        "--host",
                        "<address>",
                        "--port=<port>",
                    ],
                    "port": _free_preferred_port(),
                }
            ],
            session_id="log-test",
        )
    try:
        assert len(servers) == 1 and servers[0].ready
        port = servers[0].port
        expected = (
            f"server: {sys.executable} {script} {argv_out} 5 "
            f"--host 127.0.0.1 --port={port}"
        )
        assert expected in caplog.text
        # placeholders are gone from the logged line
        assert "<address>" not in caplog.text
        assert "<port>" not in caplog.text
        assert "<session_id>" not in caplog.text
    finally:
        await stop_server_processes(servers)


# ── cross-loop teardown (Ctrl+C: close runs asyncio.run in a fresh loop) ──


def test_stop_reaps_child_spawned_on_a_different_loop(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Reproduces the Ctrl+C failure: the child was spawned on loop A (now
    closed); stop_server_processes runs on loop B via asyncio.run.  Awaiting
    proc.wait() on loop B raises RuntimeError('attached to a different loop');
    the stop path must fall back to a loop-independent reap instead of logging
    'failed to reap process'.
    """
    import os
    import time as _time

    script = _write_script(tmp_path, "sleeper.py", _SLEEP)

    async def spawn():
        return await start_server_processes(
            tmp_path, [{"cmd": sys.executable, "args": [script, "60"]}]
        )

    servers = asyncio.run(spawn())  # loop A: spawns the child, then closes
    proc = servers[0].process
    assert proc is not None and proc.returncode is None
    pid = proc.pid

    with caplog.at_level(logging.DEBUG, logger="kimi_agent_sdk._server_process"):
        asyncio.run(stop_server_processes(servers))  # loop B

    assert "failed to reap process" not in caplog.text
    assert get_live_server_processes() == []
    # On POSIX the pid is fully reaped (os.kill raises ChildProcessError).  On
    # Windows terminate()==TerminateProcess is immediate, but a terminated
    # process handle stays openable until the (dead) spawn loop releases it, so
    # os.kill(pid, 0) is not a reliable liveness probe there; the absence of a
    # 'failed to reap' log + a drained registry is the contract we assert.
    if sys.platform != "win32":
        with pytest.raises(ChildProcessError):
            os.kill(pid, 0)
    _ = (os, _time, pid)  # keep names referenced for the win32 branch above


def test_reap_without_loop_windows_short_circuit(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows, terminate() == TerminateProcess; the fallback reaper
    reports success without polling the (dead) spawn loop's transport."""

    class _Proc:
        returncode = None
        pid = 12345

        def kill(self) -> None:  # pragma: no cover - must not be reached
            raise AssertionError("kill() should not be needed on Windows")

    monkeypatch.setattr(server_process.os, "name", "nt", raising=False)
    assert server_process._reap_without_loop(_Proc(), 0.1) is True


def test_reap_without_loop_posix_waits_for_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POSIX path: polls waitpid(WNOHANG) until the child is reaped."""

    class _Proc:
        returncode = None
        pid = 4242

        def kill(self) -> None:  # pragma: no cover - child exits in time
            raise AssertionError("kill() should not be needed here")

    calls = {"n": 0}

    def fake_waitpid(pid: int, flags: int) -> tuple[int, int]:
        calls["n"] += 1
        if calls["n"] < 3:
            return (0, 0)  # not exited yet
        return (pid, 0)

    monkeypatch.setattr(server_process.os, "name", "posix", raising=False)
    monkeypatch.setattr(server_process.os, "WNOHANG", 1, raising=False)
    monkeypatch.setattr(server_process.os, "waitpid", fake_waitpid)
    assert server_process._reap_without_loop(_Proc(), 5.0) is True
    assert calls["n"] == 3


def test_reap_without_loop_posix_kills_stubborn_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POSIX path: child still alive after the grace period -> kill + blocking reap."""

    killed = {"called": False}

    class _Proc:
        returncode = None
        pid = 4243

        def kill(self) -> None:
            killed["called"] = True

    def fake_waitpid(pid: int, flags: int) -> tuple[int, int]:
        if flags & server_process.os.WNOHANG:
            return (0, 0)  # never exits on its own
        return (pid, 0)  # reaped after kill()

    monkeypatch.setattr(server_process.os, "name", "posix", raising=False)
    monkeypatch.setattr(server_process.os, "WNOHANG", 1, raising=False)
    monkeypatch.setattr(server_process.os, "waitpid", fake_waitpid)
    assert server_process._reap_without_loop(_Proc(), 0.1) is True
    assert killed["called"] is True
