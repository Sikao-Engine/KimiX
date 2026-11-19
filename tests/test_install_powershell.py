"""Unit tests for the PowerShell 7 (``pwsh``) installer in install.py.

The tests never touch the network: every download / probe / PATH write is
replaced by a fake, so the suite also runs on Linux and macOS (where the whole
PowerShell step is a no-op).
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import install  # noqa: E402


def _make_file(path: Path, payload: bytes = b"MZ fake") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _write_fake_pwsh_zip(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("pwsh.exe", b"MZ fake")
        zf.writestr("pwsh.dll", b"fake")
        zf.writestr("Modules/PSReadLine/PSReadLine.dll", b"fake")
        zf.writestr("LICENSE.txt", b"MIT")


def _raise_urlerror(_url: str, _dest: Path) -> None:
    raise install.urllib.error.URLError("network unreachable")


class TestVersionHelpers:
    def test_parse_version(self):
        assert install._parse_version("PowerShell 7.6.9") == (7, 6, 9)
        assert install._parse_version("v7.7.0") == (7, 7, 0)
        assert install._parse_version("7.5") == (7, 5, 0)
        assert install._parse_version("") is None
        assert install._parse_version("no version here") is None

    def test_format_version(self):
        assert install._format_version((7, 7, 0)) == "7.7.0"
        assert install._format_version((7, 10, 1)) == "7.10.1"


class TestArchAndUrls:
    @pytest.mark.parametrize(
        ("machine", "expected"),
        [
            ("AMD64", "x64"),
            ("x86_64", "x64"),
            ("arm64", "arm64"),
            ("aarch64", "arm64"),
            ("x86", "x86"),
            ("i686", "x86"),
        ],
    )
    def test_arch_mapping(self, monkeypatch, machine, expected):
        monkeypatch.setattr(install.platform, "machine", lambda: machine)
        assert install._powershell_arch() == expected

    def test_arch_rejects_unknown_machine(self, monkeypatch):
        monkeypatch.setattr(install.platform, "machine", lambda: "riscv64")
        with pytest.raises(RuntimeError, match="Unsupported architecture"):
            install._powershell_arch()

    def test_archive_name_and_url(self):
        assert install._powershell_archive_name("7.7.0", "x64") == "PowerShell-7.7.0-win-x64.zip"
        url = install._powershell_download_url("7.7.0", "x64")
        assert url == (
            "https://github.com/PowerShell/PowerShell/releases/download/"
            "v7.7.0/PowerShell-7.7.0-win-x64.zip"
        )

    def test_install_dir_is_shared_bin(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIMI_SHARE_DIR", str(tmp_path))
        assert install._powershell_install_dir() == tmp_path / "bin"
        assert install._shared_bin_path("pwsh.exe") == tmp_path / "bin" / "pwsh.exe"


class TestFindPowershell7:
    def test_no_candidates(self, monkeypatch):
        monkeypatch.setattr(install, "_powershell_candidate_paths", list)
        assert install._find_powershell7() is None

    def test_accepts_powershell7(self, monkeypatch, tmp_path):
        pwsh = _make_file(tmp_path / "pwsh.exe")
        monkeypatch.setattr(install, "_powershell_candidate_paths", lambda: [pwsh])
        monkeypatch.setattr(install, "_powershell_version", lambda _b: (7, 7, 0))
        assert install._find_powershell7() == (pwsh, (7, 7, 0))

    def test_rejects_legacy_powershell(self, monkeypatch, tmp_path, capsys):
        pwsh = _make_file(tmp_path / "pwsh.exe")
        monkeypatch.setattr(install, "_powershell_candidate_paths", lambda: [pwsh])
        monkeypatch.setattr(install, "_powershell_version", lambda _b: (5, 1, 26100))
        assert install._find_powershell7() is None
        assert "5.1.26100" in capsys.readouterr().out

    def test_skips_app_execution_alias_stub(self, monkeypatch, tmp_path):
        stub = _make_file(tmp_path / "WindowsApps" / "pwsh.exe", b"")
        monkeypatch.setattr(install, "_powershell_candidate_paths", lambda: [stub])
        monkeypatch.setattr(
            install,
            "_powershell_version",
            lambda _b: pytest.fail("0-byte aliases must not be executed"),
        )
        assert install._find_powershell7() is None

    def test_skips_unreadable_candidate(self, monkeypatch, tmp_path):
        broken = _make_file(tmp_path / "broken.exe")
        good = _make_file(tmp_path / "good.exe")
        versions = {str(broken): None, str(good): (7, 6, 9)}
        monkeypatch.setattr(install, "_powershell_candidate_paths", lambda: [broken, good])
        monkeypatch.setattr(install, "_powershell_version", lambda b: versions[str(b)])
        assert install._find_powershell7() == (good, (7, 6, 9))

    def test_deduplicates_candidates(self, monkeypatch, tmp_path):
        pwsh = _make_file(tmp_path / "pwsh.exe")
        seen: list[Path] = []

        def fake_version(binary: Path):
            seen.append(binary)
            return (7, 7, 0)

        monkeypatch.setattr(install, "_powershell_candidate_paths", lambda: [pwsh, pwsh])
        monkeypatch.setattr(install, "_powershell_version", fake_version)
        assert install._find_powershell7() == (pwsh, (7, 7, 0))
        assert seen == [pwsh]


class TestLatestVersionResolution:
    def test_prefers_redirect_resolver(self, monkeypatch):
        monkeypatch.setattr(install, "_powershell_latest_version_from_redirect", lambda: "7.7.0")
        monkeypatch.setattr(
            install,
            "_powershell_latest_version_from_api",
            lambda: pytest.fail("the API must not be consulted when the tag resolves"),
        )
        assert install._powershell_latest_version() == "7.7.0"

    def test_falls_back_to_api(self, monkeypatch, capsys):
        def broken():
            raise install.urllib.error.URLError("github.com blocked")

        monkeypatch.setattr(install, "_powershell_latest_version_from_redirect", broken)
        monkeypatch.setattr(install, "_powershell_latest_version_from_api", lambda: "7.6.9")
        assert install._powershell_latest_version() == "7.6.9"
        assert "github.com blocked" in capsys.readouterr().out

    def test_falls_back_to_pinned_version(self, monkeypatch, capsys):
        def broken():
            raise install.urllib.error.URLError("offline")

        monkeypatch.setattr(install, "_powershell_latest_version_from_redirect", broken)
        monkeypatch.setattr(install, "_powershell_latest_version_from_api", broken)
        assert install._powershell_latest_version() == install.POWERSHELL_FALLBACK_VERSION
        assert "falling back" in capsys.readouterr().out

    def test_redirect_parses_tag_from_location(self, monkeypatch):
        class FakeOpener:
            def open(self, request, timeout=0):  # noqa: ARG002
                raise install.urllib.error.HTTPError(
                    install.POWERSHELL_RELEASES_LATEST_URL,
                    302,
                    "Found",
                    {"Location": ("https://github.com/PowerShell/PowerShell/releases/tag/v7.7.0")},
                    None,
                )

        monkeypatch.setattr(install.urllib.request, "build_opener", lambda *handlers: FakeOpener())
        assert install._powershell_latest_version_from_redirect() == "7.7.0"

    def test_redirect_returns_none_without_location(self, monkeypatch):
        class FakeOpener:
            def open(self, request, timeout=0):  # noqa: ARG002
                raise install.urllib.error.HTTPError(
                    install.POWERSHELL_RELEASES_LATEST_URL, 429, "Too Many", {}, None
                )

        monkeypatch.setattr(install.urllib.request, "build_opener", lambda *handlers: FakeOpener())
        assert install._powershell_latest_version_from_redirect() is None

    def test_api_parses_tag_name(self, monkeypatch):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

            def read(self):
                return b'{"tag_name": "v7.7.0"}'

        monkeypatch.setattr(install.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
        assert install._powershell_latest_version_from_api() == "7.7.0"

    def test_api_rejects_payload_without_tag(self, monkeypatch):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

            def read(self):
                return b'{"message": "API rate limit exceeded"}'

        monkeypatch.setattr(install.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
        assert install._powershell_latest_version_from_api() is None


class TestGithubInstall:
    def test_unpacks_into_shared_bin(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIMI_SHARE_DIR", str(tmp_path))
        monkeypatch.setattr(install, "_powershell_arch", lambda: "x64")
        monkeypatch.setattr(install, "_powershell_latest_version", lambda: "7.7.0")
        requested: list[str] = []

        def fake_download(url: str, dest: Path) -> None:
            requested.append(url)
            _write_fake_pwsh_zip(dest)

        registered: list[Path] = []
        monkeypatch.setattr(install, "_download_powershell_archive", fake_download)
        monkeypatch.setattr(install, "_powershell_version", lambda _b: (7, 7, 0))
        monkeypatch.setattr(
            install,
            "_ensure_shared_bin_on_path",
            lambda bin_dir: registered.append(bin_dir) or True,
        )

        assert install._install_powershell_from_github() is True
        bin_dir = tmp_path / "bin"
        assert (bin_dir / "pwsh.exe").is_file()
        assert (bin_dir / "pwsh.dll").is_file()
        assert (bin_dir / "Modules" / "PSReadLine" / "PSReadLine.dll").is_file()
        assert requested == [install._powershell_download_url("7.7.0", "x64")]
        assert registered == [bin_dir]

    def test_retries_with_pinned_version_when_latest_is_missing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIMI_SHARE_DIR", str(tmp_path))
        monkeypatch.setattr(install, "_powershell_arch", lambda: "x64")
        monkeypatch.setattr(install, "_powershell_latest_version", lambda: "7.99.0")
        requested: list[str] = []

        def fake_download(url: str, dest: Path) -> None:
            requested.append(url)
            if "/v7.99.0/" in url:
                raise install.urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            _write_fake_pwsh_zip(dest)

        monkeypatch.setattr(install, "_download_powershell_archive", fake_download)
        monkeypatch.setattr(install, "_powershell_version", lambda _b: (7, 7, 0))
        monkeypatch.setattr(install, "_ensure_shared_bin_on_path", lambda _d: True)

        assert install._install_powershell_from_github() is True
        assert requested == [
            install._powershell_download_url("7.99.0", "x64"),
            install._powershell_download_url(install.POWERSHELL_FALLBACK_VERSION, "x64"),
        ]
        assert (tmp_path / "bin" / "pwsh.exe").is_file()

    def test_does_not_repeat_pinned_version(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIMI_SHARE_DIR", str(tmp_path))
        monkeypatch.setattr(install, "_powershell_arch", lambda: "x64")
        monkeypatch.setattr(
            install,
            "_powershell_latest_version",
            lambda: install.POWERSHELL_FALLBACK_VERSION,
        )
        monkeypatch.setattr(install, "_download_powershell_archive", _raise_urlerror)
        assert install._install_powershell_from_github() is False

    def test_rejects_unusable_binary(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("KIMI_SHARE_DIR", str(tmp_path))
        monkeypatch.setattr(install, "_powershell_arch", lambda: "x64")
        monkeypatch.setattr(install, "_powershell_latest_version", lambda: "7.7.0")

        def fake_download(_url: str, dest: Path) -> None:
            _write_fake_pwsh_zip(dest)

        monkeypatch.setattr(install, "_download_powershell_archive", fake_download)
        monkeypatch.setattr(install, "_powershell_version", lambda _b: None)
        monkeypatch.setattr(
            install,
            "_ensure_shared_bin_on_path",
            lambda _d: pytest.fail("an unusable install must not be registered"),
        )

        assert install._install_powershell_from_github() is False
        assert "not usable" in capsys.readouterr().out

    def test_unsupported_arch_fails_cleanly(self, monkeypatch, capsys):
        def bad_arch():
            raise RuntimeError("Unsupported architecture for PowerShell 7: riscv64")

        monkeypatch.setattr(install, "_powershell_arch", bad_arch)
        assert install._install_powershell_from_github() is False
        assert "Unsupported architecture" in capsys.readouterr().out


class TestWingetInstall:
    def test_missing_winget(self, monkeypatch):
        monkeypatch.setattr(install, "command_exists", lambda _cmd: False)
        assert install._install_powershell_with_winget() is False

    def test_success_refreshes_path_and_detects_pwsh(self, monkeypatch, tmp_path):
        class Result:
            returncode = 0

        calls: list[object] = []

        def fake_run(command, **_kwargs):
            calls.append(list(command))
            return Result()

        pwsh = tmp_path / "pwsh.exe"
        monkeypatch.setattr(install, "command_exists", lambda cmd: cmd == "winget")
        monkeypatch.setattr(install.subprocess, "run", fake_run)
        monkeypatch.setattr(
            install, "_refresh_windows_path_from_registry", lambda: calls.append("refresh")
        )
        monkeypatch.setattr(install, "_find_powershell7", lambda: (pwsh, (7, 7, 0)))

        assert install._install_powershell_with_winget() is True
        command = calls[0]
        assert command[:6] == [
            "winget",
            "install",
            "--id",
            "Microsoft.PowerShell",
            "--source",
            "winget",
        ]
        assert "--accept-package-agreements" in command
        assert "--accept-source-agreements" in command
        assert "refresh" in calls

    def test_non_zero_exit(self, monkeypatch):
        class Result:
            returncode = 1

        monkeypatch.setattr(install, "command_exists", lambda _cmd: True)
        monkeypatch.setattr(install.subprocess, "run", lambda *a, **k: Result())
        monkeypatch.setattr(
            install,
            "_refresh_windows_path_from_registry",
            lambda: pytest.fail("a failed winget run must not refresh PATH"),
        )
        assert install._install_powershell_with_winget() is False

    def test_timeout(self, monkeypatch):
        def fake_run(*_a, **_k):
            raise install.subprocess.TimeoutExpired(cmd="winget", timeout=1)

        monkeypatch.setattr(install, "command_exists", lambda _cmd: True)
        monkeypatch.setattr(install.subprocess, "run", fake_run)
        assert install._install_powershell_with_winget() is False

    def test_missing_executable(self, monkeypatch):
        def fake_run(*_a, **_k):
            raise FileNotFoundError("winget.exe")

        monkeypatch.setattr(install, "command_exists", lambda _cmd: True)
        monkeypatch.setattr(install.subprocess, "run", fake_run)
        assert install._install_powershell_with_winget() is False

    def test_success_but_no_pwsh_found(self, monkeypatch):
        class Result:
            returncode = 0

        monkeypatch.setattr(install, "command_exists", lambda _cmd: True)
        monkeypatch.setattr(install.subprocess, "run", lambda *a, **k: Result())
        monkeypatch.setattr(install, "_refresh_windows_path_from_registry", lambda: None)
        monkeypatch.setattr(install, "_find_powershell7", lambda: None)
        assert install._install_powershell_with_winget() is False


class TestInstallPowershell:
    def test_noop_off_windows(self, monkeypatch):
        monkeypatch.setattr(install.sys, "platform", "linux")
        monkeypatch.setattr(
            install,
            "_find_powershell7",
            lambda: pytest.fail("non-Windows platforms must not probe for pwsh"),
        )
        assert install._install_powershell() == (False, False)

    def test_skips_when_already_installed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(install.sys, "platform", "win32")
        monkeypatch.setattr(
            install, "_find_powershell7", lambda: (tmp_path / "pwsh.exe", (7, 6, 9))
        )
        monkeypatch.setattr(
            install,
            "_install_powershell_with_winget",
            lambda: pytest.fail("installed pwsh must not be reinstalled"),
        )
        monkeypatch.setattr(
            install,
            "_install_powershell_from_github",
            lambda: pytest.fail("installed pwsh must not be re-downloaded"),
        )
        assert install._install_powershell() == (False, False)

    def test_declined_prompt(self, monkeypatch):
        monkeypatch.setattr(install.sys, "platform", "win32")
        monkeypatch.setattr(install, "_find_powershell7", lambda: None)
        monkeypatch.setattr(install, "_ask_yes_no", lambda *a, **k: False)
        monkeypatch.setattr(
            install,
            "_install_powershell_with_winget",
            lambda: pytest.fail("declined install must not run winget"),
        )
        assert install._install_powershell() == (False, False)

    def test_winget_success(self, monkeypatch):
        monkeypatch.setattr(install.sys, "platform", "win32")
        monkeypatch.setattr(install, "_find_powershell7", lambda: None)
        monkeypatch.setattr(install, "_ask_yes_no", lambda *a, **k: True)
        monkeypatch.setattr(install, "_install_powershell_with_winget", lambda: True)
        monkeypatch.setattr(
            install,
            "_install_powershell_from_github",
            lambda: pytest.fail("the download fallback must not run after a winget success"),
        )
        assert install._install_powershell() == (True, True)

    def test_winget_failure_falls_back_to_download(self, monkeypatch):
        monkeypatch.setattr(install.sys, "platform", "win32")
        monkeypatch.setattr(install, "_find_powershell7", lambda: None)
        monkeypatch.setattr(install, "_ask_yes_no", lambda *a, **k: True)
        monkeypatch.setattr(install, "_install_powershell_with_winget", lambda: False)
        monkeypatch.setattr(install, "_install_powershell_from_github", lambda: True)
        assert install._install_powershell() == (True, True)

    def test_both_strategies_fail(self, monkeypatch, capsys):
        monkeypatch.setattr(install.sys, "platform", "win32")
        monkeypatch.setattr(install, "_find_powershell7", lambda: None)
        monkeypatch.setattr(install, "_ask_yes_no", lambda *a, **k: True)
        monkeypatch.setattr(install, "_install_powershell_with_winget", lambda: False)
        monkeypatch.setattr(install, "_install_powershell_from_github", lambda: False)
        assert install._install_powershell() == (False, False)
        assert "releases" in capsys.readouterr().out


class TestRefreshWindowsPath:
    def test_appends_missing_registry_entries(self, monkeypatch, tmp_path):
        existing = tmp_path / "existing"
        user_dir = tmp_path / "user-bin"
        system_dir = tmp_path / "system-bin"
        monkeypatch.setattr(install.sys, "platform", "win32")
        monkeypatch.setenv("PATH", str(existing))

        def fake_read(hive: str, _sub_key: str):
            if hive == "user":
                return f"{user_dir};{existing}", 2
            return str(system_dir), 2

        monkeypatch.setattr(install, "_read_registry_path", fake_read)
        install._refresh_windows_path_from_registry()

        entries = install.os.environ["PATH"].split(install.os.pathsep)
        assert entries[0] == str(existing)
        assert str(user_dir) in entries
        assert str(system_dir) in entries
        existing_entries = [
            entry
            for entry in entries
            if install.os.path.normcase(entry) == install.os.path.normcase(str(existing))
        ]
        assert len(existing_entries) == 1

    def test_noop_off_windows(self, monkeypatch):
        monkeypatch.setattr(install.sys, "platform", "linux")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setattr(
            install,
            "_read_registry_path",
            lambda *a, **k: pytest.fail("no registry on POSIX"),
        )
        install._refresh_windows_path_from_registry()
        assert install.os.environ["PATH"] == "/usr/bin:/bin"


class TestMainWiring:
    def test_powershell_runs_before_other_binary_installers(self, monkeypatch):
        order: list[str] = []

        def step(name: str):
            def run(*_args, **_kwargs):
                order.append(name)
                return (False, False)

            return run

        monkeypatch.setattr(install, "_ASSUME_YES", False)
        monkeypatch.setattr(install, "command_exists", lambda _cmd: True)
        for installer, label in (
            ("_install_coreutils", "coreutils"),
            ("_install_powershell", "pwsh"),
            ("_install_ripgrep", "rg"),
            ("_install_rtk", "rtk"),
            ("_install_git", "git"),
        ):
            monkeypatch.setattr(install, installer, step(label))
        monkeypatch.setattr(install, "_ask_yes_no", lambda *a, **k: False)
        monkeypatch.setattr(install, "_sync_kimix_native_version", lambda _v: True)
        monkeypatch.setattr(install, "_install_kimix_native", lambda: True)
        monkeypatch.setattr(install, "_ensure_tool_bin_on_path", lambda: True)

        assert install.main(["-y"]) == 0
        assert order == ["coreutils", "pwsh", "rg", "rtk", "git"]

    def test_fresh_powershell_install_asks_for_a_shell_restart(self, monkeypatch, capsys):
        monkeypatch.setattr(install, "_ASSUME_YES", False)
        monkeypatch.setattr(install, "command_exists", lambda _cmd: True)
        monkeypatch.setattr(install, "_install_coreutils", lambda: (False, False))
        monkeypatch.setattr(install, "_install_powershell", lambda: (True, True))
        monkeypatch.setattr(install, "_install_ripgrep", lambda: (False, False))
        monkeypatch.setattr(install, "_install_rtk", lambda: (False, False))
        monkeypatch.setattr(install, "_install_git", lambda: (False, False))
        monkeypatch.setattr(install, "_ask_yes_no", lambda *a, **k: False)
        monkeypatch.setattr(install, "_sync_kimix_native_version", lambda _v: True)
        monkeypatch.setattr(install, "_install_kimix_native", lambda: True)
        monkeypatch.setattr(install, "_ensure_tool_bin_on_path", lambda: True)

        assert install.main(["-y"]) == 0
        assert "restart your current shell" in capsys.readouterr().out


class TestDownloadRequest:
    def test_method_is_forwarded(self):
        request = install._download_request("https://example.invalid/x", method="HEAD")
        assert request.get_method() == "HEAD"

    def test_user_agent_and_extra_headers(self):
        request = install._download_request(
            "https://example.invalid/x", {"Accept": "application/octet-stream"}
        )
        assert request.get_header("User-agent") == "kimi-agent-install/1.0"
        assert request.get_header("Accept") == "application/octet-stream"
