"""Shared, dependency-free helpers for installing the ripgrep binary.

This module contains only the parts of ripgrep installation that are identical
between the runtime async installer (``kimi_cli.tools.file.grep_local``) and the
standalone bootstrap script (``scripts/install_ripgrep.py``). It intentionally
avoids any third-party dependencies so it can be imported by the bootstrap
script before the package is installed.
"""

from __future__ import annotations

import platform
import shutil
import stat
import sys
import tarfile
import zipfile
from pathlib import Path

RG_VERSION = "15.2.0"
RG_BASE_URL = "https://github.com/BurntSushi/ripgrep/releases/download"


def _rg_binary_name() -> str:
    return "rg.exe" if sys.platform == "win32" else "rg"


def _detect_rg_target() -> str:
    sys_name = platform.system()
    mach = platform.machine().lower()

    if mach in ("x86_64", "amd64"):
        arch = "x86_64"
    elif mach in ("arm64", "aarch64"):
        arch = "aarch64"
    else:
        raise RuntimeError(f"Unsupported architecture for ripgrep: {mach}")

    if sys_name == "Darwin":
        os_name = "apple-darwin"
    elif sys_name == "Linux":
        os_name = "unknown-linux-musl" if arch == "x86_64" else "unknown-linux-gnu"
    elif sys_name == "Windows":
        os_name = "pc-windows-msvc"
    else:
        raise RuntimeError(f"Unsupported operating system for ripgrep: {sys_name}")

    return f"{arch}-{os_name}"


def _rg_archive_extension(target: str) -> str:
    return "zip" if "windows" in target else "tar.gz"


def _rg_archive_name(version: str, target: str) -> str:
    return f"ripgrep-{version}-{target}.{_rg_archive_extension(target)}"


def _rg_download_url(version: str, target: str) -> str:
    return f"{RG_BASE_URL}/{version}/{_rg_archive_name(version, target)}"


def _extract_rg_archive(
    archive_path: Path,
    destination: Path,
    target: str,
    bin_name: str,
) -> None:
    """Extract the ripgrep binary from *archive_path* to *destination*."""
    is_windows = "windows" in target

    try:
        if is_windows:
            with zipfile.ZipFile(archive_path, "r") as zf:
                member_name = next(
                    (name for name in zf.namelist() if Path(name).name == bin_name),
                    None,
                )
                if not member_name:
                    raise RuntimeError("Ripgrep binary not found in archive")
                with zf.open(member_name) as source, open(destination, "wb") as dest_fh:
                    shutil.copyfileobj(source, dest_fh)
        else:
            with tarfile.open(archive_path, "r:gz") as tar:
                member = next(
                    (m for m in tar.getmembers() if Path(m.name).name == bin_name),
                    None,
                )
                if not member:
                    raise RuntimeError("Ripgrep binary not found in archive")
                extracted = tar.extractfile(member)
                if not extracted:
                    raise RuntimeError("Failed to extract ripgrep binary")
                with open(destination, "wb") as dest_fh:
                    shutil.copyfileobj(extracted, dest_fh)
    except (zipfile.BadZipFile, tarfile.TarError, OSError) as exc:
        raise RuntimeError("Failed to extract ripgrep archive") from exc

    destination.chmod(
        destination.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
