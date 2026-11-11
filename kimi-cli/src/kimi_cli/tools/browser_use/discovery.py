"""Chromium-family executable discovery for :mod:`kimi_cli.tools.browser_use`.

Every rule here is a pure function with injectable hooks (``environ``, ``is_file``,
``read_app_path``, ``which``, ``home``) so the Windows registry and ``Program
Files`` matrix can be covered by unit tests on any host platform.  Nothing in this
module starts a process, imports Playwright, or touches the network.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import shutil
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

__all__ = ["DiscoveredBrowser", "clean_executable_path", "discover_browsers", "registry_app_path"]

#: Browser flavours in launch-preference order: stable channels first, then the
#: side-by-side channels that are still Chromium/CDP compatible.  Each entry is
#: ``(display name, path components under an install root)``.
WINDOWS_FLAVOURS: tuple[tuple[str, ...], ...] = (
    ("Chrome", "Google", "Chrome", "Application", "chrome.exe"),
    ("Edge", "Microsoft", "Edge", "Application", "msedge.exe"),
    ("Chrome Beta", "Google", "Chrome Beta", "Application", "chrome.exe"),
    ("Chrome Dev", "Google", "Chrome Dev", "Application", "chrome.exe"),
    # Chrome Canary records itself as "Chrome SxS" and installs per user.
    ("Chrome Canary", "Google", "Chrome SxS", "Application", "chrome.exe"),
    ("Edge Beta", "Microsoft", "Edge Beta", "Application", "msedge.exe"),
    ("Brave", "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
    ("Chromium", "Chromium", "Application", "chrome.exe"),
)

#: ``%ProgramW6432%`` is the 64-bit view for 32-bit processes because WOW64
#: redirects ``%ProgramFiles%`` to ``Program Files (x86)``.  ``%LOCALAPPDATA%``
#: covers per-user installs (including Chrome Canary).
WINDOWS_INSTALL_ROOTS: tuple[str, ...] = (
    "PROGRAMFILES",
    "PROGRAMW6432",
    "PROGRAMFILES(X86)",
    "LOCALAPPDATA",
)

#: ``App Paths`` registrations to consult, in preference order.  This is the only
#: source that survives a non-default install directory.
WINDOWS_APP_PATH_EXECUTABLES: tuple[str, ...] = ("chrome.exe", "msedge.exe", "brave.exe")

WINDOWS_PATH_EXECUTABLES: tuple[str, ...] = (*WINDOWS_APP_PATH_EXECUTABLES, "chromium.exe")

MACOS_FLAVOURS: tuple[tuple[str, str], ...] = (
    ("Chrome", "Google Chrome.app/Contents/MacOS/Google Chrome"),
    ("Edge", "Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
    ("Chrome Beta", "Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta"),
    ("Chrome Canary", "Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary"),
    ("Brave", "Brave Browser.app/Contents/MacOS/Brave Browser"),
    ("Chromium", "Chromium.app/Contents/MacOS/Chromium"),
)

LINUX_PATH_EXECUTABLES: tuple[str, ...] = (
    "google-chrome-stable",
    "google-chrome",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
    "microsoft-edge-stable",
    "brave-browser",
)

LINUX_FLAVOURS: tuple[tuple[str, str], ...] = (
    ("Chrome", "/opt/google/chrome/chrome"),
    ("Chrome", "/usr/bin/google-chrome-stable"),
    ("Chrome", "/usr/bin/google-chrome"),
    ("Chromium", "/usr/bin/chromium"),
    ("Chromium", "/usr/bin/chromium-browser"),
    ("Chromium", "/snap/bin/chromium"),
    ("Edge", "/usr/bin/microsoft-edge"),
    ("Edge", "/usr/bin/microsoft-edge-stable"),
    ("Brave", "/usr/bin/brave-browser"),
)

#: Display names for executables found on ``PATH`` and through ``App Paths``.
EXECUTABLE_NAMES: dict[str, str] = {
    "chrome.exe": "Chrome",
    "msedge.exe": "Edge",
    "brave.exe": "Brave",
    "chromium.exe": "Chromium",
    "google-chrome-stable": "Chrome",
    "google-chrome": "Chrome",
    "chromium": "Chromium",
    "chromium-browser": "Chromium",
    "microsoft-edge": "Edge",
    "microsoft-edge-stable": "Edge",
    "brave-browser": "Brave",
}

_APP_PATHS_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"

#: ``sys.platform`` values that expose a POSIX-style browser layout.
_POSIX_PLATFORMS: tuple[str, ...] = ("linux", "freebsd", "openbsd", "netbsd", "sunos", "aix")


@dataclass(frozen=True, slots=True)
class DiscoveredBrowser:
    """One detected Chromium-family executable and where it was found."""

    executable: str
    source: str
    name: str = ""

    @property
    def label(self) -> str:
        """Short human label used in tool output and logs."""
        display = self.name or executable_name(self.executable)
        return f"{display} ({self.source})"


def executable_name(executable: str) -> str:
    """Best-effort display name for an executable path."""
    base = executable.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return EXECUTABLE_NAMES.get(base, base)


def registry_app_path(executable: str, winreg_module: object | None = None) -> str | None:
    """Resolve ``App Paths`` for *executable*.

    The registry is the only source that keeps working when Chrome is installed
    outside ``Program Files`` (a second drive, an enterprise image, a portable copy
    pinned by policy).  ``HKCU`` is consulted first so a per-user install is never
    shadowed by a machine-wide one, and the default value is read because that is
    where Chrome, Edge, and Brave record their executable.
    """
    module = winreg_module
    if module is None:
        if sys.platform != "win32":
            return None
        import winreg

        module = winreg
    open_key = getattr(module, "OpenKey", None)
    query_value = getattr(module, "QueryValueEx", None)
    key_read = getattr(module, "KEY_READ", 0)
    if open_key is None or query_value is None:  # pragma: no cover - defensive
        return None
    for hive_name in ("HKEY_CURRENT_USER", "HKEY_LOCAL_MACHINE"):
        hive = getattr(module, hive_name, None)
        if hive is None:  # pragma: no cover - defensive
            continue
        try:
            with open_key(hive, rf"{_APP_PATHS_KEY}\{executable}", 0, key_read) as key:
                value = query_value(key, "")
        except (FileNotFoundError, OSError):
            continue
        raw = value[0] if isinstance(value, tuple) else value
        cleaned = clean_executable_path(raw)
        if cleaned:
            return cleaned
    return None


def clean_executable_path(value: object) -> str | None:
    """Normalize a configured or registered path.

    Web deploy manifests and ``set VAR="C:\\Program Files"`` style environments
    routinely wrap paths in quotes or pad them with spaces; ``%SystemRoot%``-style
    ``REG_EXPAND_SZ`` values need expanding too.  A path that is still empty after
    normalization is treated as unset rather than as a candidate.
    """
    if not isinstance(value, str):
        return None
    cleaned = value.strip().strip('"').strip()
    if not cleaned:
        return None
    expanded = os.path.expandvars(cleaned).strip().strip('"').strip()
    return expanded or None


def discover_browsers(
    *,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    is_file: Callable[[str], bool] | None = None,
    read_app_path: Callable[[str], str | None] | None = None,
    which: Callable[[str], str | None] | None = None,
    home: str | None = None,
) -> list[DiscoveredBrowser]:
    """Return the Chromium-family executables that exist on this host.

    Candidates are ordered by preference (registry first, then well-known install
    roots, then ``PATH``) and deduplicated case-insensitively on Windows, because
    ``%ProgramFiles%`` and ``%ProgramW6432%`` usually resolve to one directory.
    """
    target = sys.platform if platform is None else platform
    env = os.environ if environ is None else environ
    exists = default_is_file if is_file is None else is_file
    lookup = registry_app_path if read_app_path is None else read_app_path
    find_on_path = shutil.which if which is None else which

    found: list[DiscoveredBrowser] = []
    case_insensitive = False
    if target == "win32":
        found = _windows_browsers(env, lookup, find_on_path)
        case_insensitive = True
    elif target == "darwin":
        found = _macos_browsers(os.path.expanduser("~") if home is None else home)
    elif target.startswith(_POSIX_PLATFORMS):
        found = _linux_browsers(find_on_path)
    return _filter_existing(found, exists, case_insensitive=case_insensitive)


def default_is_file(path: str) -> bool:
    try:
        return bool(path) and os.path.isfile(path)
    except (OSError, ValueError):  # pragma: no cover - hostile path values
        return False


def _env_value(environ: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive environment lookup.

    ``os.environ`` is case-insensitive on Windows, but tests (and any future POSIX
    caller) use plain mappings, so the lookup folds case explicitly.
    """
    raw = environ.get(name)
    if raw is None:
        wanted = name.lower()
        for key, value in environ.items():
            if key.lower() == wanted:
                raw = value
                break
    return clean_executable_path(raw)


def _windows_browsers(
    environ: Mapping[str, str],
    read_app_path: Callable[[str], str | None],
    which: Callable[[str], str | None],
) -> list[DiscoveredBrowser]:
    found: list[DiscoveredBrowser] = []
    for executable in WINDOWS_APP_PATH_EXECUTABLES:
        registered = read_app_path(executable)
        if registered:
            found.append(
                DiscoveredBrowser(registered, "registry", executable_name(executable))
            )
    for variable in WINDOWS_INSTALL_ROOTS:
        root = _env_value(environ, variable)
        if not root:
            continue
        for flavour in WINDOWS_FLAVOURS:
            name, parts = flavour[0], flavour[1:]
            found.append(DiscoveredBrowser(ntpath.join(root, *parts), f"%{variable}%", name))
    for executable in WINDOWS_PATH_EXECUTABLES:
        located = which(executable)
        if located:
            found.append(DiscoveredBrowser(located, "PATH", executable_name(executable)))
    return found


def _macos_browsers(home: str) -> list[DiscoveredBrowser]:
    found: list[DiscoveredBrowser] = []
    for root in ("/Applications", posixpath.join(home, "Applications")):
        for name, flavour in MACOS_FLAVOURS:
            found.append(DiscoveredBrowser(posixpath.join(root, flavour), root, name))
    return found


def _linux_browsers(which: Callable[[str], str | None]) -> list[DiscoveredBrowser]:
    found: list[DiscoveredBrowser] = []
    for executable in LINUX_PATH_EXECUTABLES:
        located = which(executable)
        if located:
            found.append(DiscoveredBrowser(located, "PATH", executable_name(executable)))
    found.extend(DiscoveredBrowser(path, "well-known path", name) for name, path in LINUX_FLAVOURS)
    return found


def _filter_existing(
    found: Iterable[DiscoveredBrowser],
    is_file: Callable[[str], bool],
    *,
    case_insensitive: bool,
) -> list[DiscoveredBrowser]:
    seen: set[str] = set()
    browsers: list[DiscoveredBrowser] = []
    for browser in found:
        key = ntpath.normcase(browser.executable) if case_insensitive else browser.executable
        if key in seen:
            continue
        seen.add(key)
        if is_file(browser.executable):
            browsers.append(browser)
    return browsers
