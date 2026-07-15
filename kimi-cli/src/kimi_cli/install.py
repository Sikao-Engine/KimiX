"""rtk binary installation and discovery.

Downloads the rtk binary from GitHub releases and installs it to the shared bin directory.
Mirrors the same pattern used for ripgrep installation in kimi_cli.tools.file.grep_local.
"""

from __future__ import annotations

import asyncio
import platform
import shutil
import stat
import tarfile
import tempfile
import zipfile
from pathlib import Path

import aiohttp

import kimi_cli
from kimi_cli.share import get_share_dir
from kimi_cli.utils.aiohttp import new_client_session
from kimi_cli.utils.logging import logger

RTK_VERSION = "0.43.0"
RTK_BASE_URL = "https://github.com/rtk-ai/rtk/releases/download"
_RTK_DOWNLOAD_LOCK = asyncio.Lock()


def _rtk_binary_name() -> str:
    return "rtk.exe" if platform.system() == "Windows" else "rtk"


def _find_existing_rtk(bin_name: str) -> Path | None:
    """Find rtk binary: share dir, bundled deps, then PATH."""
    share_bin = get_share_dir() / "bin" / bin_name
    if share_bin.is_file():
        return share_bin

    assert kimi_cli.__file__ is not None
    local_dep = Path(kimi_cli.__file__).parent / "deps" / "bin" / bin_name
    if local_dep.is_file():
        return local_dep

    system_rtk = shutil.which("rtk")
    if system_rtk:
        return Path(system_rtk)

    return None


def _detect_rtk_target() -> str | None:
    sys_name = platform.system()
    mach = platform.machine().lower()

    if mach in ("x86_64", "amd64"):
        arch = "x86_64"
    elif mach in ("arm64", "aarch64"):
        arch = "aarch64"
    else:
        logger.error("Unsupported architecture for rtk: {mach}", mach=mach)
        return None

    if sys_name == "Darwin":
        os_name = "apple-darwin"
    elif sys_name == "Linux":
        os_name = "unknown-linux-musl" if arch == "x86_64" else "unknown-linux-gnu"
    elif sys_name == "Windows":
        if arch != "x86_64":
            logger.error("Unsupported Windows arch for rtk: {mach}", mach=mach)
            return None
        os_name = "pc-windows-msvc"
    else:
        logger.error("Unsupported OS for rtk: {sys_name}", sys_name=sys_name)
        return None

    return f"{arch}-{os_name}"


async def _download_and_install_rtk(bin_name: str) -> Path:
    target = _detect_rtk_target()
    if not target:
        raise RuntimeError("Unsupported platform for rtk download")

    is_windows = "windows" in target
    archive_ext = "zip" if is_windows else "tar.gz"
    filename = f"rtk-{target}.{archive_ext}"
    url = f"{RTK_BASE_URL}/v{RTK_VERSION}/{filename}"
    logger.info("Downloading rtk from {url}", url=url)

    share_bin_dir = get_share_dir() / "bin"
    share_bin_dir.mkdir(parents=True, exist_ok=True)
    destination = share_bin_dir / bin_name

    download_timeout = aiohttp.ClientTimeout(total=600, sock_read=60, sock_connect=15)
    async with new_client_session(timeout=download_timeout) as session:
        with tempfile.TemporaryDirectory(prefix="kimi-rtk-") as tmpdir:
            archive_path = Path(tmpdir) / filename

            try:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    with open(archive_path, "wb") as fh:
                        async for chunk in resp.content.iter_chunked(1024 * 64):
                            if chunk:
                                fh.write(chunk)
            except (aiohttp.ClientError, TimeoutError) as exc:
                raise RuntimeError("Failed to download rtk binary") from exc

            try:
                if is_windows:
                    with zipfile.ZipFile(archive_path, "r") as zf:
                        member_name = next(
                            (name for name in zf.namelist() if Path(name).name == bin_name),
                            None,
                        )
                        if not member_name:
                            raise RuntimeError("rtk binary not found in archive")
                        with zf.open(member_name) as source, open(destination, "wb") as dest_fh:
                            shutil.copyfileobj(source, dest_fh)
                else:
                    with tarfile.open(archive_path, "r:gz") as tar:
                        member = next(
                            (m for m in tar.getmembers() if Path(m.name).name == bin_name),
                            None,
                        )
                        if not member:
                            raise RuntimeError("rtk binary not found in archive")
                        extracted = tar.extractfile(member)
                        if not extracted:
                            raise RuntimeError("Failed to extract rtk binary")
                        with open(destination, "wb") as dest_fh:
                            shutil.copyfileobj(extracted, dest_fh)
            except (zipfile.BadZipFile, tarfile.TarError, OSError) as exc:
                raise RuntimeError("Failed to extract rtk archive") from exc

    destination.chmod(destination.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    logger.info("Installed rtk to {destination}", destination=destination)
    return destination


async def ensure_rtk_path() -> str:
    bin_name = _rtk_binary_name()
    existing = _find_existing_rtk(bin_name)
    if existing:
        return str(existing)

    async with _RTK_DOWNLOAD_LOCK:
        existing = _find_existing_rtk(bin_name)
        if existing:
            return str(existing)

        downloaded = await _download_and_install_rtk(bin_name)
        return str(downloaded)


__all__ = [
    "RTK_VERSION",
    "RTK_BASE_URL",
    "_rtk_binary_name",
    "_find_existing_rtk",
    "_detect_rtk_target",
    "_download_and_install_rtk",
    "ensure_rtk_path",
]
