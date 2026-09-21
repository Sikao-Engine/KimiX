"""Tests for Environment.detect() and git-bash resolution on Windows."""

from __future__ import annotations

import platform

import pytest
from kaos.path import KaosPath

from kimi_cli.utils import environment as env_mod
from kimi_cli.utils.environment import (
    Environment,
    GitBashNotFoundError,
    _find_git_bash_path,
    is_windows,
)


@pytest.fixture(autouse=True)
def _clear_which_cache():
    """Keep the memoized PATH scan from leaking between tests."""
    env_mod._which_all_cached.cache_clear()
    yield
    env_mod._which_all_cached.cache_clear()


def _patch_which(monkeypatch, mapping: dict[str, list[str]]):
    """Patch PATH lookups so ``exe`` resolves to the given candidates.

    Shell discovery is a pure-Python PATH scan (``_which_all``); faking it is
    both faster and more precise than spawning ``where.exe`` in tests.
    """

    def fake_which_all(exe: str, path: str | None = None) -> list[str]:
        return list(mapping.get(exe, []))

    monkeypatch.setattr(env_mod, "_which_all", fake_which_all)
    monkeypatch.setattr(env_mod, "_where_git_executables", lambda: list(mapping.get("git", [])))


@pytest.mark.skipif(platform.system() == "Windows", reason="Skipping test on Windows")
async def test_environment_detection_linux(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(platform, "version", lambda: "5.15.0-123-generic")

    async def _mock_is_file(self: KaosPath) -> bool:
        return str(self) == "/usr/bin/bash"

    monkeypatch.setattr(KaosPath, "is_file", _mock_is_file)

    env = await Environment.detect()
    assert env.os_kind == "Linux"
    assert env.os_arch == "x86_64"
    assert env.os_version == "5.15.0-123-generic"
    assert env.shell_name == "bash"
    assert str(env.shell_path) == "/usr/bin/bash"


@pytest.mark.skipif(platform.system() == "Windows", reason="Skipping test on Windows")
async def test_environment_detection_linux_falls_back_to_sh(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(platform, "version", lambda: "5.15.0")

    async def _mock_is_file(self: KaosPath) -> bool:
        return False  # No bash anywhere

    monkeypatch.setattr(KaosPath, "is_file", _mock_is_file)

    env = await Environment.detect()
    assert env.shell_name == "sh"
    assert str(env.shell_path) == "/bin/sh"


@pytest.mark.skipif(platform.system() == "Windows", reason="Skipping test on Windows")
async def test_environment_detection_windows_with_env_override(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(platform, "version", lambda: "10.0.19044")
    monkeypatch.setenv("KIMI_CLI_GIT_BASH_PATH", r"D:\custom\bash.exe")

    _patch_which(monkeypatch, {})

    async def _mock_is_file(self: KaosPath) -> bool:
        return str(self) == r"D:\custom\bash.exe"

    monkeypatch.setattr(KaosPath, "is_file", _mock_is_file)

    env = await Environment.detect()
    assert env.os_kind == "Windows"
    assert env.shell_name == "bash"
    assert str(env.shell_path) == r"D:\custom\bash.exe"


@pytest.mark.skipif(platform.system() == "Windows", reason="Skipping test on Windows")
async def test_invalid_git_bash_override_raises(monkeypatch):
    """An override that does not point at a file must be reported loudly.

    ``Environment.detect()`` deliberately swallows this so the session can
    still start with PowerShell, so the helper is asserted directly.
    """
    monkeypatch.setenv("KIMI_CLI_GIT_BASH_PATH", r"D:\nonexistent\bash.exe")

    async def _mock_is_file(self: KaosPath) -> bool:
        return False

    monkeypatch.setattr(KaosPath, "is_file", _mock_is_file)

    with pytest.raises(GitBashNotFoundError) as excinfo:
        await _find_git_bash_path()

    assert "KIMI_CLI_GIT_BASH_PATH" in str(excinfo.value)
    assert "D:\\nonexistent\\bash.exe" in str(excinfo.value)


@pytest.mark.skipif(platform.system() == "Windows", reason="Skipping test on Windows")
async def test_environment_detection_windows_via_git_path(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(platform, "version", lambda: "10.0.19044")
    monkeypatch.delenv("KIMI_CLI_GIT_BASH_PATH", raising=False)

    # ``git.exe`` lives in <Git>\cmd, bash.exe in <Git>\bin.
    _patch_which(monkeypatch, {"git": [r"C:\Program Files\Git\cmd\git.exe"]})

    expected_bash = r"C:\Program Files\Git\cmd\..\bin\bash.exe"

    async def _mock_is_file(self: KaosPath) -> bool:
        return str(self) == expected_bash

    monkeypatch.setattr(KaosPath, "is_file", _mock_is_file)

    env = await Environment.detect()
    assert env.shell_name == "bash"
    assert str(env.shell_path) == expected_bash


@pytest.mark.skipif(platform.system() == "Windows", reason="Skipping test on Windows")
async def test_find_git_bash_path_resolves_mingw64_layout_without_git_subprocess(monkeypatch):
    """<Git>\\mingw64\\bin\\git.exe must resolve via the ancestor scan.

    Git for Windows puts the mingw64 directory first on PATH, and its
    ``<Git>\\bin\\bash.exe`` sibling is two levels up. Finding it this way
    avoids the very slow ``git --exec-path`` subprocess.
    """
    monkeypatch.delenv("KIMI_CLI_GIT_BASH_PATH", raising=False)

    _patch_which(monkeypatch, {"git": [r"C:\Program Files\Git\mingw64\bin\git.exe"]})

    expected_bash = r"C:\Program Files\Git\bin\bash.exe"

    async def _mock_is_file(self: KaosPath) -> bool:
        return str(self) == expected_bash

    monkeypatch.setattr(KaosPath, "is_file", _mock_is_file)

    # Fail loudly if the resolution ever falls back to spawning git.
    def _boom(git_path: str) -> str | None:
        raise AssertionError("git --exec-path must not be spawned for a standard install")

    monkeypatch.setattr(env_mod, "_git_exec_path", _boom)

    path = await _find_git_bash_path()
    assert str(path) == expected_bash


@pytest.mark.skipif(platform.system() == "Windows", reason="Skipping test on Windows")
async def test_environment_detection_windows_checks_all_git_matches(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(platform, "version", lambda: "10.0.19044")
    monkeypatch.delenv("KIMI_CLI_GIT_BASH_PATH", raising=False)

    shim_git = r"C:\Users\me\scoop\shims\git.exe"
    _patch_which(
        monkeypatch,
        {"git": [shim_git, r"C:\Program Files\Git\cmd\git.exe"]},
    )
    # The Scoop shim is broken: asking it for --exec-path fails.
    monkeypatch.setattr(env_mod, "_git_exec_path", lambda git_path: None)

    expected_bash = r"C:\Program Files\Git\cmd\..\bin\bash.exe"

    async def _mock_is_file(self: KaosPath) -> bool:
        return str(self) == expected_bash

    monkeypatch.setattr(KaosPath, "is_file", _mock_is_file)

    env = await Environment.detect()
    assert env.shell_name == "bash"
    assert str(env.shell_path) == expected_bash


@pytest.mark.skipif(platform.system() == "Windows", reason="Skipping test on Windows")
async def test_environment_detection_windows_resolves_shim_only_git(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(platform, "version", lambda: "10.0.19044")
    monkeypatch.delenv("KIMI_CLI_GIT_BASH_PATH", raising=False)

    shim_git = r"C:\Users\me\scoop\shims\git.exe"
    _patch_which(monkeypatch, {"git": [shim_git]})

    def fake_exec_path(git_path: str) -> str | None:
        assert git_path == shim_git
        return "C:/Users/me/scoop/apps/git/current/mingw64/libexec/git-core"

    monkeypatch.setattr(env_mod, "_git_exec_path", fake_exec_path)

    expected_bash = r"C:\Users\me\scoop\apps\git\current\bin\bash.exe"

    async def _mock_is_file(self: KaosPath) -> bool:
        return str(self) == expected_bash

    monkeypatch.setattr(KaosPath, "is_file", _mock_is_file)

    env = await Environment.detect()
    assert env.shell_name == "bash"
    assert str(env.shell_path) == expected_bash


@pytest.mark.skipif(platform.system() == "Windows", reason="Skipping test on Windows")
async def test_environment_detection_windows_default_install_location(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(platform, "version", lambda: "10.0.19044")
    monkeypatch.delenv("KIMI_CLI_GIT_BASH_PATH", raising=False)

    _patch_which(monkeypatch, {})  # git is not on PATH at all

    fallback = r"C:\Program Files\Git\bin\bash.exe"

    async def _mock_is_file(self: KaosPath) -> bool:
        return str(self) == fallback

    monkeypatch.setattr(KaosPath, "is_file", _mock_is_file)

    env = await Environment.detect()
    assert env.shell_name == "bash"
    assert str(env.shell_path) == fallback


@pytest.mark.skipif(platform.system() == "Windows", reason="Skipping test on Windows")
async def test_environment_detection_windows_no_git_bash_anywhere(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(platform, "version", lambda: "10.0.19044")
    monkeypatch.delenv("KIMI_CLI_GIT_BASH_PATH", raising=False)

    _patch_which(monkeypatch, {})

    async def _mock_is_file(self: KaosPath) -> bool:
        return False

    monkeypatch.setattr(KaosPath, "is_file", _mock_is_file)

    env = await Environment.detect()
    assert env.shell_name == "powershell"
    assert str(env.shell_path) == "powershell.exe"


@pytest.mark.skipif(platform.system() == "Windows", reason="Skipping test on Windows")
async def test_environment_detection_windows_prefers_pwsh(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(platform, "version", lambda: "10.0.19044")
    monkeypatch.delenv("KIMI_CLI_GIT_BASH_PATH", raising=False)

    pwsh_path = r"C:\Program Files\PowerShell\7\pwsh.exe"
    _patch_which(monkeypatch, {"pwsh": [pwsh_path]})

    async def _mock_is_file(self: KaosPath) -> bool:
        return str(self) == pwsh_path

    monkeypatch.setattr(KaosPath, "is_file", _mock_is_file)

    env = await Environment.detect()
    assert env.shell_name == "pwsh"
    assert str(env.shell_path) == pwsh_path


@pytest.mark.skipif(platform.system() == "Windows", reason="Skipping test on Windows")
async def test_environment_detection_windows_falls_back_to_powershell(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(platform, "version", lambda: "10.0.19044")
    monkeypatch.delenv("KIMI_CLI_GIT_BASH_PATH", raising=False)

    ps_path = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    _patch_which(monkeypatch, {"powershell": [ps_path]})

    async def _mock_is_file(self: KaosPath) -> bool:
        return str(self) == ps_path

    monkeypatch.setattr(KaosPath, "is_file", _mock_is_file)

    env = await Environment.detect()
    assert env.shell_name == "powershell"
    assert str(env.shell_path) == ps_path


@pytest.mark.skipif(platform.system() == "Windows", reason="Skipping test on Windows")
async def test_environment_detection_windows_falls_back_to_git_bash(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(platform, "version", lambda: "10.0.19044")
    monkeypatch.delenv("KIMI_CLI_GIT_BASH_PATH", raising=False)

    _patch_which(monkeypatch, {})

    fallback = r"C:\Program Files\Git\bin\bash.exe"

    async def _mock_is_file(self: KaosPath) -> bool:
        return str(self) == fallback

    monkeypatch.setattr(KaosPath, "is_file", _mock_is_file)

    env = await Environment.detect()
    assert env.shell_name == "bash"
    assert str(env.shell_path) == fallback


@pytest.mark.skipif(platform.system() == "Windows", reason="Skipping test on Windows")
async def test_environment_detection_windows_ultimate_fallback(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(platform, "version", lambda: "10.0.19044")
    monkeypatch.delenv("KIMI_CLI_GIT_BASH_PATH", raising=False)

    _patch_which(monkeypatch, {})

    async def _mock_find_git_bash_path():
        raise GitBashNotFoundError("not found")

    monkeypatch.setattr(env_mod, "_find_git_bash_path", _mock_find_git_bash_path)

    async def _mock_is_file(self: KaosPath) -> bool:
        return False

    monkeypatch.setattr(KaosPath, "is_file", _mock_is_file)

    env = await Environment.detect()
    assert env.shell_name == "powershell"
    assert str(env.shell_path) == "powershell.exe"


@pytest.mark.skipif(platform.system() == "Windows", reason="Skipping test on Windows")
async def test_find_git_bash_path_directly(monkeypatch):
    """Direct unit test for the helper, without going through Environment.detect()."""
    monkeypatch.setenv("KIMI_CLI_GIT_BASH_PATH", r"E:\git\bash.exe")

    async def _mock_is_file(self: KaosPath) -> bool:
        return str(self) == r"E:\git\bash.exe"

    monkeypatch.setattr(KaosPath, "is_file", _mock_is_file)

    path = await _find_git_bash_path()
    assert str(path) == r"E:\git\bash.exe"


def test_is_windows_reflects_platform_system(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    assert is_windows() is True
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    assert is_windows() is False
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    assert is_windows() is False
