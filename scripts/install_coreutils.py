"""Install Microsoft Coreutils for Windows silently.

Strategy (in priority order):
1. WinGet (official Microsoft recommendation)
2. Direct download from GitHub latest release

Usage:
    python install_coreutils.py                          # default install
    python install_coreutils.py --dir "D:\\Coreutils"     # custom dir
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import urllib.parse
import winreg
from pathlib import Path

# ============================================================
# Global configuration
# ============================================================
_GITHUB_API_URL = "https://api.github.com/repos/microsoft/coreutils/releases/latest"
_GITHUB_RELEASES_URL = "https://github.com/microsoft/coreutils/releases/latest"


def _get_share_dir() -> Path:
    """Get the share directory path."""
    if share_dir := os.getenv("KIMI_SHARE_DIR"):
        share_dir = Path(share_dir)
    else:
        share_dir = Path.home() / ".kimi"
    share_dir.mkdir(parents=True, exist_ok=True)
    return share_dir


INSTALL_DIR = _get_share_dir() / "coreutils"
"""Default install directory for the portable extraction strategy."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _is_windows() -> bool:
    return sys.platform == "win32"


def _run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess[str]:
    """Run a subprocess and return the result (stdout/stderr captured as text)."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _get_github_token() -> str | None:
    """Return a GitHub token from the environment, or ``None``.

    Checks ``GITHUB_TOKEN`` then ``GH_TOKEN``.
    """
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


def _download_file(url: str, dest: Path) -> None:
    """Download *url* to *dest*, with a progress indicator."""

    # If the URL is a GitHub release download, try to add token auth
    req = urllib.request.Request(url)
    token = _get_github_token()
    if token and "github.com" in url:
        req.add_header("Authorization", f"Bearer {token}")
    req.add_header("User-Agent", "kimix-installer")

    with urllib.request.urlopen(req, timeout=120) as resp:
        with open(str(dest), "wb") as f:
            total_size = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if total_size > 0:
                    pct = min(100, int(downloaded * 100 / total_size))
                    sys.stdout.write(f"\r  {pct}%")
                    sys.stdout.flush()
    print()  # newline after progress


def _refresh_path_from_registry() -> None:
    """Refresh ``os.environ["PATH"]`` and ``os.environ["PATHEXT"]``
    from the Windows registry.

    Reads both the system (HKLM) and user (HKCU) values,
    expands REG_EXPAND_SZ entries, and merges them into the
    current process environment so that ``shutil.which`` can
    find binaries installed by external package managers
    (e.g. WinGet) without restarting the process.
    """
    import ctypes

    def _expand(value: str) -> str:
        """Expand REG_EXPAND_SZ strings via Windows API."""
        if "%" not in value:
            return value
        try:
            nchars = ctypes.windll.kernel32.ExpandEnvironmentStringsW(
                value, None, 0
            )
            if nchars == 0:
                return value
            buf = ctypes.create_unicode_buffer(nchars)
            ctypes.windll.kernel32.ExpandEnvironmentStringsW(
                value, buf, nchars
            )
            return buf.value
        except Exception:
            return os.path.expandvars(value)

    def _read(hive: int, subkey: str, name: str) -> tuple[str | None, int | None]:
        try:
            with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ) as key:
                val, reg_type = winreg.QueryValueEx(key, name)
                if isinstance(val, str):
                    return val, reg_type
                return None, None
        except (FileNotFoundError, OSError):
            return None, None

    def _merge_dedup(*sources: str) -> str:
        """Merge semicolon-separated sources, dedup case-insensitively."""
        seen: set[str] = set()
        merged: list[str] = []
        for src in sources:
            for part in src.split(";"):
                part = part.strip()
                if part and part.lower() not in seen:
                    seen.add(part.lower())
                    merged.append(part)
        return ";".join(merged)

    # --- PATH ---
    sys_val, sys_type = _read(
        winreg.HKEY_LOCAL_MACHINE,
        r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        "Path",
    )
    usr_val, usr_type = _read(
        winreg.HKEY_CURRENT_USER,
        r"Environment",
        "Path",
    )

    path_parts: list[str] = []
    if sys_val:
        if sys_type == winreg.REG_EXPAND_SZ:
            sys_val = _expand(sys_val)
        path_parts.append(sys_val)
    if usr_val:
        if usr_type == winreg.REG_EXPAND_SZ:
            usr_val = _expand(usr_val)
        path_parts.append(usr_val)

    if path_parts:
        os.environ["PATH"] = _merge_dedup(*path_parts)

    # --- PATHEXT ---
    sys_val, sys_type = _read(
        winreg.HKEY_LOCAL_MACHINE,
        r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        "PATHEXT",
    )
    usr_val, usr_type = _read(
        winreg.HKEY_CURRENT_USER,
        r"Environment",
        "PATHEXT",
    )

    pathext_parts: list[str] = []
    if sys_val:
        if sys_type == winreg.REG_EXPAND_SZ:
            sys_val = _expand(sys_val)
        pathext_parts.append(sys_val)
    if usr_val:
        if usr_type == winreg.REG_EXPAND_SZ:
            usr_val = _expand(usr_val)
        pathext_parts.append(usr_val)

    if pathext_parts:
        os.environ["PATHEXT"] = _merge_dedup(*pathext_parts)


def _ensure_in_user_path(dirpath: str) -> None:
    """Add *dirpath* to the current user's PATH environment variable (persistent)."""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Environment",
            0,
            winreg.KEY_READ | winreg.KEY_WRITE,
        )
    except FileNotFoundError:
        return

    try:
        path_val, _ = winreg.QueryValueEx(key, "Path")
    except FileNotFoundError:
        path_val = ""

    entries = [p.strip() for p in path_val.split(";") if p.strip()]
    if dirpath in entries:
        winreg.CloseKey(key)
        return

    entries.append(dirpath)
    new_path = ";".join(entries)
    winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, new_path)
    winreg.CloseKey(key)


def _coreutils_found(install_dir: str | None = None) -> bool:
    """Return ``True`` if ``cat.exe`` is available.

    When *install_dir* is given, checks that directory first
    (looking under ``bin/cat.exe``).  Falls back to checking
    PATH when *install_dir* is ``None``.
    """
    if install_dir:
        base = Path(install_dir)
        if (base / "bin" / "cat.exe").exists():
            return True
    return shutil.which("cat.exe") is not None


# ---------------------------------------------------------------------------
# strategy implementations
# ---------------------------------------------------------------------------

def _try_winget() -> bool:
    """Install Coreutils via WinGet (preferred, official Microsoft channel)."""
    if not shutil.which("winget"):
        return False
    try:
        print("Installing Coreutils via WinGet ...")
        result = _run(
            [
                "winget",
                "install",
                "--id",
                "Microsoft.Coreutils",
                "--silent",
                "--accept-package-agreements",
                "--accept-source-agreements"
            ],
            timeout=3000,
        )
        # WinGet updates the *registry* PATH; refresh the current process
        # environment so that shutil.which can see the new location.
        _refresh_path_from_registry()
        # In case the registry is not yet updated, probe known default
        # installation directories directly and temporarily inject them
        # into os.environ["PATH"] so the rest of the script can locate
        # the binaries.
        for candidate in (
            r"C:\Program Files\coreutils\bin",
            r"C:\Program Files (x86)\coreutils\bin",
        ):
            if (Path(candidate) / "cat.exe").exists():
                current_path = os.environ.get("PATH", "")
                if candidate.lower() not in current_path.lower():
                    os.environ["PATH"] = candidate + ";" + current_path
                return True
        # WinGet returns 0 on success but may also return non-zero when
        # the package is already installed; verify by looking for cat.exe.
        return _coreutils_found()
    except Exception as exc:
        print(f"WinGet install failed: {exc}")
        return False


def _get_latest_tag_via_redirect() -> str | None:
    """Get the latest release tag by following the ``/releases/latest`` redirect.

    This is a fallback when the GitHub API is rate-limited.
    """
    try:
        req = urllib.request.Request(
            _GITHUB_RELEASES_URL,
            headers={"User-Agent": "kimix-installer"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            final_url = resp.url
        # e.g. https://github.com/microsoft/coreutils/releases/tag/v2026.5.29
        tag = final_url.rstrip("/").rsplit("/", 1)[-1]
        if tag.startswith("v"):
            return tag
        print(f"Unexpected redirect URL format: {final_url}")
        return None
    except Exception as exc:
        print(f"Failed to resolve latest release redirect: {exc}")
        return None


def _construct_download_url_from_tag(tag: str) -> str | None:
    """Construct a download URL from a release tag, trying known asset name patterns.

    Returns the first valid download URL found, or ``None``.
    """
    version = tag.lstrip("v")
    base = f"https://github.com/microsoft/coreutils/releases/download/{tag}"
    # Try x64 first, then arm64, then any .exe
    archs = ["-x64.exe", "-arm64.exe", ".exe"]
    for arch in archs:
        for name in [
            f"coreutils-{version}{arch}",
            f"Coreutils-{version}{arch}",
        ]:
            url = f"{base}/{name}"
            try:
                req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "kimix-installer"})
                token = _get_github_token()
                if token:
                    req.add_header("Authorization", f"Bearer {token}")
                with urllib.request.urlopen(req, timeout=10):
                    return url
            except Exception:
                continue
    return None


def _try_github_download(
    install_dir: str | None = None,
) -> bool:
    """Download the latest Coreutils installer from GitHub and run it silently.

    Strategy:
    1. Query the GitHub API (authenticated with token if available).
    2. Fallback: resolve ``/releases/latest`` redirect and construct the
       download URL using known asset name patterns.

    This is a best-effort fallback: we try common silent-install flags
    used by NSIS, Inno Setup, and WiX.  If none succeed we give up.
    """
    download_url: str | None = None
    installer_name: str | None = None

    # --- Strategy 1: GitHub API ---
    try:
        print("Querying GitHub API for latest Coreutils release ...")
        headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "kimix-installer",
        }
        token = _get_github_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(_GITHUB_API_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        assets = data.get("assets", [])
        # Prefer x64 over arm64
        asset = next(
            (a for a in assets if a.get("name", "").endswith("-x64.exe")),
            None,
        )
        if asset is None:
            asset = next(
                (a for a in assets if a.get("name", "").endswith(".exe")),
                None,
            )
        if asset is not None:
            download_url = asset["browser_download_url"]
            installer_name = asset["name"]
    except Exception as exc:
        print(f"GitHub API query failed: {exc}")

    # --- Strategy 2: Fallback via redirect + pattern ---
    if download_url is None:
        print("Falling back to release redirect pattern ...")
        tag = _get_latest_tag_via_redirect()
        if tag:
            url = _construct_download_url_from_tag(tag)
            if url:
                download_url = url
                # Derive a reasonable filename from the URL
                installer_name = download_url.rstrip("/").rsplit("/", 1)[-1]

    if download_url is None or installer_name is None:
        print("Could not determine download URL for latest Coreutils release.")
        return False

    installer = Path(tempfile.gettempdir()) / installer_name
    # --- download ---
    try:
        print(f"Downloading {installer_name} ...")
        _download_file(download_url, installer)
    except Exception as exc:
        print(f"Download failed: {exc}")
        return False

    # --- install ---
    # Try a handful of common silent-install flag sets.
    
    ok = False
    try:
        print(f"Running installer ...")
        _run([str(installer)], timeout=3000)
        if _coreutils_found():
            ok = True
    except subprocess.TimeoutExpired:
        print("Installer timed out.")
    except Exception as exc:
        print(f"Installer error: {exc}")

    # --- clean up ---
    installer.unlink(missing_ok=True)
    return ok


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def install_coreutils(
    install_dir: str | None = None,
    *,
    add_to_path: bool = True,
    timeout: int = 300,
) -> str | None:
    """Silently install Microsoft Coreutils for Windows.

    Tries, in order:
      1. WinGet (``winget install Microsoft.Coreutils --silent``).
      2. Direct download from GitHub latest release (portable installer).

    Parameters
    ----------
    install_dir:
        Target directory.  Defaults to ``<share_dir>/coreutils``.
    add_to_path:
        Whether to append the ``bin`` folder to the user PATH.
    timeout:
        Seconds to wait for each install subprocess.

    Returns
    -------
    The ``bin`` directory path on success, or ``None`` on failure.
    """
    if not _is_windows():
        print("install_coreutils: this script only supports Windows.", file=sys.stderr)
        return None

    target = Path(install_dir) if install_dir else INSTALL_DIR
    bin_dir = str(target / "bin")

    # Already installed on PATH or in target dir?
    if _coreutils_found(install_dir):
        where = f"at {install_dir}" if install_dir else "on PATH"
        print(f"Coreutils is already installed {where}.")
        if add_to_path:
            _ensure_in_user_path(bin_dir)
        return bin_dir

    strategies: list[tuple[str, object]] = [
        ("winget", _try_winget),
        ("github direct download", lambda: _try_github_download(install_dir)),
    ]

    for name, fn in strategies:
        print(f"Trying {name} ...")
        try:
            ok = fn()  # type: ignore[operator]
        except Exception as exc:
            print(f"  {name} raised: {exc}")
            ok = False
        if ok and _coreutils_found():
            print(f"Coreutils installed successfully via {name}.")
            if add_to_path:
                _ensure_in_user_path(bin_dir)
            return bin_dir
        print(f"  {name} did not succeed.")

    print("All installation strategies failed.", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Install Microsoft Coreutils for Windows silently.",
    )
    parser.add_argument(
        "--dir",
        dest="install_dir",
        default=None,
        help="Custom install directory",
    )
    parser.add_argument(
        "--github",
        action="store_true",
        help="Use GitHub direct download strategy only",
    )
    args = parser.parse_args()

    if args.github:
        success = _try_github_download(install_dir=args.install_dir)
    else:
        success = install_coreutils(install_dir=args.install_dir)
    sys.exit(0 if success else 1)
