from __future__ import annotations

import ast
import io
import queue
import subprocess
import sys
import threading
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


class _FakeTTYStdin(io.StringIO):
    """Stdin stub that reports itself as an interactive terminal."""

    def isatty(self) -> bool:
        return True


def test_connect_cli_prints_banner_only_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    endpoint = ipc.ServerEndpoint(123, 4099, "token", tmp_path / "pid_123")
    monkeypatch.setattr(core, "discover_servers", lambda *_, **__: [endpoint])
    monkeypatch.setattr(core.constants, "curr_dir", tmp_path)
    monkeypatch.setattr(sys, "stdin", _FakeTTYStdin(""))
    sent: list[list[str]] = []
    monkeypatch.setattr(core, "send_messages", lambda target, messages: sent.append(messages))
    answers = iter(["first", "second", "third"])
    prompts: list[str] = []

    def fake_input(prompt: str = "") -> str:
        prompts.append(prompt)
        try:
            return next(answers)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", fake_input)
    debug_messages: list[str] = []
    monkeypatch.setattr(core, "print_debug", debug_messages.append)

    core._connect_cli("")

    out = capsys.readouterr().out
    assert out.count("Message for CLI server (Ctrl+C to quit)") == 1
    # The hint is a one-time banner; the per-line reads stay silent
    # (three messages plus the final read that raises EOFError).
    assert prompts == ["", "", "", ""]
    assert sent == [["first"], ["second"], ["third"]]


def test_connect_cli_debug_message_mentions_target_pid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    endpoint = ipc.ServerEndpoint(4321, 4099, "token", tmp_path / "pid_4321")
    monkeypatch.setattr(core, "discover_servers", lambda *_, **__: [endpoint])
    monkeypatch.setattr(core.constants, "curr_dir", tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO("hello\n"))
    monkeypatch.setattr(core, "send_messages", lambda *_, **__: None)
    debug_messages: list[str] = []
    monkeypatch.setattr(core, "print_debug", debug_messages.append)

    core._connect_cli("")

    assert len(debug_messages) == 1
    assert "4321" in debug_messages[0]
    assert "4099" not in debug_messages[0]


def test_connect_cli_debug_message_lists_multiple_servers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = ipc.ServerEndpoint(111, 4099, "token-a", tmp_path / "pid_111")
    second = ipc.ServerEndpoint(222, 4100, "token-b", tmp_path / "pid_222")
    monkeypatch.setattr(core, "discover_servers", lambda *_, **__: [first, second])
    monkeypatch.setattr(core.constants, "curr_dir", tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO("hello\n"))
    sent: list[ipc.ServerEndpoint] = []
    monkeypatch.setattr(core, "send_messages", lambda target, _msgs: sent.append(target))
    debug_messages: list[str] = []
    monkeypatch.setattr(core, "print_debug", debug_messages.append)

    core._connect_cli("")

    assert sent == [first]
    assert len(debug_messages) == 1
    assert "111" in debug_messages[0]
    assert "222" in debug_messages[0]
    assert "using PID 111" in debug_messages[0]


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


def _make_server(tmp_path: Path) -> ipc.CLIIPCServer:
    return ipc.CLIIPCServer(tmp_path / ".kimix_cache", tmp_path)


def test_local_input_ignored_while_busy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _make_server(tmp_path)
    calls: list[str] = []

    def fake_input() -> str:
        calls.append("called")
        return "typed while busy"

    monkeypatch.setattr("builtins.input", fake_input)
    server.set_busy(True)
    # The loop sleeps while busy, so close it from a timer to unblock it.
    timer = threading.Timer(0.1, server._closed.set)
    timer.start()
    try:
        server._read_local_input()
    finally:
        timer.cancel()

    assert calls == []
    assert server._inputs.empty()


def test_local_input_resumes_after_busy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _make_server(tmp_path)
    calls: list[str] = []

    def fake_input() -> str:
        calls.append("called")
        server._closed.set()  # stop the loop after this read
        return "typed while idle"

    monkeypatch.setattr("builtins.input", fake_input)
    server.set_busy(True)
    server.set_busy(False)
    server._read_local_input()

    assert calls == ["called"]
    assert server._inputs.get_nowait() == "typed while idle"


def test_local_input_drops_line_completed_while_busy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _make_server(tmp_path)

    def fake_input() -> str:
        # The window becomes busy while the blocking read is in flight.
        server.set_busy(True)
        return "completed during busy"

    monkeypatch.setattr("builtins.input", fake_input)
    # The loop sleeps while busy, so close it from a timer to unblock it.
    timer = threading.Timer(0.1, server._closed.set)
    timer.start()
    try:
        server._read_local_input()
    finally:
        timer.cancel()

    assert server._inputs.empty()


def test_ipc_input_still_accepted_while_busy(tmp_path: Path) -> None:
    with ipc.CLIIPCServer(tmp_path / ".kimix_cache", tmp_path) as server:
        registration = orjson.loads(server.path.read_bytes())
        endpoint = ipc.ServerEndpoint(
            pid=server.pid,
            port=server.port,
            token=registration["token"],
            path=server.path,
        )
        server.set_busy(True)
        try:
            ipc.send_messages(endpoint, ["remote while busy"])
            assert server.read_input() == "remote while busy"
        finally:
            server.set_busy(False)


def test_set_busy_disables_echo_and_flushes_pending_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Busy windows must swallow keystrokes completely: no echo, no buffer."""
    server = _make_server(tmp_path)
    calls: list[tuple[str, bool]] = []

    monkeypatch.setattr(
        ipc, "_set_stdin_echo", lambda enabled: calls.append(("echo", enabled))
    )
    monkeypatch.setattr(ipc, "_flush_stdin", lambda: calls.append(("flush", True)))

    server.set_busy(True)
    server.set_busy(False)

    # Entering busy: echo off first, then discard pending input.
    # Leaving busy: discard stale input first, then echo back on.
    assert calls == [("echo", False), ("flush", True), ("flush", True), ("echo", True)]


def test_set_busy_nested_keeps_window_busy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Overlapping busy regions must not restore echo mid-run."""
    server = _make_server(tmp_path)
    calls: list[tuple[str, bool]] = []

    monkeypatch.setattr(
        ipc, "_set_stdin_echo", lambda enabled: calls.append(("echo", enabled))
    )
    monkeypatch.setattr(ipc, "_flush_stdin", lambda: calls.append(("flush", True)))

    server.set_busy(True)
    server.set_busy(True)
    server.set_busy(False)
    assert server._busy.is_set()
    assert calls == [("echo", False), ("flush", True)]
    server.set_busy(False)
    assert not server._busy.is_set()
    assert calls[-2:] == [("flush", True), ("echo", True)]


def test_close_restores_terminal_echo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exiting while busy must not leave the user's terminal without echo."""
    server = _make_server(tmp_path)
    calls: list[tuple[str, bool]] = []

    monkeypatch.setattr(
        ipc, "_set_stdin_echo", lambda enabled: calls.append(("echo", enabled))
    )
    monkeypatch.setattr(ipc, "_flush_stdin", lambda: calls.append(("flush", True)))

    server.set_busy(True)
    server.close()

    assert ("echo", True) in calls


def test_busy_property_reflects_window_state(tmp_path: Path) -> None:
    """Confirmation prompts ask the server whether the window is idle."""
    server = _make_server(tmp_path)
    assert server.busy is False
    server.set_busy(True)
    assert server.busy is True
    server.set_busy(False)
    assert server.busy is False


def test_reader_slot_is_freed_before_the_error_is_delivered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The dead reader must free its slot *before* the waiter can wake up.

    Otherwise the next ``read_input`` still sees ``_local_input_started`` set,
    starts no reader at all and blocks forever on a queue nobody fills — which
    is exactly what a Ctrl+C at the ``/plan`` confirmation would do.
    """
    server = _make_server(tmp_path)
    state_when_queued: list[bool] = []

    class _ProbeQueue(queue.Queue):
        def put(self, item: object, *args: object, **kwargs: object) -> None:
            state_when_queued.append(server._local_input_started)
            super().put(item, *args, **kwargs)  # type: ignore[arg-type]

    probe: queue.Queue = _ProbeQueue()
    server._inputs = probe  # type: ignore[assignment]
    server._local_input_started = True

    def failing_input() -> str:
        raise EOFError("stdin closed")

    monkeypatch.setattr("builtins.input", failing_input)
    server._read_local_input()

    assert state_when_queued == [False]
    queued = probe.get_nowait()
    assert isinstance(queued, EOFError)


def test_read_input_starts_a_fresh_reader_after_a_failed_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One EOF/Ctrl+C must not make the CLI permanently unreadable."""
    server = _make_server(tmp_path)
    answers = [EOFError("stdin closed"), "typed again"]

    def fake_input() -> str:
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr("builtins.input", fake_input)

    with pytest.raises(EOFError):
        server.read_input()
    assert server.read_input() == "typed again"


class _BusyServer:
    """Stand-in for ``CLIIPCServer`` that records busy transitions."""

    def __init__(self, answer: str = "answer", *, busy: bool = False) -> None:
        self.answer = answer
        self.busy = busy
        self.busy_calls: list[bool] = []
        self.reads = 0

    def read_input(self) -> str:
        self.reads += 1
        return self.answer

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.busy_calls.append(busy)


def test_input_returns_queued_lines_without_touching_the_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Queued command text is consumed first (multi-line commands, scripts)."""
    server = _BusyServer()
    monkeypatch.setattr("builtins.input", lambda *a, **k: pytest.fail("read the terminal"))
    queued = ["first", "second"]

    assert utils._input("", queued, server) == "first"
    assert utils._input("", queued, server) == "second"
    assert queued == []
    assert server.reads == 0
    assert capsys.readouterr().out == ""


def test_input_lifts_and_restores_a_busy_window(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A busy window pauses its reader: asking a question must un-pause it."""
    server = _BusyServer(busy=True)

    assert utils._input("ask? ", [], server) == "answer"

    assert server.busy_calls == [False, True]
    assert server.busy is True
    assert capsys.readouterr().out == "ask? "


def test_input_leaves_an_idle_window_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _BusyServer()

    assert utils._input("", [], server) == "answer"
    assert server.busy_calls == []


def test_input_falls_back_to_the_terminal_without_a_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Embedded use (no IPC server) still reads the plain terminal."""
    monkeypatch.setattr(utils, "_active_ipc_server", None)
    monkeypatch.setattr("builtins.input", lambda: "typed")

    assert utils._input() == "typed"


def test_only_the_input_funnel_reads_the_keyboard() -> None:
    """Every CLI prompt must go through ``kimix.cli_impl.utils._input``.

    One stdin consumer is a hard invariant: the CLI owns the terminal with its
    ``kimix-local-input`` reader thread, so any other ``input()`` call silently
    loses the typed line — its question stays on screen and the flow hangs
    (exactly what happened to the ``/plan`` review questions).
    """
    allowed = {
        "src/kimix/cli_impl/ipc.py",  # the terminal reader thread itself
        "src/kimix/cli_impl/utils.py",  # _input's no-server fallback
    }
    repo_root = Path(__file__).resolve().parents[1]
    src_root = repo_root / "src" / "kimix"
    offenders: list[str] = []
    for path in sorted(src_root.rglob("*.py")):
        relative = path.relative_to(repo_root).as_posix()
        if relative in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "input"
            ):
                offenders.append(f"{relative}:{node.lineno}")
    assert offenders == [], f"bare input() outside the CLI input funnel: {offenders}"
