"""Install ripgrep (rg) into the KIMI share directory.

This mirrors the download-and-extract logic used by the local Grep tool so
that the same binary can be made available system-wide during project install.

Usage:
    python install_ripgrep.py                          # default install
    python install_ripgrep.py --version 15.1.0         # pin version
    python install_ripgrep.py --dir "D:\\Tools"         # custom bin dir
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
RG_VERSION: str = "15.1.0"
"""Ripgrep version to install when using the direct-download strategy."""

RG_BASE_URL = "https://github.com/BurntSushi/ripgrep/releases/download"


def _get_share_dir() -> Path:
    """Get the KIMI share directory path."""
    if share_dir := os.getenv("KIMI_SHARE_DIR"):
        return Path(share_dir)
    return Path.home() / ".kimi"


INSTALL_DIR = _get_share_dir() / "bin"
"""Default directory for the ripgrep binary."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _rg_binary_name() -> str:
    return "rg.exe" if sys.platform == "win32" else "rg"


def _detect_target() -> str | None:
    sys_name = platform.system()
    mach = platform.machine().lower()

    if mach in ("x86_64", "amd64"):
        arch = "x86_64"
    elif mach in ("arm64", "aarch64"):
        arch = "aarch64"
    else:
        print(f"Unsupported architecture for ripgrep: {mach}", file=sys.stderr)
        return None

    if sys_name == "Darwin":
        os_name = "apple-darwin"
    elif sys_name == "Linux":
        os_name = "unknown-linux-musl" if arch == "x86_64" else "unknown-linux-gnu"
    elif sys_name == "Windows":
        os_name = "pc-windows-msvc"
    else:
        print(f"Unsupported operating system for ripgrep: {sys_name}", file=sys.stderr)
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


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def install_ripgrep(
    version: str = RG_VERSION,
    install_dir: str | None = INSTALL_DIR,
) -> str | None:
    """Download and install ripgrep into *install_dir*.

    Parameters
    ----------
    version:
        Ripgrep version string.
    install_dir:
        Directory to place the ``rg`` / ``rg.exe`` binary in.
        Defaults to ``<share_dir>/bin``.

    Returns
    -------
    The directory path on success, or ``None`` on failure.
    """
    bin_name = _rg_binary_name()
    target_dir = Path(install_dir) if install_dir else INSTALL_DIR
    destination = target_dir / bin_name

    # Already installed in the target directory?
    if destination.is_file():
        print(f"Ripgrep is already installed at {destination}.")
        return str(target_dir)

    target = _detect_target()
    if not target:
        return None

    is_windows = "windows" in target
    archive_ext = "zip" if is_windows else "tar.gz"
    filename = f"ripgrep-{version}-{target}.{archive_ext}"
    url = f"{RG_BASE_URL}/{version}/{filename}"

    print(f"Downloading ripgrep {version} for {target} ...")
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        with tempfile.TemporaryDirectory(prefix="kimi-rg-") as tmpdir:
            archive_path = Path(tmpdir) / filename
            _download_file(url, archive_path)

            print(f"Extracting ripgrep to {target_dir} ...")
            if is_windows:
                with zipfile.ZipFile(archive_path, "r") as zf:
                    member_name = next(
                        (name for name in zf.namelist() if Path(name).name == bin_name),
                        None,
                    )
                    if not member_name:
                        print("Ripgrep binary not found in archive.", file=sys.stderr)
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
                        print("Ripgrep binary not found in archive.", file=sys.stderr)
                        return None
                    extracted = tar.extractfile(member)
                    if not extracted:
                        print("Failed to extract ripgrep binary.", file=sys.stderr)
                        return None
                    with open(destination, "wb") as dest_fh:
                        shutil.copyfileobj(extracted, dest_fh)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"Failed to download ripgrep: {exc}", file=sys.stderr)
        return None
    except (zipfile.BadZipFile, tarfile.TarError, OSError) as exc:
        print(f"Failed to extract ripgrep archive: {exc}", file=sys.stderr)
        return None

    destination.chmod(destination.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"Ripgrep installed at {destination}.")
    return str(target_dir)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Install ripgrep into the KIMI share directory.",
    )
    parser.add_argument(
        "--version",
        default=RG_VERSION,
        help=f"Ripgrep version (default: {RG_VERSION})",
    )
    parser.add_argument(
        "--dir",
        dest="install_dir",
        default=INSTALL_DIR,
        help="Custom bin directory (default: <share_dir>/bin)",
    )
    args = parser.parse_args()

    result = install_ripgrep(version=args.version, install_dir=args.install_dir)
    sys.exit(0 if result else 1)
