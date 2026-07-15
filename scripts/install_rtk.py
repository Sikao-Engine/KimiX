"""Install rtk (reasoning toolkit) into the KIMI share directory.

This mirrors the download-and-extract logic used by ``kimi_cli.install`` so
that the same binary can be made available system-wide during project install.

Usage:
    python install_rtk.py                          # default install
    python install_rtk.py --version 0.43.0         # pin version
    python install_rtk.py --dir "D:\\Tools"         # custom bin dir
"""

from __future__ import annotations

import os
import platform
import shutil
import stat
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

# ============================================================
# Global configuration
# ============================================================
RTK_VERSION: str = "0.43.0"
"""rtk version to install when using the direct-download strategy."""

RTK_BASE_URL = "https://github.com/rtk-ai/rtk/releases/download"


def _get_share_dir() -> Path:
    """Get the KIMI share directory path."""
    if share_dir := os.getenv("KIMI_SHARE_DIR"):
        return Path(share_dir)
    return Path.home() / ".kimi"


INSTALL_DIR = _get_share_dir() / "bin"
"""Default directory for the rtk binary."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _rtk_binary_name() -> str:
    return "rtk.exe" if sys.platform == "win32" else "rtk"


def _detect_target() -> str | None:
    sys_name = platform.system()
    mach = platform.machine().lower()

    if mach in ("x86_64", "amd64"):
        arch = "x86_64"
    elif mach in ("arm64", "aarch64"):
        arch = "aarch64"
    else:
        print(f"Unsupported architecture for rtk: {mach}", file=sys.stderr)
        return None

    if sys_name == "Darwin":
        os_name = "apple-darwin"
    elif sys_name == "Linux":
        os_name = "unknown-linux-musl" if arch == "x86_64" else "unknown-linux-gnu"
    elif sys_name == "Windows":
        if arch != "x86_64":
            print(f"Unsupported Windows arch for rtk: {mach}", file=sys.stderr)
            return None
        os_name = "pc-windows-msvc"
    else:
        print(f"Unsupported operating system for rtk: {sys_name}", file=sys.stderr)
        return None

    return f"{arch}-{os_name}"


def _download_file(url: str, dest: Path) -> None:
    """Download *url* to *dest*, with a progress indicator."""

    def _report(block_num: int, block_size: int, total_size: int) -> None:
        if total_size > 0:
            pct = min(100, int(block_num * block_size * 100 / total_size))
            sys.stdout.write(f"\r  {pct}%")
            sys.stdout.flush()

    urllib.request.urlretrieve(url, str(dest), _report)
    print()  # newline after progress


def _ensure_in_user_path(dirpath: str) -> None:
    """Add *dirpath* to the current user's PATH environment variable (persistent).

    Updates both the registry (Windows, for new processes) and the current
    process's ``os.environ`` so that ``shutil.which`` picks it up immediately.
    """
    # --- current process (immediate) ---
    current_path = os.environ.get("PATH", "")
    current_entries = [p.strip() for p in current_path.split(os.pathsep) if p.strip()]
    if dirpath not in current_entries:
        current_entries.append(dirpath)
        os.environ["PATH"] = os.pathsep.join(current_entries)

    # --- registry (persistent, Windows only) ---
    if sys.platform != "win32":
        return

    import winreg

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


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def install_rtk(
    version: str = RTK_VERSION,
    install_dir: str | None = INSTALL_DIR,
) -> str | None:
    """Download and install rtk into *install_dir*.

    Parameters
    ----------
    version:
        rtk version string.
    install_dir:
        Directory to place the ``rtk`` / ``rtk.exe`` binary in.
        Defaults to ``<share_dir>/bin``.

    Returns
    -------
    The directory path on success, or ``None`` on failure.
    """
    bin_name = _rtk_binary_name()
    target_dir = Path(install_dir) if install_dir else INSTALL_DIR
    destination = target_dir / bin_name

    # Already installed in the target directory?
    if destination.is_file():
        print(f"rtk is already installed at {destination}.")
        _ensure_in_user_path(str(target_dir))
        return str(target_dir)

    # Already on PATH?  Still add target dir so the share binary takes priority.
    if shutil.which(bin_name):
        print(f"rtk is already on PATH ({shutil.which(bin_name)}).")
        _ensure_in_user_path(str(target_dir))
        return str(target_dir)

    target = _detect_target()
    if not target:
        return None

    is_windows = "windows" in target
    archive_ext = "zip" if is_windows else "tar.gz"
    filename = f"rtk-{target}.{archive_ext}"
    url = f"{RTK_BASE_URL}/v{version}/{filename}"

    print(f"Downloading rtk {version} for {target} ...")
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        with tempfile.TemporaryDirectory(prefix="kimi-rtk-") as tmpdir:
            archive_path = Path(tmpdir) / filename
            _download_file(url, archive_path)

            print(f"Extracting rtk to {target_dir} ...")
            if is_windows:
                with zipfile.ZipFile(archive_path, "r") as zf:
                    member_name = next(
                        (name for name in zf.namelist() if Path(name).name == bin_name),
                        None,
                    )
                    if not member_name:
                        print("rtk binary not found in archive.", file=sys.stderr)
                        return None
                    with zf.open(member_name) as source, open(destination, "wb") as dest_fh:
                        shutil.copyfileobj(source, dest_fh)
            else:
                with tarfile.open(archive_path, "r:gz") as tar:
                    member = next(
                        (m for m in tar.getmembers() if Path(m.name).name == bin_name),
                        None,
                    )
                    if not member:
                        print("rtk binary not found in archive.", file=sys.stderr)
                        return None
                    extracted = tar.extractfile(member)
                    if not extracted:
                        print("Failed to extract rtk binary.", file=sys.stderr)
                        return None
                    with open(destination, "wb") as dest_fh:
                        shutil.copyfileobj(extracted, dest_fh)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"Failed to download rtk: {exc}", file=sys.stderr)
        return None
    except (zipfile.BadZipFile, tarfile.TarError, OSError) as exc:
        print(f"Failed to extract rtk archive: {exc}", file=sys.stderr)
        return None

    destination.chmod(destination.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    _ensure_in_user_path(str(target_dir))
    print(f"rtk installed at {destination}.")
    return str(target_dir)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Install rtk into the KIMI share directory.",
    )
    parser.add_argument(
        "--version",
        default=RTK_VERSION,
        help=f"rtk version (default: {RTK_VERSION})",
    )
    parser.add_argument(
        "--dir",
        dest="install_dir",
        default=INSTALL_DIR,
        help="Custom bin directory (default: <share_dir>/bin)",
    )
    args = parser.parse_args()

    result = install_rtk(version=args.version, install_dir=args.install_dir)
    sys.exit(0 if result else 1)
