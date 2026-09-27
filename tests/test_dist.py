"""Unit tests for the pure helpers in dist.py (packaging script)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dist  # noqa: E402


class TestNamingAndUrls:
    def test_native_archive_names(self):
        assert dist.native_archive_name("win") == "kimix_base-windows-x64-1.2.1.zip"
        assert dist.native_archive_name("ubuntu") == "kimix_base-linux-x64-1.2.1.zip"

    def test_native_archive_urls(self):
        assert dist.native_archive_url("win").endswith(
            "/Release/kimix_base-windows-x64-1.2.1.zip"
        )
        assert dist.native_archive_url("ubuntu").endswith(
            "/Release/kimix_base-linux-x64-1.2.1.zip"
        )

    def test_rg_urls(self):
        assert dist.rg_url("win").endswith(
            "ripgrep-15.2.0-x86_64-pc-windows-msvc.zip"
        )
        assert dist.rg_url("ubuntu").endswith(
            "ripgrep-15.2.0-x86_64-unknown-linux-musl.tar.gz"
        )

    def test_rtk_urls(self):
        assert dist.rtk_url("win").endswith("rtk-x86_64-pc-windows-msvc.zip")
        assert dist.rtk_url("ubuntu").endswith("rtk-x86_64-unknown-linux-musl.tar.gz")


class TestPickPbsAsset:
    def _assets(self, names):
        return [{"name": n, "browser_download_url": f"https://x/{n}"} for n in names]

    def test_picks_install_only_for_target(self):
        names = [
            "cpython-3.14.0+20251001-x86_64-pc-windows-msvc-install_only.tar.gz",
            "cpython-3.14.1+20251115-x86_64-pc-windows-msvc-install_only.tar.gz",
            "cpython-3.14.1+20251115-x86_64-pc-windows-msvc-install_only_stripped.tar.gz",
            "cpython-3.13.9+20251115-x86_64-pc-windows-msvc-install_only.tar.gz",
        ]
        picked = dist.pick_pbs_asset(self._assets(names), "win")
        assert picked == "cpython-3.14.1+20251115-x86_64-pc-windows-msvc-install_only.tar.gz"

    def test_ignores_other_platform_assets(self):
        names = [
            "cpython-3.14.1+20251115-x86_64-unknown-linux-gnu-install_only.tar.gz",
            "cpython-3.14.1+20251115-aarch64-apple-darwin-install_only.tar.gz",
        ]
        assert dist.pick_pbs_asset(self._assets(names), "win") is None
        assert (
            dist.pick_pbs_asset(self._assets(names), "ubuntu")
            == "cpython-3.14.1+20251115-x86_64-unknown-linux-gnu-install_only.tar.gz"
        )

    def test_pbs_asset_regex_rejects_wrong_series(self):
        import re

        pattern = re.compile(dist.pbs_asset_regex("win"))
        assert not pattern.match(
            "cpython-3.15.0+20251115-x86_64-pc-windows-msvc-install_only.tar.gz"
        )


class TestSelectNativeArtifacts:
    def test_windows_picks_pyd_and_dlls(self):
        names = ["runtime_py.pyd", "vcruntime140.dll", "README.txt", "kimix_native"]
        assert dist.select_native_artifacts(names, "win") == [
            "runtime_py.pyd",
            "vcruntime140.dll",
        ]

    def test_linux_picks_so_only(self):
        names = ["runtime_py.so", "notes.md"]
        assert dist.select_native_artifacts(names, "ubuntu") == ["runtime_py.so"]

    def test_dlls_are_staged_alongside_extension(self):
        # Mirrors install.py _stage_native_files: DLLs ride along with the
        # extension. Callers only invoke this on a directory that contains the
        # extension, so a DLL-only result never reaches staging in practice.
        assert dist.select_native_artifacts(["other.dll"], "win") == ["other.dll"]


class TestWslPath:
    def test_drive_translation(self):
        assert dist.win_path_to_wsl(Path(r"D:\kimi-agent\dist.py")) == (
            "/mnt/d/kimi-agent/dist.py"
        )

    def test_forward_slashes(self):
        assert dist.win_path_to_wsl(Path("D:/kimi-agent")) == "/mnt/d/kimi-agent"


class TestLaunchers:
    def test_windows_launcher_uses_bundle_python(self):
        script = dist.launcher_script("win")
        assert 'KIMIX_HOME=%~dp0' in script
        assert r'"%KIMIX_HOME%python\python.exe" -m kimix %*' in script
        assert "KIMIX_NATIVE_PATH" in script
        assert "rg.exe" in script and "rtk.exe" in script
        assert "copy /y" in script  # offline bootstrap of ~/.kimi/bin

    def test_linux_launcher_uses_bundle_python(self):
        script = dist.launcher_script("ubuntu")
        assert 'KIMIX_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"' in script
        assert 'exec "$KIMIX_HOME/python/bin/python3" -m kimix "$@"' in script
        assert "KIMIX_NATIVE_PATH" in script
        assert "for b in rg rtk" in script

    def test_launcher_dispatch(self):
        assert "cmd" not in dist.launcher_script("ubuntu")
        assert "BASH_SOURCE" not in dist.launcher_script("win")


class TestTargetsTable:
    def test_both_targets_defined(self):
        assert set(dist.TARGETS) == {"win", "ubuntu"}
        for key, target in dist.TARGETS.items():
            for field in (
                "family",
                "pbs_target",
                "python_exe",
                "native_archive",
                "native_mod",
                "rg_target",
                "rtk_target",
                "launcher",
            ):
                assert target[field], f"{key}.{field} must be non-empty"

    def test_python_exe_paths(self):
        assert dist.python_exe_path(Path("/out"), "win") == Path(
            "/out/python/python.exe"
        )
        assert dist.python_exe_path(Path("/out"), "ubuntu") == Path(
            "/out/python/bin/python3"
        )


class TestParseArgs:
    def test_defaults(self):
        args = dist.parse_args([])
        assert args.platform == "all"
        assert args.clean is False

    def test_platform_choice_validation(self):
        with pytest.raises(SystemExit):
            dist.parse_args(["--platform", "macos"])
