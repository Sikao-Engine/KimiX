from __future__ import annotations

import os
import queue
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import orjson

_MAX_MESSAGE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class ServerEndpoint:
    pid: int
    port: int
    token: str
    path: Path


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _windows_process_details(pid: int) -> tuple[str, str, Path | None] | None:
    script = (
        f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}'; "
        "if ($null -eq $p) { exit 1 }; "
        "$v = @{ ExecutablePath = $p.ExecutablePath; CommandLine = $p.CommandLine }; "
        "[Console]::Out.Write((ConvertTo-Json -Compress -InputObject $v))"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=3,
            check=False,
        )
        if result.returncode != 0:
            return None
        details = orjson.loads(result.stdout)
        executable = str(details.get("ExecutablePath") or "")
        command = str(details.get("CommandLine") or "")
        return executable, command, None
    except (OSError, subprocess.SubprocessError, orjson.JSONDecodeError):
        return None


def _posix_process_details(pid: int) -> tuple[str, str, Path | None] | None:
    proc_dir = Path(f"/proc/{pid}")
    try:
        if proc_dir.is_dir():
            command = (proc_dir / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                errors="replace"
            )
            try:
                executable = os.readlink(proc_dir / "exe")
            except OSError:
                executable = command.split(maxsplit=1)[0] if command else ""
            try:
                cwd = Path(os.readlink(proc_dir / "cwd"))
            except OSError:
                cwd = None
            return executable, command, cwd

        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm=,args="],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if result.returncode != 0:
            return None
        command = result.stdout.strip()
        executable = command.split(maxsplit=1)[0] if command else ""
        return executable, command, None
    except (OSError, subprocess.SubprocessError):
        return None


def _process_details(pid: int) -> tuple[str, str, Path | None] | None:
    """Return executable, command line, and working directory for *pid*."""
    return (
        _windows_process_details(pid)
        if sys.platform == "win32"
        else _posix_process_details(pid)
    )


def _is_project_python_process(pid: int, project_dir: Path) -> bool:
    if not _pid_alive(pid):
        return False
    details = _process_details(pid)
    if details is None:
        return False
    executable, command, process_cwd = details
    executable_name = Path(executable).name.casefold()
    if not (executable_name == "python" or executable_name.startswith("python")):
        return False
    if "kimix" not in command.casefold():
        return False
    if process_cwd is not None:
        try:
            if process_cwd.resolve() != project_dir.resolve():
                return False
        except OSError:
            return False
    return True


def _read_endpoint(path: Path, project_dir: Path) -> ServerEndpoint | None:
    try:
        if not path.name.startswith("pid_"):
            return None
        pid = int(path.name[4:])
        data = orjson.loads(path.read_bytes())
        if data.get("pid") != pid:
            return None
        if Path(str(data["cwd"])).resolve() != project_dir.resolve():
            return None
        if not _is_project_python_process(pid, project_dir):
            return None
        port = int(data["port"])
        token = str(data["token"])
        if not 1 <= port <= 65535 or not token:
            return None
        return ServerEndpoint(pid=pid, port=port, token=token, path=path)
    except (KeyError, TypeError, ValueError, OSError, orjson.JSONDecodeError):
        return None


def discover_servers(
    cache_dir: Path,
    project_dir: Path,
    *,
    requested_pid: int | None = None,
    exclude_pid: int | None = None,
) -> list[ServerEndpoint]:
    """Find valid project servers and remove stale registration files."""
    if requested_pid is not None:
        paths = [cache_dir / f"pid_{requested_pid}"]
    else:
        try:
            paths = sorted(cache_dir.glob("pid_*"), key=lambda path: path.name)
        except OSError:
            return []

    endpoints: list[ServerEndpoint] = []
    for path in paths:
        try:
            pid = int(path.name[4:])
        except ValueError:
            continue
        if pid == exclude_pid:
            continue
        endpoint = _read_endpoint(path, project_dir)
        if endpoint is not None:
            endpoints.append(endpoint)
            continue
        # A registration is only useful while its process and endpoint are valid.
        # Failure to remove a stale file must not prevent trying the other files.
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    return endpoints


def send_messages(endpoint: ServerEndpoint, messages: Iterable[str]) -> None:
    """Send one authenticated list of input strings to a CLI server."""
    payload = orjson.dumps(
        {"token": endpoint.token, "messages": list(messages)},
    )
    if len(payload) > _MAX_MESSAGE_BYTES:
        raise ValueError("IPC input exceeds the 16 MiB message limit")
    with socket.create_connection(("127.0.0.1", endpoint.port), timeout=3) as connection:
        connection.settimeout(3)
        connection.sendall(payload)
        connection.shutdown(socket.SHUT_WR)
        if connection.recv(16) != b"ok\n":
            raise ConnectionError(f"CLI server {endpoint.pid} did not accept the input")


class CLIIPCServer:
    """Loopback IPC listener which multiplexes remote and terminal input."""

    def __init__(self, cache_dir: Path, project_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.project_dir = project_dir
        self.pid = os.getpid()
        self.path = cache_dir / f"pid_{self.pid}"
        self.port = 0
        self._token = ""
        self._listener: socket.socket | None = None
        self._inputs: queue.Queue[str | BaseException] = queue.Queue()
        self._closed = threading.Event()
        self._local_input_started = False
        self._local_input_lock = threading.Lock()

    def start(self) -> None:
        import secrets

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(0.2)
        self._listener = listener
        self.port = int(listener.getsockname()[1])
        self._token = secrets.token_urlsafe(32)
        registration = {
            "pid": self.pid,
            "port": self.port,
            "token": self._token,
            "cwd": str(self.project_dir.resolve()),
        }
        temporary_path = self.cache_dir / f"pid_{self.pid}.tmp"
        temporary_path.write_bytes(orjson.dumps(registration))
        temporary_path.replace(self.path)
        threading.Thread(target=self._accept_loop, name="kimix-ipc", daemon=True).start()

    def close(self) -> None:
        self._closed.set()
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass

    def __enter__(self) -> CLIIPCServer:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _accept_loop(self) -> None:
        while not self._closed.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(
                target=self._receive,
                args=(connection,),
                name="kimix-ipc-client",
                daemon=True,
            ).start()

    def _receive(self, connection: socket.socket) -> None:
        with connection:
            connection.settimeout(5)
            chunks: list[bytes] = []
            size = 0
            try:
                while True:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > _MAX_MESSAGE_BYTES:
                        raise ValueError("IPC input exceeds the 16 MiB message limit")
                    chunks.append(chunk)
                request = orjson.loads(b"".join(chunks))
                if request.get("token") != self._token:
                    raise ValueError("Invalid IPC token")
                messages = request.get("messages")
                if not isinstance(messages, list) or not all(
                    isinstance(message, str) for message in messages
                ):
                    raise ValueError("IPC messages must be a list of strings")
                for message in messages:
                    for sentence in message.splitlines():
                        self._inputs.put(sentence)
                connection.sendall(b"ok\n")
            except (OSError, ValueError, TypeError, orjson.JSONDecodeError):
                # Malformed or disconnected clients must not stop the CLI server.
                return

    def _read_local_input(self) -> None:
        while not self._closed.is_set():
            try:
                self._inputs.put(input())
            except BaseException as error:
                self._inputs.put(error)
                return

    def read_input(self) -> str:
        with self._local_input_lock:
            if not self._local_input_started:
                self._local_input_started = True
                threading.Thread(
                    target=self._read_local_input,
                    name="kimix-local-input",
                    daemon=True,
                ).start()
        value = self._inputs.get()
        if isinstance(value, BaseException):
            raise value
        return value
