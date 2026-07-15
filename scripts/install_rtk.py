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
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

# ============================================================
# Import shared helpers. When this script is run before the
# package is installed (e.g. from the root install.py), locate
# the in-repo source tree and add it to sys.path.
# ============================================================


def _import_rtk_common():
    """Import ``kimi_cli._rtk_common`` from the installed package or source tree."""
    try:
        from kimi_cli import _rtk_common

        return _rtk_common
    except ImportError:
        pass

    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent
    src_path = repo_root / "kimi-cli" / "src"
    if src_path.is_dir():
        sys.path.insert(0, str(src_path))

    from kimi_cli import _rtk_common

    return _rtk_common


_rtk_common = _import_rtk_common()

# Global configuration
RTK_VERSION: str = _rtk_common.RTK_VERSION
"""rtk version to install when using the direct-download strategy."""

RTK_BASE_URL = _rtk_common.RTK_BASE_URL

_rtk_binary_name = _rtk_common._rtk_binary_name
_detect_target = _rtk_common._detect_rtk_target
_rtk_archive_name = _rtk_common._rtk_archive_name
_rtk_download_url = _rtk_common._rtk_download_url
_extract_rtk_archive = _rtk_common._extract_rtk_archive


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
    install_dir: str | Path | None = INSTALL_DIR,
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

    try:
        target = _detect_target()
    except RuntimeError as exc:
        print(f"{exc}", file=sys.stderr)
        return None

    filename = _rtk_archive_name(target)
    url = _rtk_download_url(version, target)

    print(f"Downloading rtk {version} for {target} ...")
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        with tempfile.TemporaryDirectory(prefix="kimi-rtk-") as tmpdir:
            archive_path = Path(tmpdir) / filename
            _download_file(url, archive_path)

            print(f"Extracting rtk to {target_dir} ...")
            _extract_rtk_archive(archive_path, destination, target, bin_name)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"Failed to download rtk: {exc}", file=sys.stderr)
        return None
    except RuntimeError as exc:
        print(f"Failed to extract rtk archive: {exc}", file=sys.stderr)
        return None

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
