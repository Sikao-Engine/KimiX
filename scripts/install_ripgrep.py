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
import sys
import tempfile
import urllib.request
from pathlib import Path

# ============================================================
# Import shared helpers. When this script is run before the
# package is installed (e.g. from the root install.py), locate
# the in-repo source tree and add it to sys.path.
# ============================================================


def _import_ripgrep_common():
    """Import ``kimi_cli._ripgrep_common`` from the installed package or source tree."""
    try:
        from kimi_cli import _ripgrep_common

        return _ripgrep_common
    except ImportError:
        pass

    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent
    src_path = repo_root / "kimi-cli" / "src"
    if src_path.is_dir():
        sys.path.insert(0, str(src_path))

    from kimi_cli import _ripgrep_common

    return _ripgrep_common


_ripgrep_common = _import_ripgrep_common()

# Global configuration
RG_VERSION: str = _ripgrep_common.RG_VERSION
"""Ripgrep version to install when using the direct-download strategy."""

RG_BASE_URL = _ripgrep_common.RG_BASE_URL

_rg_binary_name = _ripgrep_common._rg_binary_name
_detect_target = _ripgrep_common._detect_rg_target
_rg_archive_name = _ripgrep_common._rg_archive_name
_rg_download_url = _ripgrep_common._rg_download_url
_extract_rg_archive = _ripgrep_common._extract_rg_archive


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
    install_dir: str | Path | None = INSTALL_DIR,
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

    try:
        target = _detect_target()
    except RuntimeError as exc:
        print(f"{exc}", file=sys.stderr)
        return None

    filename = _rg_archive_name(version, target)
    url = _rg_download_url(version, target)

    print(f"Downloading ripgrep {version} for {target} ...")
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        with tempfile.TemporaryDirectory(prefix="kimi-rg-") as tmpdir:
            archive_path = Path(tmpdir) / filename
            _download_file(url, archive_path)

            print(f"Extracting ripgrep to {target_dir} ...")
            _extract_rg_archive(archive_path, destination, target, bin_name)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"Failed to download ripgrep: {exc}", file=sys.stderr)
        return None
    except RuntimeError as exc:
        print(f"Failed to extract ripgrep archive: {exc}", file=sys.stderr)
        return None

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
