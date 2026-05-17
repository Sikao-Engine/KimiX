"""Shared Params model for bash tools."""
import os
import sys
from pathlib import Path

from pydantic import BaseModel, Field


class Params(BaseModel):
    path: str = Field(description="Executable path.")
    args: list[str] = Field(default_factory=list, description="Command arguments.")
    timeout: int = Field(default=10, description="Timeout in seconds.")
    cwd: str | None = Field(default=None, description="Working directory (default: current directory).")
    output_path: str | None = Field(default=None, description="Output file path (optional).")


_UNIX_PROTECTED_PATHS = frozenset({
    "/", "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64",
    "/proc", "/sbin", "/sys", "/usr", "/usr/bin", "/usr/lib",
    "/usr/lib64", "/usr/sbin", "/usr/local", "/var", "/home",
    "/root", "/tmp", "/run", "/mnt", "/media", "/opt",
})

_WINDOWS_PROTECTED_PATHS = frozenset({
    "/Windows", "/Program Files", "/Program Files (x86)",
    "/ProgramData", "/System32", "/SysWOW64",
})

if sys.platform == "win32":
    _PROTECTED_PATHS = _WINDOWS_PROTECTED_PATHS
else:
    _PROTECTED_PATHS = _UNIX_PROTECTED_PATHS


def _resolve_path(path: str, cwd: str | None = None) -> Path:
    """Resolve a path relative to cwd."""
    if os.path.isabs(path):
        return Path(path).resolve()
    return (Path(cwd or os.getcwd()) / path).resolve()


def _is_protected_path(path: str, cwd: str | None = None) -> tuple[bool, str]:
    """Check if a path is protected. Returns (is_protected, reason)."""
    try:
        resolved = _resolve_path(path, cwd)
    except (OSError, ValueError):
        return False, ""

    for protected in _PROTECTED_PATHS:
        try:
            prot_resolved = Path(protected).resolve()
            if resolved == prot_resolved:
                return True, f"'{path}' is a system-critical path"
            try:
                resolved.relative_to(prot_resolved)
                return True, f"'{path}' is inside system-critical path '{protected}'"
            except ValueError:
                pass
        except (OSError, ValueError):
            pass

    return False, ""


_PROTECTED_PIDS = frozenset(range(1, 20))

_PROTECTED_PROCESS_NAMES = frozenset({
    "init", "systemd", "sshd", "ssh", "cron", "crond",
    "kernel", "kthreadd", "kworker", "ksoftirqd",
    "explorer.exe", "csrss.exe", "smss.exe", "services.exe",
    "lsass.exe", "svchost.exe", "winlogon.exe", "wininit.exe",
    "System", "Registry", "MemCompression",
})


def _is_protected_pid(pid: int) -> tuple[bool, str]:
    """Check if a PID is protected. Returns (is_protected, reason)."""
    if pid <= 0:
        return True, f"PID {pid} is invalid"
    if pid in _PROTECTED_PIDS:
        return True, f"PID {pid} is a critical system process"
    if pid == os.getpid():
        return True, f"PID {pid} is the current process"
    return False, ""


def _is_protected_process_name(name: str) -> tuple[bool, str]:
    """Check if a process name is protected. Returns (is_protected, reason)."""
    lower_name = name.lower()
    for protected in _PROTECTED_PROCESS_NAMES:
        if lower_name == protected.lower():
            return True, f"'{name}' is a critical system process"
    return False, ""
