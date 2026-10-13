"""Comprehensive tests for the Bash tool (bash_tool.py) which uses the system bash executable."""

import asyncio
import ntpath
import os
import shutil
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kimi_cli.session import Session
from kimi_cli.tools import SkipThisTool

from kimi_agent_sdk import ToolError, ToolOk
from kimi_cli.tools.background import TaskOutput, TaskOutputParams
from kimi_cli.tools.background.utils import TaskData, _pop_task_data, get_all_tasks
from kimi_cli.tools.common import _env_with_rg_bin_path
from kimi_cli.tools.file.bash import (
    Bash,
    BashParams,
    Powershell,
)
from kimi_cli.tools.file.bash.bash_fix import BashFix, fix_bash_command
from kimi_cli.tools.file.bash.bash_tool import (
    _BASH_EXTERNAL_PROGRAM_PROBE,
    _bash_runs,
    _bash_subprocess_env,
    _configured_shell,
    _find_git_bash_windows,
    _git_bash_candidate_from_git_path,
    _git_bash_candidates_from_exec_path,
    _git_install_root_from_exec_path,
    _is_git_bash_install,
    _prepare_bash_cmd,
    _with_msystem_neutralized,
    find_bash,
)
from kimi_cli.tools.file.bash.pwsh_tool import PowershellParams, find_pwsh
from kimi_cli.native_loader import get_compat as _get_compat

# Round-0 review regressions target the canonical pure-Python scanner in the
# kimix_native shim directly (same module ``bash_fix`` re-exports).
_shell_compat = _get_compat("_shell_compat")


def _bash_is_available() -> bool:
    """Return True when a real bash executable exists on this host.

    Host-capability probe: the agent config's ``shell`` selection is ignored
    so the result depends only on whether bash is actually installed (the
    default Windows policy is Git-Bash-first with PowerShell as fallback, so
    real sessions enable the Bash tool wherever this probe is True).
    """
    return find_bash() is not None


def _pwsh_is_available() -> bool:
    """Return True when a real PowerShell can run on this host.

    Host-capability probe (Windows only, mirroring the tool's platform gate):
    PowerShell 7 via ``find_pwsh`` or the Windows PowerShell fallback.
    """
    if sys.platform != "win32":
        return False
    if find_pwsh() is not None:
        return True
    return (
        shutil.which("powershell.exe") is not None
        or shutil.which("powershell") is not None
    )


BASH_AVAILABLE = _bash_is_available()
PWSH_AVAILABLE = _pwsh_is_available()


def _bash_version() -> tuple[int, ...] | None:
    """Return the installed bash version as a tuple, or None if unknown.

    The host's *real* bash (``find_bash()``) is queried, never the bare
    ``bash`` name: on Windows ``CreateProcess`` resolves that name through
    ``C:\\Windows\\System32`` before ``PATH``, so it picks up the WSL/Store
    stub (which exits 1 and prints a UTF-16 "no distributions" banner)
    even when Git Bash is installed and first on ``PATH``.
    """
    if not BASH_AVAILABLE:
        return None
    bash = find_bash()
    if bash is None:
        return None
    try:
        proc = subprocess.run(
            [bash, "-c", "echo ${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"],
            capture_output=True,
            timeout=15,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return tuple(int(part) for part in proc.stdout.strip().split("."))
    except ValueError:
        return None


# bash 5.3 (released 2025) started accepting a heredoc delimiter line
# followed by operators such as ``&&`` — older versions reject it with a
# syntax error.  The ``_fix_for_windows`` rewrite only matters on < 5.3.
BASH_REJECTS_HEREDOC_TRAILING_OPERATOR = (_bash_version() or (99,)) < (5, 3)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_session() -> MagicMock:
    session = MagicMock(spec=Session)
    session.custom_data = {}
    return session


@pytest.fixture(autouse=True)
def cleanup_task_data(mock_session: MagicMock) -> Any:
    yield
    _pop_task_data(mock_session)


# ============================================================================
# find_bash
# ============================================================================

class TestFindBash:
    def test_returns_path_on_this_system(self) -> None:
        path = find_bash()
        assert path is not None
        assert Path(path).exists()

    def test_returns_basename_bash(self) -> None:
        path = find_bash()
        assert path is not None
        assert Path(path).name.lower() in ("bash.exe", "bash")


class TestFindGitBashWindows:
    def test_git_bash_candidate_from_git_path(self) -> None:
        candidate = _git_bash_candidate_from_git_path(r"C:\Program Files\Git\cmd\git.exe")
        assert str(candidate) == r"C:\Program Files\Git\bin\bash.exe"

    def test_install_root_from_exec_path(self) -> None:
        assert (
            _git_install_root_from_exec_path(r"C:\Program Files\Git\mingw64\libexec\git-core")
            == r"C:\Program Files\Git"
        )
        assert _git_install_root_from_exec_path(r"C:\some\random\path") is None

    def test_bash_candidates_from_exec_path(self) -> None:
        candidates = _git_bash_candidates_from_exec_path(
            r"C:\Program Files\Git\mingw64\libexec\git-core"
        )
        assert [str(c) for c in candidates] == [r"C:\Program Files\Git\bin\bash.exe"]

        candidates = _git_bash_candidates_from_exec_path(r"C:\Program Files\Git\libexec\git-core")
        assert [str(c) for c in candidates] == [r"C:\Program Files\Git\bin\bash.exe"]

    def test_honors_env_override(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("KIMIX_GIT_BASH_PATH", r"C:\Custom\Git\bin\bash.exe")
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.Path.exists",
            lambda self: str(self) == r"C:\Custom\Git\bin\bash.exe",
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool.shutil.which",
            return_value=None,
        ), patch(
            # ``Path.resolve`` prefixes the CWD for drive-less paths on
            # POSIX hosts; keep it an identity so Windows path strings
            # round-trip unchanged on any platform.
            "kimi_cli.tools.file.bash.bash_tool.Path.resolve",
            lambda self: self,
        ), patch(
            # The candidate must also pass the --version smoke test.
            "kimi_cli.tools.file.bash.bash_tool._bash_runs",
            return_value=True,
        ):
            assert _find_git_bash_windows() == r"C:\Custom\Git\bin\bash.exe"

    def test_env_override_missing_file_ignored(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("KIMIX_GIT_BASH_PATH", r"C:\Custom\Git\bin\bash.exe")
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.Path.exists",
            lambda self: str(self) == r"C:\Program Files\Git\bin\bash.exe",
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool._where_git_executables",
            return_value=[r"C:\Program Files\Git\cmd\git.exe"],
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool._git_exec_path",
            return_value=None,
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool.shutil.which",
            return_value=None,
        ), patch(
            # See test_honors_env_override: keep resolve() an identity so
            # Windows path strings round-trip unchanged on any platform.
            "kimi_cli.tools.file.bash.bash_tool.Path.resolve",
            lambda self: self,
        ), patch(
            # The candidate must also pass the --version smoke test.
            "kimi_cli.tools.file.bash.bash_tool._bash_runs",
            return_value=True,
        ):
            assert _find_git_bash_windows() == r"C:\Program Files\Git\bin\bash.exe"

    def test_windowsapps_bash_stub_ignored(self, monkeypatch: Any) -> None:
        """A WindowsApps ``bash.exe`` is only a Store stub (installs WSL).

        With no git install the stub must not count as bash, so PowerShell
        becomes the fallback shell.
        """
        monkeypatch.delenv("KIMIX_GIT_BASH_PATH", raising=False)
        stub = r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\bash.exe"
        with patch(
            "kimi_cli.tools.file.bash.bash_tool._where_git_executables",
            return_value=[],
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool.shutil.which",
            return_value=stub,
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool.Path.exists",
            lambda self: False,
        ), patch(
            # See test_honors_env_override: keep resolve() an identity so
            # Windows path strings round-trip unchanged on any platform.
            "kimi_cli.tools.file.bash.bash_tool.Path.resolve",
            lambda self: self,
        ):
            assert _find_git_bash_windows() is None

    def test_real_bash_on_path_still_accepted(self, monkeypatch: Any) -> None:
        """A genuine (non-stub) bash on PATH is still a valid bash."""
        monkeypatch.delenv("KIMIX_GIT_BASH_PATH", raising=False)
        real = r"C:\msys64\usr\bin\bash.exe"
        with patch(
            "kimi_cli.tools.file.bash.bash_tool._where_git_executables",
            return_value=[],
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool.shutil.which",
            return_value=real,
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool.Path.exists",
            lambda self: False,
        ), patch(
            # See test_honors_env_override: keep resolve() an identity so
            # Windows path strings round-trip unchanged on any platform.
            "kimi_cli.tools.file.bash.bash_tool.Path.resolve",
            lambda self: self,
        ), patch(
            # The PATH candidate must also pass the --version smoke test.
            "kimi_cli.tools.file.bash.bash_tool._bash_runs",
            return_value=True,
        ):
            assert _find_git_bash_windows() == real

    def test_invalid_git_bash_falls_through_to_valid_path_bash(
        self, monkeypatch: Any
    ) -> None:
        """An existing-but-broken git bash is skipped; a working bash on PATH
        is used instead — only a fully unusable bash drops to PowerShell."""
        monkeypatch.delenv("KIMIX_GIT_BASH_PATH", raising=False)
        broken = r"C:\Program Files\Git\bin\bash.exe"
        good = r"C:\msys64\usr\bin\bash.exe"
        with patch(
            "kimi_cli.tools.file.bash.bash_tool._where_git_executables",
            return_value=[r"C:\Program Files\Git\cmd\git.exe"],
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool._git_exec_path",
            return_value=None,
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool.Path.exists",
            lambda self: str(self) in (broken, good),
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool._bash_runs",
            side_effect=lambda p: p == good,
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool.shutil.which",
            return_value=good,
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool.Path.resolve",
            lambda self: self,
        ):
            assert _find_git_bash_windows() == good

    def test_all_bash_candidates_invalid_returns_none(
        self, monkeypatch: Any
    ) -> None:
        """Every candidate exists but none can run: report "no bash" so
        PowerShell becomes the fallback shell."""
        monkeypatch.delenv("KIMIX_GIT_BASH_PATH", raising=False)
        with patch(
            "kimi_cli.tools.file.bash.bash_tool._where_git_executables",
            return_value=[r"C:\Program Files\Git\cmd\git.exe"],
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool._git_exec_path",
            return_value=None,
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool.Path.exists",
            lambda self: True,
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool._bash_runs",
            return_value=False,
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool.shutil.which",
            return_value=None,
        ):
            assert _find_git_bash_windows() is None

    def test_bash_runs_smoke_test(self, monkeypatch: Any) -> None:
        """The smoke test trusts the exit status of an external-program probe:
        exit 0 counts as bash; a non-zero exit, a timeout, or a missing binary
        does not."""
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(
            "kimi_cli.tools.file.bash.bash_tool.subprocess.run", fake_run
        )
        assert _bash_runs("bash") is True
        assert calls == [
            ["bash", "--noprofile", "--norc", "-c", _BASH_EXTERNAL_PROGRAM_PROBE]
        ]

        def fake_run_failing(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 1, "", "")

        monkeypatch.setattr(
            "kimi_cli.tools.file.bash.bash_tool.subprocess.run", fake_run_failing
        )
        assert _bash_runs("bash") is False
        assert _bash_runs(r"C:\definitely\missing\bash.exe") is False

    def test_bash_runs_probe_launches_external_programs(self) -> None:
        """The probe must launch external MSYS programs, not just builtins.

        A builtin-only probe (``--version``) passes even when Git for Windows
        cannot fork children under system-wide Mandatory ASLR, while every
        real command fails; the external-program probe rejects such a broken
        bash so PowerShell becomes the fallback shell.
        """
        assert "/usr/bin/true" in _BASH_EXTERNAL_PROGRAM_PROBE
        assert "/usr/bin/cat" in _BASH_EXTERNAL_PROGRAM_PROBE

    def test_bash_runs_timeout(self, monkeypatch: Any) -> None:
        def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=15)

        monkeypatch.setattr(
            "kimi_cli.tools.file.bash.bash_tool.subprocess.run", fake_run
        )
        assert _bash_runs("bash") is False


# ============================================================================
# _bash_subprocess_env — MSYS argv-conversion opt-out (Windows)
# ============================================================================

class TestBashSubprocessEnv:
    def test_win32_sets_msys_opt_out(self, monkeypatch: Any) -> None:
        """On Windows the bash env opts out of MSYS argv path conversion.

        Without this, Git Bash rewrites slash-prefixed arguments (``/FO``,
        ``/TN``) of native tools such as ``tasklist``/``schtasks`` into
        ``C:/.../git/FO``-style paths.
        """
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.delenv("MSYS_NO_PATHCONV", raising=False)
        monkeypatch.delenv("MSYS2_ARG_CONV_EXCL", raising=False)
        env = _bash_subprocess_env()
        assert env["MSYS_NO_PATHCONV"] == "1"
        assert env["MSYS2_ARG_CONV_EXCL"] == "*"

    def test_respects_user_overrides(self, monkeypatch: Any) -> None:
        """Explicit user settings win over the opt-out defaults (setdefault)."""
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("MSYS_NO_PATHCONV", "0")
        monkeypatch.setenv("MSYS2_ARG_CONV_EXCL", "/dev")
        env = _bash_subprocess_env()
        assert env["MSYS_NO_PATHCONV"] == "0"
        assert env["MSYS2_ARG_CONV_EXCL"] == "/dev"

    def test_posix_untouched(self, monkeypatch: Any) -> None:
        """Off Windows no MSYS variables are injected."""
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.delenv("MSYS_NO_PATHCONV", raising=False)
        monkeypatch.delenv("MSYS2_ARG_CONV_EXCL", raising=False)
        env = _bash_subprocess_env()
        assert "MSYS_NO_PATHCONV" not in env
        assert "MSYS2_ARG_CONV_EXCL" not in env

    def test_delegates_to_rg_bin_path(self, monkeypatch: Any) -> None:
        """The bash env builds on ``_env_with_rg_bin_path`` (shared bin dir first)."""
        monkeypatch.delenv("MSYS_NO_PATHCONV", raising=False)
        monkeypatch.delenv("MSYS2_ARG_CONV_EXCL", raising=False)
        env = _bash_subprocess_env()
        base = _env_with_rg_bin_path()
        assert env["PATH"] == base["PATH"]

# ============================================================================
# Bash / Powershell mutual exclusion on Windows
# ============================================================================

class TestWindowsShellExclusion:
    @pytest.fixture
    def mock_session(self) -> MagicMock:
        session = MagicMock()
        session.custom_config.get.return_value = {}
        session.custom_data = {}
        return session

    def _platform_patchers(self, bash_available: bool, pwsh_preferred: bool) -> list[Any]:
        return [
            patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"),
            patch("kimi_cli.tools.file.bash.bash_tool.USE_SYSTEM_PWSH_ON_WINDOWS", pwsh_preferred),
            patch(
                "kimi_cli.tools.file.bash.bash_tool.find_bash",
                return_value=(r"C:\Git\bin\bash.exe" if bash_available else None),
            ),
            # No `agent.shell` config: exercise the legacy platform heuristics.
            patch("kimi_cli.tools.file.bash.bash_tool._configured_shell", return_value=None),
        ]

    def _with_platform(self, bash_available: bool, pwsh_preferred: bool) -> ExitStack:
        stack = ExitStack()
        for cm in self._platform_patchers(bash_available, pwsh_preferred):
            stack.enter_context(cm)
        return stack

    def test_bash_enabled_powershell_disabled_when_git_bash_available(
        self, mock_session: MagicMock
    ) -> None:
        with self._with_platform(bash_available=True, pwsh_preferred=False):
            Bash(mock_session)  # does not raise
            with pytest.raises(SkipThisTool):
                Powershell(mock_session)

    def test_powershell_enabled_bash_disabled_when_git_bash_missing(
        self, mock_session: MagicMock
    ) -> None:
        with self._with_platform(bash_available=False, pwsh_preferred=False):
            Powershell(mock_session)  # does not raise
            with pytest.raises(SkipThisTool):
                Bash(mock_session)

    def test_powershell_enabled_bash_disabled_when_pwsh_preferred(
        self, mock_session: MagicMock
    ) -> None:
        with self._with_platform(bash_available=True, pwsh_preferred=True):
            Powershell(mock_session)  # does not raise
            with pytest.raises(SkipThisTool):
                Bash(mock_session)

    def test_default_flag_prefers_bash_on_windows(
        self, mock_session: MagicMock
    ) -> None:
        """Shipped default: Git Bash is preferred on Windows; PowerShell is
        only the fallback when no bash (no git install) exists."""
        import kimi_cli.tools.file.bash.bash_tool as bash_tool_module

        assert bash_tool_module.USE_SYSTEM_PWSH_ON_WINDOWS is False
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"), patch(
            "kimi_cli.tools.file.bash.bash_tool._configured_shell", return_value=None
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool.find_bash",
            return_value=r"C:\Git\bin\bash.exe",
        ):
            Bash(mock_session)  # does not raise
            with pytest.raises(SkipThisTool):
                Powershell(mock_session)

    def test_default_flag_falls_back_to_powershell_without_bash(
        self, mock_session: MagicMock
    ) -> None:
        """Shipped default: no git install (no bash) on Windows → PowerShell
        becomes the fallback shell tool."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"), patch(
            "kimi_cli.tools.file.bash.bash_tool._configured_shell", return_value=None
        ), patch("kimi_cli.tools.file.bash.bash_tool.find_bash", return_value=None):
            Powershell(mock_session)  # does not raise
            with pytest.raises(SkipThisTool):
                Bash(mock_session)


# ============================================================================
# _configured_shell — reading agent.shell from the agent config file
# ============================================================================

class TestConfiguredShellConfigRead:
    """Unit tests for reading the `agent.shell` config key."""

    def _patch_agent_file(self, tmp_path: Path, content: str) -> Any:
        agent_file = tmp_path / "agent.json"
        agent_file.write_text(content, encoding="utf-8")
        return patch("kimix.base._default_agent_file", agent_file)

    def test_reads_powershell_value(self, tmp_path: Path) -> None:
        with self._patch_agent_file(tmp_path, '{"agent": {"shell": "powershell"}}'):
            assert _configured_shell() == "powershell"

    def test_reads_bash_value(self, tmp_path: Path) -> None:
        with self._patch_agent_file(tmp_path, '{"agent": {"shell": "bash"}}'):
            assert _configured_shell() == "bash"

    def test_pwsh_alias_normalized(self, tmp_path: Path) -> None:
        with self._patch_agent_file(tmp_path, '{"agent": {"shell": "pwsh"}}'):
            assert _configured_shell() == "powershell"

    def test_value_is_case_insensitive(self, tmp_path: Path) -> None:
        with self._patch_agent_file(tmp_path, '{"agent": {"shell": "PowerShell"}}'):
            assert _configured_shell() == "powershell"

    def test_missing_key_returns_none(self, tmp_path: Path) -> None:
        with self._patch_agent_file(tmp_path, '{"agent": {}}'):
            assert _configured_shell() is None

    def test_unknown_value_returns_none(self, tmp_path: Path) -> None:
        with self._patch_agent_file(tmp_path, '{"agent": {"shell": "cmd"}}'):
            assert _configured_shell() is None

    def test_non_string_value_returns_none(self, tmp_path: Path) -> None:
        with self._patch_agent_file(tmp_path, '{"agent": {"shell": 42}}'):
            assert _configured_shell() is None

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing.json"
        with patch("kimix.base._default_agent_file", missing):
            assert _configured_shell() is None

    def test_invalid_json_returns_none(self, tmp_path: Path) -> None:
        with self._patch_agent_file(tmp_path, "not json"):
            assert _configured_shell() is None


# ============================================================================
# Config-driven shell selection (agent.shell key)
# ============================================================================

class TestConfiguredShellSelection:
    """The `agent.shell` config key selects Bash or Powershell over the legacy
    platform heuristics."""

    @pytest.fixture
    def mock_session(self) -> MagicMock:
        session = MagicMock()
        session.custom_config.get.return_value = {}
        session.custom_data = {}
        return session

    def _with_platform(self, platform: str, bash_path: str | None) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", platform))
        stack.enter_context(
            patch("kimi_cli.tools.file.bash.bash_tool.find_bash", return_value=bash_path)
        )
        return stack

    def test_windows_config_powershell_enables_pwsh_disables_bash(
        self, mock_session: MagicMock
    ) -> None:
        with self._with_platform("win32", r"C:\Git\bin\bash.exe"), patch(
            "kimi_cli.tools.file.bash.bash_tool._configured_shell", return_value="powershell"
        ):
            Powershell(mock_session)  # does not raise
            with pytest.raises(SkipThisTool):
                Bash(mock_session)

    def test_windows_config_bash_enables_bash_disables_pwsh(
        self, mock_session: MagicMock
    ) -> None:
        with self._with_platform("win32", r"C:\Git\bin\bash.exe"), patch(
            "kimi_cli.tools.file.bash.bash_tool._configured_shell", return_value="bash"
        ):
            Bash(mock_session)  # does not raise
            with pytest.raises(SkipThisTool):
                Powershell(mock_session)

    def test_windows_config_bash_overrides_pwsh_preferred_flag(
        self, mock_session: MagicMock
    ) -> None:
        with self._with_platform("win32", r"C:\Git\bin\bash.exe"), patch(
            "kimi_cli.tools.file.bash.bash_tool.USE_SYSTEM_PWSH_ON_WINDOWS", True
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool._configured_shell", return_value="bash"
        ):
            Bash(mock_session)  # does not raise
            with pytest.raises(SkipThisTool):
                Powershell(mock_session)

    def test_non_windows_config_powershell_falls_back_to_bash(
        self, mock_session: MagicMock
    ) -> None:
        with self._with_platform("linux", "/bin/bash"), patch(
            "kimi_cli.tools.file.bash.bash_tool._configured_shell", return_value="powershell"
        ):
            # Powershell is a Windows-only tool: Bash remains the fallback.
            Bash(mock_session)  # does not raise
            with pytest.raises(SkipThisTool):
                Powershell(mock_session)

    def test_non_windows_config_bash_enables_bash(
        self, mock_session: MagicMock
    ) -> None:
        with self._with_platform("linux", "/bin/bash"), patch(
            "kimi_cli.tools.file.bash.bash_tool._configured_shell", return_value="bash"
        ):
            Bash(mock_session)  # does not raise
            with pytest.raises(SkipThisTool):
                Powershell(mock_session)

    def test_windows_config_powershell_without_bash_installed(
        self, mock_session: MagicMock
    ) -> None:
        with self._with_platform("win32", None), patch(
            "kimi_cli.tools.file.bash.bash_tool._configured_shell", return_value="powershell"
        ):
            Powershell(mock_session)  # does not raise
            with pytest.raises(SkipThisTool):
                Bash(mock_session)

    def test_windows_config_bash_without_bash_falls_back_to_pwsh(
        self, mock_session: MagicMock
    ) -> None:
        # Bash is configured but not installed (e.g. no Git Bash): PowerShell
        # becomes the fallback shell tool on Windows.
        with self._with_platform("win32", None), patch(
            "kimi_cli.tools.file.bash.bash_tool._configured_shell", return_value="bash"
        ):
            Powershell(mock_session)  # does not raise
            with pytest.raises(SkipThisTool):
                Bash(mock_session)


# ============================================================================
# BashParams
# ============================================================================


# ============================================================================
# Native POSIX command compatibility for Windows Git Bash
# ============================================================================


def _fix_for_platform(command: str, platform: str) -> BashFix:
    with patch("kimi_cli.tools.file.bash.bash_fix.sys.platform", platform):
        return fix_bash_command(command)


def _fix_for_windows(command: str) -> BashFix:
    return _fix_for_platform(command, "win32")


def _decode_startup_payload(process_task_call_args: Any) -> str:
    """Decode the base64+gzip payload used to deliver interactive startups.

    ``bash_tool`` carries the startup script (compatibility prelude + initial
    command) in the ``KIMIX_BASH_PAYLOAD`` environment entry because the
    decoded script is far beyond the MSYS2 ``bash -c`` argv length limit; the
    ``bash -c`` string itself only references the variable (the payload's
    first line unsets it).  Tests reverse the wrapping to assert on the
    original script text.
    """
    env = process_task_call_args.args[3]
    payload = env["KIMIX_BASH_PAYLOAD"]
    import base64 as _b64
    import gzip as _gz

    return _gz.decompress(_b64.b64decode(payload)).decode("utf-8")


class TestBashFixResult:
    def test_result_is_immutable(self) -> None:
        result = BashFix("echo ok")
        with pytest.raises((AttributeError, TypeError)):
            result.command = "echo changed"  # type: ignore[misc]

    def test_unchanged_result(self) -> None:
        result = _fix_for_windows("echo ok")
        assert result == BashFix("echo ok")
        assert result.command == "echo ok"
        assert result.replacements == ()
        assert result.warning == ""
        assert not result.changed

    def test_changed_result_reports_every_command(self) -> None:
        result = _fix_for_windows("gtimeout 1 true; printf x | rev")
        assert result.replacements == ("gtimeout", "rev")
        assert result.changed
        assert "gtimeout" in result.warning
        assert "rev" in result.warning

    @pytest.mark.parametrize("command", ["", " ", "\t\n", "echo ok\n"])
    def test_empty_and_plain_inputs_round_trip(self, command: str) -> None:
        assert _fix_for_windows(command).command == command

    @pytest.mark.parametrize("platform", ["linux", "darwin", "freebsd", "cygwin"])
    @pytest.mark.parametrize(
        "command",
        [
            "gtimeout 1 true",
            "printf abc | rev",
            "xdg-open .",
            "open README.md",
              "printf text | pbcopy",
              "pbpaste",
              "wget https://example.com/f.zip",
              "xclip -selection clipboard",
              "xsel -bo",
              "gsed -n 1p file",
              "zip -r out.zip dir",
              "nc -z example.com 80",
              "pgrep bash",
              "tree -L 1 dir",
              "say hello",
              "wl-copy < file",
              "python3 --version",
          ],
    )
    def test_non_windows_is_byte_for_byte_noop(self, platform: str, command: str) -> None:
        result = _fix_for_platform(command, platform)
        assert result == BashFix(command)


class TestBashFixMappings:
    @pytest.mark.parametrize(
        ("source", "expected", "replacement"),
        [
            ("gtimeout 3 echo ok", "timeout 3 echo ok", "gtimeout"),
            (
                "printf 'abc\\n' | rev",
                "printf 'abc\\n' | perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --",
                "rev",
            ),
            ("rev first.txt second.txt", "perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' -- first.txt second.txt", "rev"),
            ("xdg-open README.md", "start README.md", "xdg-open"),
            ("open https://example.com", "start https://example.com", "open"),
            ("printf text | pbcopy", "printf text | clip.exe", "pbcopy"),
              (
                  "pbpaste > clipboard.txt",
                  "powershell.exe -NoProfile -NonInteractive -Command "
                  "'[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
                  "[Console]::Out.Write((Get-Clipboard -Raw))' > clipboard.txt",
                  "pbpaste",
              ),
              ("wget https://example.com/f.zip", "curl -fSL -o f.zip -- https://example.com/f.zip", "wget"),
              ("printf text | xclip -selection clipboard", "printf text | clip.exe", "xclip"),
              ("xsel --clipboard", "clip.exe", "xsel"),
              ("gsed -n 1p file", "sed -n 1p file", "gsed"),
              ("zip -r out.zip dir", "Compress-Archive dir out.zip", "zip"),
              ("nc -z example.com 80", "Test-NetConnection example.com -Port 80", "nc"),
              ("netcat -z example.com 80", "Test-NetConnection example.com -Port 80", "netcat"),
              ("pgrep bash", "Get-Process bash", "pgrep"),
              ("tree -L 1 dir", "Get-ChildItem dir", "tree"),
              ("say hello", "SpeechSynthesizer hello", "say"),
              ("printf text | wl-copy", "printf text | clip.exe", "wl-copy"),
              ("python3 --version", "python --version", "python3"),
          ],
    )
    def test_verified_windows_mapping(
        self, source: str, expected: str, replacement: str
    ) -> None:
        result = _fix_for_windows(source)
        assert result.command.endswith("\n" + source)
        assert result.command != expected
        assert result.replacements == (replacement,)

    @pytest.mark.parametrize(
        "command",
        [
            "timeout 1 true",
            "stdbuf -oL echo ok",
            "mktemp",
            "truncate -s 0 file",
            "readlink file",
            "realpath file",
            "stat file",
            "sed -n 1p file",
            "grep value file",
            "find . -name '*.py'",
            "xargs echo",
            "tac file",
            "numfmt 1000",
            "nproc",
            "getconf PATH",
        ],
    )
    def test_git_bash_bundled_commands_are_not_rewritten(self, command: str) -> None:
        assert _fix_for_windows(command) == BashFix(command)

    @pytest.mark.parametrize(
        "command",
        [
            "setsid app",
            "flock lockfile app",
            "script transcript.txt",
            "getent passwd",
            "lsof file",
            "service app status",
              "apt update",
              "apt-get update",
          ],
    )
    def test_commands_without_faithful_mapping_are_preserved(self, command: str) -> None:
        # No fallback exists AND the command is not known-unsupported: the
        # text must pass through untouched for Bash to resolve (or reject).
        assert _fix_for_windows(command) == BashFix(command)


class TestBashFixCommandPositions:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("rev", "perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --"),
            ("'rev' <<< abc", "perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' -- <<< abc"),
            ('"rev" <<< abc', "perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' -- <<< abc"),
            (r"\rev <<< abc", "perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' -- <<< abc"),
            ('r""ev <<< abc', "perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' -- <<< abc"),
            ("  rev  ", "  perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --  "),
            ("true; rev", "true; perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --"),
            ("true && rev", "true && perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --"),
            ("false || rev", "false || perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --"),
            ("printf x | rev", "printf x | perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --"),
            ("rev & wait", "perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' -- & wait"),
            ("echo first\nrev", "echo first\nperl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --"),
            ("(rev)", "(perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --)"),
            ("{ rev; }", "{ perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --; }"),
            ("! rev", "! perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --"),
            ("if rev; then echo yes; fi", "if perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --; then echo yes; fi"),
            ("while rev; do break; done", "while perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --; do break; done"),
            ("until rev; do break; done", "until perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --; do break; done"),
            ("for x in one; do rev; done", "for x in one; do perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --; done"),
            ("result=$(rev)", "result=$(perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --)"),
            ("echo $(rev)", "echo $(perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --)"),
            ("echo `rev`", "echo `perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --`"),
            (
                'echo "$(rev)"',
                'echo "$(perl -ne \'s/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)\' --)"',
            ),
            ("diff <(rev) file", "diff <(perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --) file"),
            ("cat >(rev)", "cat >(perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --)"),
            ("FOO=bar rev", "FOO=bar perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --"),
            ("FOO=bar BAR=baz rev", "FOO=bar BAR=baz perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --"),
            (">output rev", ">output perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --"),
            ("2>/dev/null rev", "2>/dev/null perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --"),
            ("command rev", "command perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --"),
            ("command -- rev", "command -- perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --"),
            ("env rev", "env perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --"),
            ("env -i rev", "env -i perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --"),
            ("env FOO=bar rev", "env FOO=bar perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --"),
            ("nohup rev", "nohup perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --"),
            ("exec rev", "exec perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --"),
            ("time rev", "time perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --"),
        ],
    )
    def test_rewrites_only_executable_command_words(self, source: str, expected: str) -> None:
        result = _fix_for_windows(source)
        executable_wrappers = ("command ", "env ", "nohup ", "exec ")
        if source.startswith(executable_wrappers):
            assert not result.command.endswith("\n" + source)
        else:
            assert result.command.endswith("\n" + source)
        assert result.command != expected
        assert result.replacements == ("rev",)

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            (
                "gtimeout 2 sh -c 'printf ok' | rev",
                "timeout 2 sh -c 'printf ok' | perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' --",
            ),
            ("open one; xdg-open two", "start one; start two"),
            (
                "pbpaste | rev | pbcopy",
                "powershell.exe -NoProfile -NonInteractive -Command "
                "'[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
                "[Console]::Out.Write((Get-Clipboard -Raw))' | "
                "perl -ne 's/[\\r\\n]+\\z//; print scalar(reverse($_)), qq(\\n)' -- | clip.exe",
            ),
        ],
    )
    def test_multiple_rewrites_preserve_order(self, source: str, expected: str) -> None:
        result = _fix_for_windows(source)
        assert result.command.endswith("\n" + source)
        assert result.command != expected
        assert len(result.replacements) >= 2


class TestBashFixShellWrappers:
    """Redundant leading ``bash``/``sh`` invocations are unwrapped for Git Bash.

    Agents commonly emit ``bash cd /c/dev/x && ...`` or ``bash -c '...'`` as a
    habit; the Bash tool already runs the whole string via bash, so a bare
    ``bash cd ...`` would make bash try to open ``cd`` as a script file and
    fail on Windows.  The scanner removes that redundant prefix and scans
    ``-c`` inline scripts so fallback commands and Windows paths inside them
    are fixed in place — the ``bash -c`` wrapper itself stays, because the
    child shell's isolation (``exit``, assignments, ``cd``, traps) must be
    preserved and the nested bash inherits the exported fallback functions.
    """

    @pytest.mark.parametrize(
        ("command", "expected_tail"),
        [
            ("bash cd /c/dev/x && echo ok", "cd C:/dev/x && echo ok"),
            ("sh cd /c/dev/x && echo ok", "cd C:/dev/x && echo ok"),
            ("bash cd /c/dev/x && ls", "cd C:/dev/x && ls"),
            (
                "bash grep -rn kimix src tests --include=*.h | head -40",
                "grep -rn kimix src tests --include=*.h | head -40",
            ),
            (
                "bash cd /c/dev/hermes-native && grep -rn \"kimix\" src tests "
                "--include=*.h --include=*.cpp --include=*.lua | grep -v \"src/ext\" "
                "| head -40",
                "cd C:/dev/hermes-native && grep -rn \"kimix\" src tests "
                "--include=*.h --include=*.cpp --include=*.lua | grep -v \"src/ext\" "
                "| head -40",
            ),
        ],
    )
    def test_redundant_shell_prefix_is_unwrapped(
        self, command: str, expected_tail: str
    ) -> None:
        result = _fix_for_windows(command)
        assert not result.command.startswith("bash ")
        assert not result.command.startswith("sh ")
        assert result.command.split("\n")[-1] == expected_tail
        assert result.shell_wrappers == (command.split(" ", 1)[0],)
        assert result.changed
        assert "Removed redundant shell wrapper" in result.warning

    @pytest.mark.parametrize(
        ("source", "expected_tail", "replacements"),
        [
            ("bash -c 'rev'", "bash -c 'rev'", ("rev",)),
            ("bash -c \"rev\"", "bash -c \"rev\"", ("rev",)),
            ("bash -lc 'rev'", "bash -lc 'rev'", ("rev",)),
            ("bash -cl 'rev'", "bash -cl 'rev'", ("rev",)),
            ("bash -l -c 'rev'", "bash -l -c 'rev'", ("rev",)),
            ("sh -c 'rev'", "sh -c 'rev'", ("rev",)),
            ("dash -c 'rev'", "dash -c 'rev'", ("rev",)),
            ("bash -c 'rev' && echo done", "bash -c 'rev' && echo done", ("rev",)),
            ("echo $(bash -c 'rev')", "echo $(bash -c 'rev')", ("rev",)),
            ("bash -c 'echo $HOME'", "bash -c 'echo $HOME'", ()),
        ],
    )
    def test_dash_c_wrapper_keeps_subshell_semantics(
        self, source: str, expected_tail: str, replacements: tuple[str, ...]
    ) -> None:
        # ``bash -c 'script'`` runs the script in a CHILD shell: ``exit``,
        # variable assignments, ``cd`` and traps must not leak into the
        # surrounding script.  Inlining the script (the old unwrap) broke
        # that isolation, so the wrapper stays and only the inline script
        # is scanned and fixed in place; the exported fallback functions
        # are inherited by the child shell.
        result = _fix_for_windows(source)
        assert result.command.split("\n")[-1] == expected_tail
        assert result.shell_wrappers == ()
        assert result.replacements == replacements

    @pytest.mark.parametrize(
        "source",
        [
            "bash -c 'exit 3'; echo after",
            "bash -c 'x=inner'; echo \"[$x]\"",
            "bash -c 'cd subdir'; pwd",
            "bash -c 'trap \"echo t\" EXIT; echo body'",
        ],
    )
    def test_dash_c_subshell_isolation_not_broken(self, source: str) -> None:
        # The wrapper must survive: these scripts rely on child-shell
        # isolation, so the command is returned with only in-place script
        # fixes (none needed here).
        result = _fix_for_windows(source)
        assert result.shell_wrappers == ()
        assert result.command.split("\n")[-1] == source

    @staticmethod
    def _run_raw(command: str) -> tuple[int, str]:
        bash = find_bash()
        assert bash is not None
        p = subprocess.run(
            [bash, "-c", command], capture_output=True, text=True,
            timeout=60, check=False,
        )
        return p.returncode, p.stdout

    def test_dash_c_script_with_subshell_semantics_runs_identically(self) -> None:
        # Differential proof: original and scanner output must behave the
        # same in real bash (the fallback prefix must not change the
        # exit-code/stdout behavior of the child shell).
        source = "bash -c 'exit 3'; echo after"
        result = _fix_for_windows(source)
        assert self._run_raw(result.command) == self._run_raw(source)

    def test_inline_script_fixes_inner_windows_path_and_fallback(self) -> None:
        result = _fix_for_windows(r"bash -c 'cd C:\x && rev'")
        assert result.command.split("\n")[-1] == r"bash -c 'cd C:/x && rev'"
        assert result.replacements == ("rev",)
        assert result.path_changes == (r"C:\x",)
        assert result.shell_wrappers == ()

    def test_redundant_prefix_keeps_inner_fallback_rewrite(self) -> None:
        result = _fix_for_windows("bash cd /c/dev/x && rev")
        assert result.command.split("\n")[-1] == "cd C:/dev/x && rev"
        assert result.replacements == ("rev",)
        assert result.shell_wrappers == ("bash",)

    @pytest.mark.parametrize(
        "source",
        [
            "'bash' cd /c/dev/x && echo ok",
            '"bash" cd /c/dev/x && echo ok',
            r"\bash cd /c/dev/x && echo ok",
        ],
    )
    def test_quoted_shell_words_are_unwrapped(self, source: str) -> None:
        result = _fix_for_windows(source)
        assert result.command.split("\n")[-1] == "cd C:/dev/x && echo ok"
        assert result.shell_wrappers == ("bash",)

    @pytest.mark.parametrize(
        "command",
        [
            "bash script.sh",
            "bash ./script.sh",
            "bash ../tools/run",
            "bash scripts/deploy.sh",
            "sh build.sh --release",
            "bash -c 'echo hi' arg1",
            'bash -x "rev"',
            "bash -s",
            "bash --",
            "bash",
            "echo bash",
            "ls sh",
            "bash -c",
        ],
    )
    def test_legitimate_shell_invocations_are_preserved(self, command: str) -> None:
        assert _fix_for_windows(command) == BashFix(command)

    @pytest.mark.parametrize(
        "command",
        [
            'bash -e -c "rev"',
            'bash -ec "rev"',
            'sh -c "rev" a b',
            "find . -maxdepth 0 -exec sh -c 'tree' \\;",
        ],
    )
    def test_option_led_c_script_keeps_wrapper_and_registers_fallbacks(
        self, command: str
    ) -> None:
        """Option-led ``-c`` wrappers register the nested script's fallbacks.

        ``_fix_shell_c_script_in_place`` repairs the ``-c`` script in place and
        the shell word stays (``bash -e -c 'rev'`` must keep running the script
        in a child shell), but the inner fallback names have to be merged
        exactly like the wrapped ``bash -c`` branch does: the emitted
        definitions are ``export -f``-ed and a nested bash / msys ``sh``
        imports exported functions, so the repaired command resolves ``rev``
        to the fallback instead of failing with ``rev: command not found``.
        """
        result = _fix_for_windows(command)
        assert result.replacements, command
        # The wrapper (and the script operand) stay byte-for-byte: the child
        # shell's isolation is what makes the invocation legitimate.
        assert result.command.split("\n")[-1] == command
        assert result.shell_wrappers == ()

    @pytest.mark.skipif(not BASH_AVAILABLE, reason="bash not installed")
    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ('bash -e -c "rev"', "cba"),
            ('bash -ec "rev"', "cba"),
            ("sh -c 'rev' a b", "cba"),
        ],
    )
    def test_option_led_c_script_fallback_actually_runs(
        self, command: str, expected: str, tmp_path: Path
    ) -> None:
        """Real Git Bash ground truth for the merged fallback definitions.

        The repaired text is executed as a script file (not ``bash -c``) so no
        Windows argv-quoting artefact can distort the run, and stdin carries
        the ``rev`` input.
        """
        bash = find_bash()
        assert bash is not None
        script = tmp_path / "kmx_option_c_probe.sh"
        script.write_text(
            _fix_for_windows(command).command + "\n", encoding="utf-8", newline="\n"
        )
        proc = subprocess.run(
            [bash, "--noprofile", "--norc", str(script)],
            capture_output=True,
            input="abc\n",
            timeout=60,
            encoding="utf-8",
            errors="replace",
        )
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert expected in proc.stdout, (command, proc.stdout, proc.stderr)

    @pytest.mark.parametrize("platform", ["linux", "darwin"])
    def test_non_windows_keeps_shell_wrapper(self, platform: str) -> None:
        command = "bash cd /c/dev/x && echo ok"
        result = _fix_for_platform(command, platform)
        assert result == BashFix(command)
        assert not result.changed


class TestBashFixNulRedirection:
    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("echo hi > nul", "echo hi > /dev/null"),
            ("echo hi >NUL", "echo hi >/dev/null"),
            ("echo hi 2> nul", "echo hi 2> /dev/null"),
            ("echo hi &> nul", "echo hi &> /dev/null"),
            ("echo hi >> nul", "echo hi >> /dev/null"),
            ("echo hi 2>> nul", "echo hi 2>> /dev/null"),
            ("echo hi>nul", "echo hi>/dev/null"),
            (
                "echo hi > nul; echo bye > nul",
                "echo hi > /dev/null; echo bye > /dev/null",
            ),
            ("echo 'nul' > nul", "echo 'nul' > /dev/null"),
        ],
    )
    def test_rewrites_unquoted_nul_target(self, command: str, expected: str) -> None:
        result = _fix_for_windows(command)
        assert result.command == expected
        assert result.changed is True
        assert result.nul_fixes
        for spelling in result.nul_fixes:
            assert spelling.casefold() == "nul"
        assert "/dev/null" in result.warning
        assert "nul" in result.warning

    def test_multiple_targets_record_each_original_spelling(self) -> None:
        result = _fix_for_windows("echo a > nul; echo b > NUL; echo c >nul")
        assert result.command == "echo a > /dev/null; echo b > /dev/null; echo c >/dev/null"
        assert result.nul_fixes == ("nul", "NUL", "nul")

    @pytest.mark.parametrize(
        "command",
        [
            "echo hi > 'nul'",
            'echo hi > "nul"',
            "echo hi < nul",
            "echo nul",
            "echo /dev/null > nul.txt",
            "echo hi > null",
            "echo hi > /dev/null",
        ],
    )
    def test_preserved_cases_are_byte_for_byte(self, command: str) -> None:
        result = _fix_for_windows(command)
        assert result == BashFix(command)
        assert result.nul_fixes == ()
        assert not result.changed


class TestBashFixFalsePositives:
    @pytest.mark.parametrize(
        "command",
        [
            "echo rev",
            "printf '%s' rev",
                          "echo gtimeout open xdg-open pbcopy pbpaste wget xclip xsel "
                          "gsed zip nc pgrep tree say wl-copy wl-paste python3 pip3",
            "name=rev",
            "tool=gtimeout",
            "array=(rev open pbcopy)",
            "printf > rev",
            "cat < pbpaste",
            "echo /usr/bin/rev",
            "./rev",
            "bin/rev",
            "$tool",
            "${tool}",
            "$(printf rev)",
            "echo `printf rev`",
            "echo rev # rev",
            "echo ok # gtimeout 1 false",
            "# rev\necho ok",
            "case value in rev) echo match;; esac",
            "case value in *) echo rev;; esac",
            "alias rev='printf alias'",
            "function rev { printf custom; }",
            "rev() { printf custom; }",
            "declare -f rev",
            "type rev",
            "command -v rev",
            "which rev",
            "printf '%s\\n' 'open https://example.com'",
            "echo $'rev\\nopen'",
            'echo "literal rev and open"',
        ],
    )
    def test_data_and_declarations_are_unchanged(self, command: str) -> None:
        assert _fix_for_windows(command) == BashFix(command)

    def test_heredoc_body_and_delimiter_are_unchanged(self) -> None:
        command = "cat <<'EOF'\nrev\ngtimeout 1 false\nopen file\nEOF"
        assert _fix_for_windows(command) == BashFix(command)

    @pytest.mark.parametrize("operator", ["&>", "&>>"])
    def test_combined_output_redirection_target_rewritten(
        self, operator: str
    ) -> None:
        import tempfile

        tmp = tempfile.gettempdir().replace("\\", "/")
        command = f"printf '%s' {operator}/tmp/kimix-output rev"
        result = _fix_for_windows(command)
        assert result.command == f"printf '%s' {operator}{tmp}/kimix-output rev"
        assert result.path_changes == ("/tmp/kimix-output",)

    def test_heredoc_delimiter_substitution_is_literal(self) -> None:
        command = "cat <<$(rev)\nbody\n$(rev)\ntype -t rev"
        assert _fix_for_windows(command) == BashFix(command)

    def test_expanding_heredoc_folds_backslash_newline_before_delimiter(self) -> None:
        command = "cat <<EOF\nprefix\\\nEOF\ncommand rev\nEOF"
        assert _fix_for_windows(command) == BashFix(command)

    def test_out_of_range_ansi_c_heredoc_delimiter_never_crashes(self) -> None:
        command = "cat <<$'\\U00110000'\nbody\nEOF\nrev <<< abc"
        assert _fix_for_windows(command) == BashFix(command)

    @pytest.mark.parametrize("escape", [r"\U00110000", r"\uD800"])
    def test_invalid_ansi_c_delimiter_spelling_cannot_end_heredoc(
        self, escape: str
    ) -> None:
        command = f"cat <<$'{escape}'\nbody\n{escape}\nrev <<< after"
        assert _fix_for_windows(command) == BashFix(command)

    def test_heredoc_body_ignored_but_following_command_rewritten(self) -> None:
        source = "cat <<EOF\nrev\nEOF\nrev"
        result = _fix_for_windows(source)
        assert result.command.endswith("\n" + source)
        assert result.replacements == ("rev",)

    def test_comment_ignored_but_following_line_rewritten(self) -> None:
        source = "echo ok # rev\nrev"
        result = _fix_for_windows(source)
        assert result.command.endswith("\n" + source)
        assert result.replacements == ("rev",)

    @pytest.mark.parametrize(
        "source",
        [
            "work() { rev <<< abc; }; work",
            "function work { rev <<< abc; }; work",
            "function work() { rev <<< abc; }; work",
        ],
    )
    def test_first_function_body_command_is_rewritten(self, source: str) -> None:
        result = _fix_for_windows(source)
        assert result.command.endswith("\n" + source)
        assert result.replacements == ("rev",)

    def test_function_name_preserved_but_body_commands_rewritten(self) -> None:
        source = "work() { printf abc | rev; }; work"
        result = _fix_for_windows(source)
        assert result.command.endswith("\n" + source)
        assert result.replacements == ("rev",)

    def test_quoted_heredoc_parenthesis_inside_command_substitution_is_literal(self) -> None:
        source = "printf '%s\\n' \"$(cat <<'EOF'\n)\n$(command rev)\nEOF\n)\""
        assert _fix_for_windows(source) == BashFix(source)

    def test_arithmetic_shift_inside_command_substitution_does_not_hide_later_command(
        self,
    ) -> None:
        source = 'printf "%s\\n" "$(\n: $((1 << 2))\n)"\nrev <<< abc'
        result = _fix_for_windows(source)
        assert result.command.endswith("\n" + source)
        assert result.replacements == ("rev",)

    def test_case_pattern_parenthesis_inside_command_substitution_is_not_closing(
        self,
    ) -> None:
        source = "printf '%s\\n' \"$(case x in x) rev <<< abc;; esac)\""
        result = _fix_for_windows(source)
        assert result.command.endswith("\n" + source)
        assert result.replacements == ("rev",)

    @pytest.mark.parametrize("terminator", [";;", ";&", ";;&"])
    def test_completed_case_inside_substitution_does_not_hide_later_command(
        self, terminator: str
    ) -> None:
        source = (
            "printf '%s\\n' \"$(case x in 'x') :"
            f"{terminator} esac)\"; rev <<< abc"
        )
        result = _fix_for_windows(source)
        assert result.command.endswith("\n" + source)
        assert result.replacements == ("rev",)

    def test_nested_completed_cases_inside_substitution_do_not_hide_later_command(
        self,
    ) -> None:
        source = (
            "printf '%s\\n' \"$(case x in x) case y in y) :;; esac;; esac)\"; "
            "rev <<< abc"
        )
        result = _fix_for_windows(source)
        assert result.command.endswith("\n" + source)
        assert result.replacements == ("rev",)

    @pytest.mark.parametrize(
        "command",
        [
            "[[ rev == rev && rev == rev ]] && printf OK",
            "rev=1; (( rev )); printf '%s' $?",
            "let rev=1",
            "for ((rev=0; rev<2; rev++)); do printf x; done",
        ],
    )
    def test_conditional_and_arithmetic_words_are_not_commands(self, command: str) -> None:
        assert _fix_for_windows(command) == BashFix(command)

    def test_command_after_case_is_detected(self) -> None:
        source = "case x in x) :;; esac; rev <<< abc"
        result = _fix_for_windows(source)
        assert result.command.endswith("\n" + source)
        assert result.replacements == ("rev",)

    @pytest.mark.parametrize(
        "subject",
        [
            "$(printf x)",  # command substitution is the case subject word
            "`printf x`",  # backquote substitution is the case subject word
            "$((0))",  # arithmetic expansion is the case subject word
            "<(printf x)",  # process substitution is the case subject word
        ],
    )
    def test_case_subject_substitution_still_finds_body_command(
        self, subject: str
    ) -> None:
        # The subject word may be a substitution rather than a plain word;
        # it still ends the case header, so body commands are executable.
        source = f"case {subject} in x) rev;; esac"
        result = _fix_for_windows(source)
        assert result.command.endswith("\n" + source)
        assert result.replacements == ("rev",)

    @pytest.mark.parametrize(
        "subject",
        ["$(printf x)", "`printf x`", "$((0))"],
    )
    def test_case_subject_substitution_inside_command_substitution(
        self, subject: str
    ) -> None:
        # The bracket matcher must not mistake the first pattern ``)`` for
        # the end of the enclosing ``$( ... )`` when the case subject is a
        # substitution; the body command is still executable.
        source = f"printf '%s' \"$(case {subject} in x) rev;; esac)\""
        result = _fix_for_windows(source)
        assert result.command.endswith("\n" + source)
        assert result.replacements == ("rev",)

    def test_case_subject_substitution_before_later_command(self) -> None:
        source = "echo $(case `printf x` in x) :;; esac); rev <<< abc"
        result = _fix_for_windows(source)
        assert result.command.endswith("\n" + source)
        assert result.replacements == ("rev",)

    @pytest.mark.parametrize(
        "source",
        [
            # Pattern words stay data even when the subject is a substitution.
            "case $(printf x) in rev) echo hit;; esac",
            "case `printf x` in rev | open) echo hit;; esac",
        ],
    )
    def test_case_pattern_after_substitution_subject_is_not_a_command(
        self, source: str
    ) -> None:
        assert _fix_for_windows(source) == BashFix(source)

    def test_case_pattern_named_in_after_substitution_subject(self) -> None:
        # ``in`` can itself be a pattern; only the first ``in`` after the
        # substitution subject is the case keyword.
        source = "case $(printf x) in in) rev;; esac"
        result = _fix_for_windows(source)
        assert result.command.endswith("\n" + source)
        assert result.replacements == ("rev",)

    def test_command_substitution_inside_array_assignment_is_detected(self) -> None:
        source = "values=($(rev <<< abc)); printf '%s' \"${values[0]}\""
        result = _fix_for_windows(source)
        assert result.command.endswith("\n" + source)
        assert result.replacements == ("rev",)

    def test_command_substitution_inside_expanding_heredoc_is_detected(self) -> None:
        source = "cat <<EOF\n$(rev <<< abc)\nEOF"
        result = _fix_for_windows(source)
        assert result.command.endswith("\n" + source)
        assert result.replacements == ("rev",)

    @pytest.mark.parametrize("quote", ["'", '"'])
    def test_quotes_are_literal_in_expanding_heredoc_body(self, quote: str) -> None:
        source = f"cat <<EOF\n{quote}$(rev <<< abc){quote}\nEOF"
        result = _fix_for_windows(source)
        assert result.command.endswith("\n" + source)
        assert result.replacements == ("rev",)

    @pytest.mark.parametrize(
        "source",
        [
            "cat <<$'EOF'\nbody\nEOF\nrev <<< abc",
            'cat <<"A\\q"\nbody\nA\\q\nrev <<< abc',
        ],
    )
    def test_quoted_heredoc_delimiter_allows_following_command(
        self, source: str
    ) -> None:
        result = _fix_for_windows(source)
        assert result.command.endswith("\n" + source)
        assert result.replacements == ("rev",)

    def test_command_substitution_inside_quoted_heredoc_is_literal(self) -> None:
        source = "cat <<'EOF'\n$(rev <<< abc)\nEOF"
        assert _fix_for_windows(source) == BashFix(source)

    def test_nested_case_detects_later_outer_clause_command(self) -> None:
        source = (
            "case z in x) case y in y) :;; esac;; "
            "z) rev <<< abc;; esac"
        )
        result = _fix_for_windows(source)
        assert result.replacements == ("rev",)
        assert result.command.endswith("\n" + source)

    def test_nested_case_outer_pattern_is_not_a_command(self) -> None:
        source = (
            "case x in x) case y in y) :;; esac;; rev) :;; esac; "
            "command -v rev >/dev/null || printf clean"
        )
        assert _fix_for_windows(source) == BashFix(source)

    @pytest.mark.parametrize(
        "source",
        [
            "coproc rev",
            "coproc worker { rev; }",
            "coproc worker if rev; then :; fi",
        ],
    )
    def test_coproc_command_is_detected(self, source: str) -> None:
        result = _fix_for_windows(source)
        assert result.command.endswith("\n" + source)
        assert result.replacements == ("rev",)

    @pytest.mark.parametrize(
        "source",
        [
            "env --default-signal rev <<< abc",
            "env --block-signal rev <<< abc",
            "env --ignore-signal rev <<< abc",
        ],
    )
    def test_env_optional_signal_options_do_not_consume_command(
        self, source: str
    ) -> None:
        result = _fix_for_windows(source)
        assert result.replacements == ("rev",)
        assert result.command != source

    @pytest.mark.parametrize(
        "source",
        [
            "command -p rev",
            "command -p -- rev",
            "command -pv rev",
            "command -vp rev",
            "command -pV rev",
            "env -S printf rev",
            "env -S'printf %s\\n' rev",
            "env -Sprintf rev",
            "env --split-string printf rev",
            "env --split-string='printf rev'",
        ],
    )
    def test_opaque_wrapper_forms_are_preserved(self, source: str) -> None:
        assert _fix_for_windows(source) == BashFix(source)


class TestBashFixHeredocTrailingOperators:
    """Repair a heredoc terminator followed by a control operator.

    Bash requires a control operator that continues a heredoc-delimited command
    to appear on the same line as the ``<<`` redirection.  A common model
    mistake is to place ``&&`` (or ``||``, ``;``, ``|``...) on the line after
    the closing delimiter, which produces ``syntax error near unexpected token
    `&&'``.  The fixer moves the operator (and the command it chains) to the
    redirection line, leaving the heredoc body and delimiter untouched.
    """

    @pytest.mark.parametrize(
        ("operator", "rest", "expected"),
        [
            ("&&", "echo next", " && echo next"),
            ("||", "echo fallback", " || echo fallback"),
            (";", "echo done", " ; echo done"),
            ("|", "tr a b", " | tr a b"),
            ("|&", "cat", " |& cat"),
        ],
    )
    def test_moves_operator_after_terminator_to_redirection_line(
        self, operator: str, rest: str, expected: str
    ) -> None:
        source = f"cat <<EOF\nhello\nEOF\n{operator} {rest}"
        result = _fix_for_windows(source)
        assert result.command == f"cat <<EOF{expected}\nhello\nEOF\n"

    def test_moves_operator_that_is_alone_on_its_line(self) -> None:
        source = "python - <<'PY'\nprint(1)\nPY\n&&\necho next"
        result = _fix_for_windows(source)
        assert result.command == "python - <<'PY' && echo next\nprint(1)\nPY\n"

    def test_multiple_heredocs_on_same_command_line(self) -> None:
        source = "cat <<A <<B\na\nA\nb\nB\n&& echo next"
        result = _fix_for_windows(source)
        assert result.command == "cat <<A <<B && echo next\na\nA\nb\nB\n"

    def test_windows_path_and_cd_flag_interaction(self) -> None:
        source = (
            "cd /d D:\\compute && python - <<'PY'\nprint(1)\nPY\n"
            "&& git status --short src/ext/BTree"
        )
        result = _fix_for_windows(source)
        expected = (
            "cd  D:/compute && python - <<'PY' && git status --short src/ext/BTree\n"
            "print(1)\nPY\n"
        )
        assert result.command == expected

    def test_no_change_when_no_trailing_operator(self) -> None:
        source = "cat <<EOF\nhello\nEOF\necho next"
        assert _fix_for_windows(source) == BashFix(source)

    def test_no_change_when_delimiter_line_has_trailing_text(self) -> None:
        source = "cat <<EOF\nhello\nEOF extra\n&& echo next"
        result = _fix_for_windows(source)
        assert result.command == source

    @pytest.mark.skipif(not BASH_AVAILABLE, reason="bash not installed")
    def test_original_command_fails_and_fixed_command_runs(self) -> None:
        # ``cat`` (unlike ``python``) is guaranteed to exist in every bash
        # environment, so this integration check does not depend on an
        # interpreter being on PATH (many hosts only ship ``python3``).
        source = "cat <<'EOF'\nhello\nEOF\n&& echo next"
        # The host's real bash is used explicitly: the bare ``bash`` name is
        # resolved by ``CreateProcess`` through ``C:\Windows\System32`` before
        # ``PATH`` on Windows, so it would launch the WSL/Store stub (exit 1,
        # UTF-16 "no distributions" banner) instead of Git Bash.
        bash = find_bash()
        assert bash is not None
        # ``errors="replace"``: some hosts (e.g. cygwin/git-bash on non-UTF-8
        # locales) emit localized diagnostics that are not valid UTF-8.
        failed = subprocess.run(
            [bash, "-c", source],
            capture_output=True,
            timeout=15,
            encoding="utf-8",
            errors="replace",
        )
        if BASH_REJECTS_HEREDOC_TRAILING_OPERATOR:
            assert failed.returncode != 0
            assert "syntax error" in failed.stderr

        fixed = _fix_for_windows(source).command
        passed = subprocess.run(
            [bash, "-c", fixed],
            capture_output=True,
            timeout=15,
            encoding="utf-8",
            errors="replace",
        )
        assert passed.returncode == 0
        assert "hello" in passed.stdout
        assert "next" in passed.stdout


class TestBashFixNewFallbacks:
    """Tests for Windows cmd-style and missing-POSIX fallback commands."""

    @pytest.mark.parametrize(
        "source",
        [
            "copy a b",
            "move a b",
            "del a",
            "erase a",
            "ren a b",
            "rename a b",
            "rd d",
            "md d",
            "chdir .",
            "cls",
            "xcopy a b",
            "mklink /D link target",
            "mklink link target",
            "findstr x file",
            "fc a b",
            "where bash",
            "tasklist",
            "taskkill /IM notepad /F",
            "taskkill /PID 1234",
            "systeminfo",
            "watch -n 1 true",
            "killall bash",
            "pidof bash",
            "column -t file",
            "column -s , -t file",
            # Common-cheat-sheet utilities missing from Git Bash (new set).
            "free -h",
            "uptime",
            "top -b -n 1",
            "htop",
            "ss -tlnp",
            "ip addr",
            "man ls",
            "systemctl status svc",
            "sudo ls",
        ],
    )
    def test_new_fallbacks_are_rewritten(self, source: str) -> None:
        result = _fix_for_windows(source)
        assert result.changed
        assert result.command.endswith("\n" + source)
        name = source.split()[0]
        assert name in result.replacements

    def test_multiple_new_fallbacks_preserve_order(self) -> None:
        source = "copy a b; move c d; del e"
        result = _fix_for_windows(source)
        assert result.replacements == ("copy", "move", "del")

    def test_new_fallbacks_in_pipes_and_substitutions(self) -> None:
        source = "pidof bash | killall bash"
        result = _fix_for_windows(source)
        assert result.replacements == ("pidof", "killall")

    @pytest.mark.parametrize(
        "source",
        [
            "echo copy a b",
            "name=copy",
            "tool=move",
            "array=(del erase)",
            "printf > md",
            "cat < rd",
            "echo /usr/bin/copy",
            "./copy",
            "bin/move",
            "$tool",
            "${tool}",
            "$(printf copy)",
            "echo `printf del`",
            "echo copy # copy a b",
            "case value in copy) echo match;; esac",
            "alias copy='printf alias'",
            "function copy { printf custom; }",
            "copy() { printf custom; }",
            "declare -f copy",
            "type copy",
            "command -v copy",
            "which copy",
            "echo tasklist",
            "echo watch date",
            "echo free -h",
            "top=1",
            "x=journalctl",
            "echo systemctl",
            "echo 'journalctl -u svc'",
        ],
    )
    def test_new_fallback_data_and_declarations_unchanged(
        self, source: str
    ) -> None:
        assert _fix_for_windows(source) == BashFix(source)

    @pytest.mark.parametrize("platform", ["linux", "darwin", "freebsd", "cygwin"])
    def test_new_fallbacks_noop_on_non_windows(self, platform: str) -> None:
        source = "copy a b; tasklist; watch -n 1 date"
        result = _fix_for_platform(source, platform)
        assert result == BashFix(source)


class TestBashFixCommonCommands:
    """Every command from the common bash cheat-sheet parses as intended.

    Native Git Bash commands pass through byte-for-byte; POSIX utilities
    missing from Git Bash gain fallback definitions; commands with no faithful
    Windows equivalent are reported via ``BashFix.unsupported`` with a reason
    (the Bash tool turns that into an error message instead of executing a
    guaranteed "command not found").
    """

    @pytest.mark.parametrize(
        "command",
        [
            # File and directory operations.
            "ls -la",
            "ls -lah --sort=size .",
            "cd src",
            "cd ..",
            "pwd",
            "mkdir -p a/b/c",
            "rmdir empty",
            "touch file.txt",
            "cp -r src dst",
            "mv old new",
            "rm -rf build",
            "find . -name '*.py' -type f",
            "ln -s target link",
            # Viewing and editing files.
            "cat file.txt",
            "less file.txt",
            "head -n 100 file.txt",
            "tail -f app.log",
            "tail -n 20 app.log",
            "grep -rn 'rev' .",
            "grep -i error app.log",
            "sed -i 's/old/new/g' file.txt",
            "awk '{print $1, $3}' file.txt",
            "sort -k2 -nr file.txt",
            "sort file.txt | uniq -c",
            "wc -l file.txt",
            # Permissions and ownership.
            "chmod 755 script.sh",
            "chmod +x script.sh",
            "chown user:group file",
            # System and processes (tools Git Bash ships natively).
            "ps aux | grep python",
            "kill -9 1234",
            "df -h",
            "du -sh dir",
            "uname -a",
            # Network.
            "ping -c 4 example.com",
            "curl -X POST -d 'a=1' https://example.com",
            "ssh user@host -p 2222",
            "scp file user@host:/path",
            "netstat -ano",
            # Archives.
            "tar -czvf a.tgz dir",
            "tar -xzvf a.tgz",
            "unzip a.zip -d dir",
            # Miscellaneous utilities.
            "echo hello > f.txt",
            "echo $HOME",
            "date +'%Y-%m-%d %H:%M:%S'",
            "which bash",
            "history | grep ssh",
            "xargs -n1 echo",
            "find . -name '*.log' | xargs rm",
            "cut -d: -f1 /etc/passwd",
            "tr 'a-z' 'A-Z' < file.txt",
        ],
    )
    def test_native_git_bash_commands_pass_through(self, command: str) -> None:
        """Commands Git Bash ships must not be rewritten in any cheat-sheet form."""
        assert _fix_for_windows(command) == BashFix(command)

    @pytest.mark.parametrize(
        "command",
        [
            "free",
            "free -h",
            "free -m",
            "free -g",
            "free -b",
            "uptime",
            "uptime -s",
            "top",
            "top -b -n 1",
            "top -d 5 -n 2",
            "htop",
            "htop -d 1",
            "ss -tln",
            "ss -tlnp",
            "ss -s",
            "ss --summary",
            "ip",
            "ip addr",
            "ip address",
            "ip -br addr",
            "ip link",
            "ip route",
            "ip neigh",
            "man ls",
            "man 1 grep",
            "man git status",
            "systemctl status svc",
            "systemctl start svc",
            "systemctl stop svc",
            "systemctl restart svc",
            "systemctl reload svc",
            "systemctl enable svc",
            "systemctl disable svc",
            "systemctl is-active svc",
            "systemctl is-enabled svc",
            "systemctl list-units",
            "systemctl list-unit-files",
            "wget https://example.com/f.zip",
            "zip -r out.zip dir",
        ],
    )
    def test_missing_commands_gain_fallback_definitions(self, command: str) -> None:
        """Cheat-sheet commands Git Bash lacks get a fallback definition."""
        result = _fix_for_windows(command)
        name = command.split()[0]
        assert name in result.replacements
        assert f"command -v {name}" in result.command
        assert result.command.endswith("\n" + command)

    def test_sudo_is_a_fallback_wrapper(self) -> None:
        """``sudo`` keeps its wrapper operand semantics AND records its
        definition (used only on hosts without ``sudo.exe``)."""
        result = _fix_for_windows("sudo systemctl status svc")
        assert result.replacements == ("sudo", "systemctl")
        assert "command -v sudo" in result.command
        # ``systemctl`` is the wrapped command word of an executable wrapper:
        # it is swapped for the standalone runner.
        assert "_wrapper_runner" not in result.command  # runner is inline text
        assert "/usr/bin/bash -c" in result.command

    @pytest.mark.parametrize(
        "command",
        [
            "journalctl",
            "journalctl -u svc",
            "journalctl -u svc -f",
            "journalctl --since today",
            "journalctl -n 50 --no-pager",
            "journalctl | grep error",
        ],
    )
    def test_unsupported_commands_keep_text_and_report_reason(
        self, command: str
    ) -> None:
        """Commands with no Windows equivalent: text untouched, reason recorded."""
        result = _fix_for_windows(command)
        assert result.unsupported == ("journalctl",)
        assert result.command == command
        assert not result.changed
        assert "journalctl" in result.warning
        assert "no Windows Git Bash equivalent" in result.warning
        assert "Get-WinEvent" in result.warning  # suggested alternative

    @pytest.mark.parametrize(
        "command",
        [
            "sudo journalctl -u svc",
            "bash -c 'journalctl -n 5'",
            "echo ok && journalctl -f",
        ],
    )
    def test_unsupported_commands_in_wrapped_positions(self, command: str) -> None:
        """Unsupported names are detected inside wrappers and ``bash -c`` too."""
        result = _fix_for_windows(command)
        assert result.unsupported == ("journalctl",)
        assert "journalctl" in result.warning

    @pytest.mark.parametrize(
        "command",
        [
            "echo journalctl",
            "journalctl=1",
            "x=journalctl",
            "echo 'journalctl -u svc'",
            "alias journalctl='printf x'",
            "journalctl() { printf fn; }",
        ],
    )
    def test_unsupported_names_as_data_and_declarations_unchanged(
        self, command: str
    ) -> None:
        """``journalctl`` as argument/assignment/declaration is not a command."""
        assert _fix_for_windows(command) == BashFix(command)

    @pytest.mark.parametrize("platform", ["linux", "darwin", "freebsd", "cygwin"])
    @pytest.mark.parametrize(
        "command",
        [
            "free -h",
            "ss -tlnp",
            "ip addr",
            "man ls",
            "systemctl status svc",
            "sudo journalctl -u svc",
            "journalctl -f",
        ],
    )
    def test_noop_on_non_windows(self, platform: str, command: str) -> None:
        result = _fix_for_platform(command, platform)
        assert result == BashFix(command)


class TestBashToolUnsupportedCommand:
    """The Bash tool surfaces the parser's unsupported reason as an error message."""

    @pytest.fixture
    def windows_tool(self, mock_session: MagicMock) -> Any:
        with (
            patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"),
            patch("kimi_cli.tools.file.bash.bash_fix.sys.platform", "win32"),
            patch(
                "kimi_cli.tools.file.bash.bash_tool.find_bash",
                return_value=r"C:\Git\bin\bash.exe",
            ),
            patch(
                "kimi_cli.tools.file.bash.bash_tool._configured_shell",
                return_value=None,
            ),
            patch(
                "kimi_cli.tools.file.bash.bash_tool.USE_SYSTEM_PWSH_ON_WINDOWS",
                False,
            ),
        ):
            yield Bash(mock_session)

    def test_prepare_command_rejects_unsupported_with_reason(
        self, windows_tool: Bash
    ) -> None:
        error = windows_tool._prepare_command("journalctl -u svc -f")
        assert isinstance(error, ToolError)
        assert "journalctl" in error.message
        assert "no Windows Git Bash equivalent" in error.message
        assert "Get-WinEvent" in error.message  # the suggested alternative
        assert error.brief == "Unsupported command on Windows"

    def test_prepare_command_allows_supported_commands(
        self, windows_tool: Bash
    ) -> None:
        prepared = windows_tool._prepare_command("free -h")
        assert isinstance(prepared, str)
        assert "free()" in prepared

    def test_prepare_command_allows_native_commands(
        self, windows_tool: Bash
    ) -> None:
        prepared = windows_tool._prepare_command("ls -la")
        assert prepared == "ls -la"

    async def test_call_returns_error_message_without_executing(
        self, windows_tool: Bash, mock_session: MagicMock
    ) -> None:
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask"
        ) as process_task:
            result = await windows_tool(BashParams(cmd="journalctl -u svc -f"))
        assert isinstance(result, ToolError)
        assert "journalctl" in result.message
        process_task.assert_not_called()  # rejected before any subprocess spawn


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows Git Bash")
class TestBashFixCommonCommandsRealGitBash:
    """Execute the new cheat-sheet fallbacks on the real Git Bash of this host.

    A fallback is dormant whenever the real executable exists on PATH (the
    definition's ``command -v`` guard), so cases whose tool is installed
    natively (e.g. a third-party coreutils ``uptime``) are skipped here — the
    guard, not the body, is what this host exercises for them.
    """

    @staticmethod
    def _native_tool(name: str) -> bool:
        """Mirror the fallback guard: does ``command -v`` find *name* in bash?"""
        bash = find_bash()
        assert bash is not None
        found = subprocess.run(
            [bash, "-c", f"command -v {name}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return found.returncode == 0

    @staticmethod
    def _run(command: str, *, timeout: int = 90) -> subprocess.CompletedProcess[str]:
        bash = find_bash()
        assert bash is not None
        fixed = _fix_for_windows(command)
        for attempt in range(2):
            try:
                return subprocess.run(
                    [bash, "-lc", fixed.command],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                if attempt == 1:
                    raise
                time.sleep(1)
        raise AssertionError("unreachable")

    #: Windows service the ``systemctl`` fallback cases target: the Windows
    #: Time service exists on every Windows install.
    _SYSTEMCTL_SERVICE = "w32time"

    @classmethod
    def _systemctl_state(cls) -> tuple[bool, bool]:
        """Return ``(running, disabled)`` for :attr:`_SYSTEMCTL_SERVICE`.

        Host-truth probe, read through exactly the tools the fallback body
        reads: ``sc query`` (the ``running`` token ``is-active`` greps for) and
        ``sc qc`` (the ``DISABLED`` start type ``is-enabled`` greps for).  The
        service's state is host-specific — it is *Stopped/Manual* on a machine
        whose time sync is off — so the expectations must follow it instead of
        assuming a running service.
        """
        name = cls._SYSTEMCTL_SERVICE
        state = subprocess.run(
            ["sc.exe", "query", name],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        ).stdout.upper()
        config = subprocess.run(
            ["sc.exe", "qc", name],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        ).stdout.upper()
        return "RUNNING" in state, "DISABLED" in config

    @classmethod
    def _systemctl_expectations(
        cls, command: str, needles: tuple[str, ...]
    ) -> tuple[tuple[str, ...], int]:
        """Return ``(needles, returncode)`` for *command*.

        ``status`` / ``is-active`` / ``is-enabled`` report the host's real
        service state, so their parametrized needles (the values a host with a
        *running* service sees) and exit code are replaced by the state-aware
        ones — the fallback answers ``Running``/``active``/``enabled`` only
        when the service really is, which is what systemctl does:

        * ``status``    -> ``Running`` | ``Stopped`` (exit 0),
        * ``is-active`` -> ``active``  (exit 0) | ``inactive`` (exit 3),
        * ``is-enabled``-> ``enabled`` (exit 0) | ``disabled`` (exit 1).

        Every other command keeps its parametrized needles and exit code 0.
        """
        parts = command.split()
        subcommand = parts[1] if len(parts) > 1 else ""
        if not parts or parts[0] != "systemctl" or subcommand not in {
            "status",
            "is-active",
            "is-enabled",
        }:
            return needles, 0
        running, disabled = cls._systemctl_state()
        if subcommand == "status":
            return ("Running" if running else "Stopped",), 0
        if subcommand == "is-active":
            return (("active",), 0) if running else (("inactive",), 3)
        return (("disabled",), 1) if disabled else (("enabled",), 0)

    @pytest.mark.parametrize(
        ("command", "needles"),
        [
            ("free", ("Mem",)),
            ("free -h", ("Mem",)),
            ("free -m", ("Mem",)),
            ("ss -tln", ("TCP",)),
            ("ss -s", ()),
            ("man ls", ("Usage",)),
            ("man 1 grep", ("Usage",)),
            # ``systemctl`` needles spell the *running-service* answer; the
            # actual expectation follows this host's service state (see
            # ``_systemctl_expectations``).
            ("systemctl status w32time", ("Running",)),
            ("systemctl is-active w32time", ("active",)),
            ("systemctl is-enabled w32time", ("enabled",)),
            ("systemctl list-units", ("W32Time",)),
            ("top -b -n 1", ("ProcessName",)),
            ("htop -b -n 1", ("ProcessName",)),
        ],
    )
    def test_fallback_executes(
        self, command: str, needles: tuple[str, ...]
    ) -> None:
        name = command.split()[0]
        if self._native_tool(name):
            pytest.skip(f"native {name} on PATH; fallback guard is dormant here")
        needles, expected_returncode = self._systemctl_expectations(command, needles)
        result = self._run(command)
        output = result.stdout + result.stderr
        assert result.returncode == expected_returncode, output
        for needle in needles:
            assert needle in output, f"{needle!r} missing from {output[:500]!r}"

    @pytest.mark.parametrize(
        ("command", "needles"),
        [
            ("uptime", ("up", "load average")),
        ],
    )
    def test_fallback_executes_native_spelling(
        self, command: str, needles: tuple[str, ...]
    ) -> None:
        # Output spellings asserted here match this fallback's own body; skip
        # on hosts where a native tool (e.g. coreutils ``uptime``) wins.
        name = command.split()[0]
        if self._native_tool(name):
            pytest.skip(f"native {name} on PATH; fallback guard is dormant here")
        result = self._run(command)
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        for needle in needles:
            assert needle in output, f"{needle!r} missing from {output[:500]!r}"

    def test_ip_family_executes(self) -> None:
        if self._native_tool("ip"):
            pytest.skip("native ip on PATH; fallback guard is dormant here")
        for command in ("ip addr", "ip link", "ip route", "ip neigh"):
            result = self._run(command)
            assert result.returncode == 0, result.stdout + result.stderr
            assert result.stdout.strip(), command

    @pytest.mark.parametrize(
        ("command", "reason"),
        [
            ("ip", "Usage"),
            ("htop --badflag", "unsupported option"),
            ("free --nonsense", "unsupported option"),
            ("systemctl badcmd", "unsupported subcommand"),
            ("man", "missing command name"),
        ],
    )
    def test_fallback_error_paths_report_reason(
        self, command: str, reason: str
    ) -> None:
        name = command.split()[0]
        if self._native_tool(name):
            pytest.skip(f"native {name} on PATH; fallback guard is dormant here")
        result = self._run(command)
        output = result.stdout + result.stderr
        assert result.returncode != 0, output
        assert reason in output, f"{reason!r} missing from {output[:500]!r}"

    def test_journalctl_is_rejected_by_tool_not_bash(self) -> None:
        """The tool refuses before bash runs: 'command not found' never appears."""
        result = _fix_for_windows("journalctl -u svc")
        assert result.unsupported == ("journalctl",)
        assert "no Windows Git Bash equivalent" in result.warning


class TestBashFixRobustness:
    @pytest.mark.parametrize(
        "command",
        [
            "'",
            '"',
            "`",
            "$(",
            "${",
            "((",
            "cat <<EOF\nunterminated",
            "echo \\",
            "rev '",
            'rev "',
            "echo $(rev",
            "echo `rev",
            "if rev; then",
            "case x in rev)",
            "\x00rev\x00",
        ],
    )
    def test_arbitrary_malformed_input_never_crashes(self, command: str) -> None:
        result = _fix_for_windows(command)
        assert isinstance(result, BashFix)
        assert isinstance(result.command, str)

    def test_large_plain_command_fast_path(self) -> None:
        command = "printf x " + "argument " * 20_000
        assert _fix_for_windows(command) == BashFix(command)

    def test_deeply_nested_command_substitutions(self) -> None:
        depth = 250
        source = "echo " + "$(echo " * depth + "$(rev)" + ")" * depth
        result = _fix_for_windows(source)
        assert result.command.count("rev()") == 1
        assert result.command.endswith("\n" + source)

    def test_extreme_nesting_beyond_scan_bound_is_unchanged(self) -> None:
        # Pathological nesting must neither hang the scanner nor crash it:
        # beyond the recursion bound the content is left byte-for-byte for
        # Bash to handle.
        depth = 2_000
        source = "echo " + "$(" * depth + "pwd" + ")" * depth
        assert _fix_for_windows(source) == BashFix(source)

    def test_nested_substitutions_still_rewritten_at_moderate_depth(self) -> None:
        depth = 50
        source = "cd " + "$(cd " * depth + "D:\\x" + ")" * depth
        result = _fix_for_windows(source)
        assert result.path_changes == ("D:\\x",)
        expected = "cd " + "$(cd " * depth + "D:/x" + ")" * depth
        assert result.command == expected

    def test_many_commands_are_all_rewritten_linearly(self) -> None:
        source = "; ".join(["rev"] * 2_000)
        result = _fix_for_windows(source)
        assert result.command.endswith("\n" + source)
        assert result.command.count("rev()") == 1
        assert result.replacements == ("rev",) * 2_000


class TestBashFixWindowsPaths:
    """Windows backslash paths and cmd.exe idioms rewritten for Git Bash."""

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("cd D:\\kimi-agent\\kimi-cli", "cd D:/kimi-agent/kimi-cli"),
            ("cd d:\\tools\\build", "cd d:/tools/build"),
            ("cd C:\\", "cd C:/"),
            ("cd \\\\server\\share\\dir", "cd //server/share/dir"),
            ("cd \\Users\\foo", "cd /Users/foo"),
            ("cd ~\\Desktop\\file.txt", "cd ~/Desktop/file.txt"),
            ("cd .\\build\\dist", "cd ./build/dist"),
            ("cd ..\\src\\lib", "cd ../src/lib"),
            ("cd ..\\..\\repo", "cd ../../repo"),
            ("mkdir build\\dist\\assets", "mkdir build/dist/assets"),
            ("cd foo\\\\bar", "cd foo/bar"),
            ("cd D:\\temp\\cache\\", "cd D:/temp/cache/"),
            ("ls D:\\*.txt", "ls D:/*.txt"),
            ("echo D:\\foo", "echo D:/foo"),
            ("cd D:\\a\\b && cp D:\\a\\b.txt E:\\dest\\",
             "cd D:/a/b && cp D:/a/b.txt E:/dest/"),
            ("cd \\Program\\ Files\\x", 'cd "/Program Files/x"'),
            ("env -C D:\\x cmd", "env -C D:/x cmd"),
            ("env --chdir D:\\x cmd", "env --chdir D:/x cmd"),
            ("env --chdir=D:\\x cmd", "env --chdir=D:/x cmd"),
        ("time -o D:\\out.txt cmd", "time -o D:/out.txt cmd"),
        ("time --output=D:\\out.txt cmd", "time --output=D:/out.txt cmd"),
        # A Windows executable path as the command word itself: Bash
        # quote removal would eat the backslashes and lose the command.
        ("env -u FOO D:\\x cmd", "env -u FOO D:/x cmd"),
        ],
    )
    def test_windows_backslash_paths_rewritten(
        self, source: str, expected: str
    ) -> None:
        result = _fix_for_windows(source)
        assert result.command == expected
        assert result.changed
        assert result.replacements == ()
        assert result.path_changes

    def test_sudo_path_option_rewritten_with_fallback_prefix(self) -> None:
        # ``sudo`` is also a fallback name now (for hosts without sudo.exe),
        # so its definition is prepended; the wrapped path option is still
        # rewritten exactly like the other wrappers.
        result = _fix_for_windows(r"sudo -D D:\x cmd")
        assert result.path_changes == (r"D:\x",)
        assert result.replacements == ("sudo",)
        assert result.command.split("\n")[-1] == "sudo -D D:/x cmd"
        assert "command -v sudo" in result.command

    def test_path_with_spaces_is_quoted(self) -> None:
        result = _fix_for_windows("cd D:\\Program\\ Files\\Git")
        assert result.command == 'cd "D:/Program Files/Git"'
        assert result.path_changes == ("D:\\Program\\ Files\\Git",)

    def test_path_with_metacharacter_is_quoted(self) -> None:
        result = _fix_for_windows("cd D:\\a\\&b\\c")
        assert result.command == 'cd "D:/a&b/c"'

    def test_tilde_with_spaces_keeps_tilde_expanding(self) -> None:
        # ``~"..."`` would NOT expand: a tilde followed by a quote is a
        # literal tilde.  The slash must stay outside the quotes so the
        # rewritten word still expands the home directory (``~/"..."``).
        result = _fix_for_windows("cd ~\\My\\ Docs\\x")
        assert result.command == 'cd ~/"My Docs/x"'

    def test_path_change_reports_warning(self) -> None:
        result = _fix_for_windows("cd D:\\x")
        assert result.changed
        assert result.replacements == ()
        assert "forward slashes" in result.warning

    def test_cd_d_flag_dropped(self) -> None:
        result = _fix_for_windows("cd /d D:\\kimi-agent\\kimi-cli")
        assert result.command == "cd  D:/kimi-agent/kimi-cli"
        assert result.changed
        assert result.path_changes == ("cd /d", "D:\\kimi-agent\\kimi-cli")

    def test_cd_upper_d_flag_dropped(self) -> None:
        result = _fix_for_windows("cd /D C:\\Users\\me")
        assert result.command == "cd  C:/Users/me"

    def test_cd_flag_requires_following_argument(self) -> None:
        assert _fix_for_windows("cd /d") == BashFix("cd /d")
        assert _fix_for_windows("cd /d && echo x") == BashFix("cd /d && echo x")
        assert _fix_for_windows("cd /d # comment") == BashFix("cd /d # comment")

    def test_cd_flag_dropped_with_quoted_path_preserved(self) -> None:
        result = _fix_for_windows("cd /d 'D:\\x'")
        assert result.command == "cd  'D:\\x'"

    def test_multiple_cd_flags_on_one_line(self) -> None:
        result = _fix_for_windows("cd D:\\x; cd /d D:\\y")
        assert result.command == "cd D:/x; cd  D:/y"

    def test_path_in_command_substitution(self) -> None:
        result = _fix_for_windows("echo $(cd D:\\foo && pwd)")
        assert result.command == "echo $(cd D:/foo && pwd)"

    def test_redirection_target_path_rewritten(self) -> None:
        result = _fix_for_windows("echo hi > D:\\out.txt")
        assert result.command == "echo hi > D:/out.txt"

    def test_path_after_environment_assignment_is_data(self) -> None:
        # `env` consumes FOO=... as its own word: the value is data.
        assert _fix_for_windows("env FOO=D:\\x true") == BashFix("env FOO=D:\\x true")

    @pytest.mark.parametrize(
        "command",
        [
            "echo foo\\bar",  # single-segment relative: ambiguous
            "echo a\\nb",  # looks like an accidental escape
            "grep a\\b file",
            "echo \\n",
            "echo 'D:\\foo'",  # single-quoted text is literal data
            'echo "D:\\foo"',  # double-quoted text is literal data
            "printf '%s\\n' a",  # tool-level escapes stay quoted
            "echo -e 'a\\tb'",
            "echo $PATH\\foo",  # expansions untouched
            "echo ${DIR}\\x",
            "x=D:\\foo",  # assignment values untouched
            "alias cd=D:\\x",
            "case value in D:\\x) echo hit;; esac",
            "[[ -d D:\\x ]]",
            "# cd D:\\x",
            "echo ok # cd D:\\x",
            "cat <<'EOF'\ncd D:\\x\nEOF",  # heredoc body is data
            "cat <<< D:\\x",  # here-string body is data
            "echo \\",  # bare backslash
            "echo \\\\",  # bare double backslash
            # Bash escape sequences are ambiguous, not Windows paths: the
            # rewrite must never turn them into slashed directory words.
            r"echo \a\b",  # single-letter root-relative escapes
            r"echo \n\t",
            r"printf \033\015",  # octal escapes
            r"echo \x\41",  # hex escape style
            r"echo \033\033",
            r"echo x\n\t",  # relative escape sequences
            r"echo a\033\015",
            r"cd \a\b\c",  # all single-letter root segments
            r"cd a\b\c",  # all single-letter relative segments
            # wrapper option values that are not paths stay data
            r"env -C 'D:\x' cmd",  # quoted option value is literal data
            r"time -o log.txt cmd",
                          r"env -u D:\x cmd",  # -u takes a name; the value is opaque data
            r"env -C /d cmd",  # no backslash: not a Windows path
            # backslash-newline line continuations inside path words: the word
            # spans two physical lines and must stay untouched so Bash performs
            # the continuation itself (rewriting would inject a raw newline
            # into the emitted word and change the command's line structure).
            "cd \\Users\\foo\\\nb",
            "cd ~\\Desktop\\\nfile.txt",
            "cd D:\\a\\\nb",
            "cd build\\dist\\\nassets",
            "cd D:\\a\\\rb",  # escaped carriage return
        ],
    )
    def test_ambiguous_or_quoted_words_are_untouched(self, command: str) -> None:
        assert _fix_for_windows(command) == BashFix(command)

    def test_non_windows_platform_is_noop(self) -> None:
        result = _fix_for_platform("cd D:\\x", "linux")
        assert result == BashFix("cd D:\\x")
        assert not result.changed


class TestBashFixGitBashPosixPaths:
    """Git Bash virtual POSIX absolute paths rewritten for native tools.

    Git Bash accepts POSIX-style absolute paths that native Windows
    executables cannot resolve: ``/tmp`` is a virtual mount mapped to the
    real Windows temp directory (``cygpath -w /tmp``), and ``/c/...`` means
    ``C:/...``.  Rewriting them makes the same POSIX-style command behave
    identically under Git Bash and native POSIX bash.
    """

    @staticmethod
    def _tmp() -> str:
        import tempfile

        return tempfile.gettempdir().replace("\\", "/")

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("echo /tmp/x.txt", "echo {tmp}/x.txt"),
            ("cat > /tmp/out.txt", "cat > {tmp}/out.txt"),
            ("cd /tmp", "cd {tmp}"),
            ("rm -f /tmp/a /tmp/b", "rm -f {tmp}/a {tmp}/b"),
            ("echo /c/dev/file.cpp", "echo C:/dev/file.cpp"),
            ("echo /C/Dev/file.cpp", "echo C:/Dev/file.cpp"),
            ("cd /d/foo", "cd D:/foo"),
            (
                "/c/Windows/System32/where.exe cmd",
                "C:/Windows/System32/where.exe cmd",
            ),
            (
                "cd /c/dev/RoboCute && cat > /tmp/test_util.cpp <<'EOF'\nx\nEOF",
                "cd C:/dev/RoboCute && cat > {tmp}/test_util.cpp <<'EOF'\nx\nEOF",
            ),
            ("echo $(cat /tmp/x)", "echo $(cat {tmp}/x)"),
            ("env --chdir=/tmp cmd", "env --chdir={tmp} cmd"),
            ("arr=(/tmp/a.txt /c/b.txt)", "arr=({tmp}/a.txt C:/b.txt)"),
        ],
    )
    def test_git_bash_posix_paths_rewritten(
        self, source: str, expected: str
    ) -> None:
        expected = expected.format(tmp=self._tmp())
        result = _fix_for_windows(source)
        assert result.command == expected
        assert result.changed
        assert result.replacements == ()
        assert result.path_changes

    @pytest.mark.parametrize(
        "command",
        [
            "echo '/tmp/x'",  # single-quoted text is literal data
            'echo "/tmp/x"',  # double-quoted text is literal data
            "cat <<'EOF'\n/tmp/x\nEOF",  # heredoc body is data
            "cat <<< /tmp/x",  # here-string body is data
            "echo /tmpfile",  # not the /tmp mount
            "echo /d",  # bare single-letter mount: ambiguous (cd /d flag)
            "echo /c",  # bare single-letter mount stays
            "x=/tmp/x",  # assignment values untouched
            "alias cd=/c/x",
            "echo ok # cd /tmp/x",
        ],
    )
    def test_git_bash_posix_path_data_and_ambiguous_untouched(
        self, command: str
    ) -> None:
        assert _fix_for_windows(command) == BashFix(command)

    def test_cd_d_flag_is_not_mistaken_for_drive_path(self) -> None:
        # ``cd /d`` is cmd.exe's flag form and must keep working; only
        # ``/d/...`` (a Git Bash drive mount with a segment) is rewritten.
        result = _fix_for_windows("cd /d D:\\x && echo /d && cd /d/foo")
        assert result.command == "cd  D:/x && echo /d && cd D:/foo"
        assert result.path_changes == ("cd /d", "D:\\x", "/d/foo")

    def test_tmp_path_with_spaces_is_quoted(
        self, monkeypatch: Any
    ) -> None:
        import kimi_cli.tools.file.bash.bash_fix as bash_fix_module

        monkeypatch.setattr(
            bash_fix_module._shell,
            "_windows_temp_dir",
            lambda: "C:/Users/John Doe/AppData/Local/Temp",
        )
        result = _fix_for_windows("echo /tmp/x")
        assert result.command == 'echo "C:/Users/John Doe/AppData/Local/Temp/x"'

    def test_fallback_command_with_tmp_path(self) -> None:
        # Fallback definitions are prepended as a prefix; the rewritten source
        # (``chdir /tmp`` -> ``chdir <tmp>``) is the final line.
        result = _fix_for_windows("chdir /tmp")
        assert result.command.split("\n")[-1] == f"chdir {self._tmp()}"
        assert "chdir" in result.replacements
        assert result.path_changes == ("/tmp",)

    @pytest.mark.parametrize(
        "suffix",
        [
            "todns_${b}_${d}",  # braced parameter expansion
            "x_$i",  # plain variable expansion
            "${x:-default}",  # expansion with operator
            "$((1 + 2))-x",  # arithmetic expansion
            "a_$$_b",  # special parameter ($$)
            "a_$_-b",  # special parameter ($_)
            "x$'\t'y",  # ANSI-C quoting
            "pre_$/post",  # a `$` that starts no expansion stays literal
        ],
    )
    def test_expansions_in_virtual_path_preserved(
        self, suffix: str
    ) -> None:
        # Unquoted ``$``-expansions in a virtual path must survive the
        # rewrite verbatim and OUTSIDE quotes: quoting or escaping the
        # ``$`` would turn Bash's runtime parameter expansion into a
        # literal ``$`` and the tool would receive a path literally named
        # ``x_${b}``.
        result = _fix_for_windows(f"echo /tmp/{suffix}")
        if suffix.startswith("pre_$/"):
            # A literal ``$`` (no expansion) is re-emitted escaped inside
            # quotes so the word value keeps the literal dollar sign.
            expected = f'echo "{self._tmp()}/{suffix.replace("$", "\\$")}"'
        else:
            expected = f"echo {self._tmp()}/{suffix}"
        assert result.command == expected
        assert result.changed
        assert result.path_changes == (f"/tmp/{suffix}",)

    @pytest.mark.parametrize(
        "suffix",
        [
            "x_$(rev)",  # command substitution
            "x_`rev`",  # backquote command substitution
            "${x:-$(rev)}",  # command substitution inside parameter expansion
            'x_$"$(rev)"',  # locale string holding a command substitution
        ],
    )
    def test_command_substitution_in_virtual_path_left_untouched(
        self, suffix: str
    ) -> None:
        # The inner command context of a substitution has already been
        # scanned (fallbacks/path fixes inside it stay active); rewriting
        # the whole word would clash with those nested edits, so the word
        # is left byte-for-byte untouched.
        source = f"echo /tmp/{suffix}"
        result = _fix_for_windows(source)
        assert result.command.split("\n")[-1] == source
        if "rev" in suffix:
            assert "rev" in result.replacements
        assert f"/tmp/{suffix}" not in result.path_changes

    def test_inner_virtual_path_in_command_substitution_rewritten(self) -> None:
        # Only the nested virtual path inside the substitution is rewritten;
        # the outer word (which embeds the substitution) stays untouched.
        result = _fix_for_windows("echo /tmp/x$(cat /tmp/y)")
        assert result.command == f"echo /tmp/x$(cat {self._tmp()}/y)"
        assert result.path_changes == ("/tmp/y",)

    def test_expansion_with_spaced_tmp_dir_is_partially_quoted(
        self, monkeypatch: Any
    ) -> None:
        import kimi_cli.tools.file.bash.bash_fix as bash_fix_module

        monkeypatch.setattr(
            bash_fix_module._shell,
            "_windows_temp_dir",
            lambda: "C:/Users/John Doe/AppData/Local/Temp",
        )
        result = _fix_for_windows("echo /tmp/${x}")
        assert (
            result.command
            == 'echo "C:/Users/John Doe/AppData/Local/Temp/"${x}'
        )

    def test_drive_mount_expansion_preserved(self) -> None:
        result = _fix_for_windows("echo /c/dev/$f")
        assert result.command == "echo C:/dev/$f"
        assert result.path_changes == ("/c/dev/$f",)

    @pytest.mark.parametrize(
        ("suffix", "emitted"),
        [
            # Bash unquoted-word backslash escapes decode to the escaped
            # character; the rewritten word must carry the same VALUE.
            ("a\\ b", "a b"),  # \\ = escaped space
            ("a\\$b", "a\\$b"),  # \\$ = literal dollar
            ("a\\&b", "a&b"),  # \\& = literal ampersand
            ("a\\\\b", "a\\\\b"),  # \\\\ = one literal backslash
            ("a\\'b", "a'b"),  # \\' = literal quote (not re-escaped)
        ],
    )
    def test_virtual_path_escapes_decoded_before_quoting(
        self, suffix: str, emitted: str
    ) -> None:
        result = _fix_for_windows(f"echo /tmp/{suffix}")
        assert result.command == f'echo "{self._tmp()}/{emitted}"'
        assert result.changed
        assert result.path_changes == (f"/tmp/{suffix}",)

    @pytest.mark.parametrize(
        "suffix",
        [
            "a\\*b",  # escaped glob metachar: re-emitting unquoted would
            "a\\?b",  # re-enable pathname expansion the escape suppressed
            "a\\[1]b",
            "a\\",  # trailing backslash: unterminated line continuation
        ],
    )
    def test_virtual_path_unsafe_escapes_left_untouched(self, suffix: str) -> None:
        command = f"echo /tmp/{suffix}"
        assert _fix_for_windows(command) == BashFix(command)

    @pytest.mark.parametrize(
        "suffix",
        [
            'x"$y"',  # quoted expansion: quotes must not become literal chars
            '"a b"',  # quoted segment with a space
            "x'a'b",  # single-quoted segment
            'x"$"',  # quoted special parameter
        ],
    )
    def test_virtual_path_with_quoted_segments_left_untouched(
        self, suffix: str
    ) -> None:
        # A quote char inside the raw word opens a quoted region whose
        # removal is part of the word's value; the rewriter cannot re-emit
        # that faithfully, so the word stays byte-for-byte untouched.
        command = f"echo /tmp/{suffix}"
        assert _fix_for_windows(command) == BashFix(command)

    @pytest.mark.parametrize(
        ("suffix", "emitted"),
        [
            ("{a,b}", "{a,b}"),  # list brace expansion keeps working
            ("pre{a,b}post", "pre{a,b}post"),
            ("{a..c}", "{a..c}"),  # sequence brace expansion
            ("x{a,b}/y", "x{a,b}/y"),  # braces mid-path
            ("a{b", "a{b"),  # unbalanced brace: literal, stays unquoted
        ],
    )
    def test_virtual_path_brace_expansion_preserved(
        self, suffix: str, emitted: str
    ) -> None:
        # Brace metacharacters must stay unquoted (like glob ones): quoting
        # them would suppress brace expansion the original word performs.
        result = _fix_for_windows(f"echo /tmp/{suffix}")
        assert result.command == f"echo {self._tmp()}/{emitted}"
        assert result.changed
        assert result.path_changes == (f"/tmp/{suffix}",)

    @pytest.mark.parametrize(
        "suffix",
        [
            "a\\{b",  # escaped brace: re-emitting unquoted would let the
            "a\\{b,c\\}d",  # decoded brace participate in expansion the
            "\\}x",  # escape suppressed — leave the word untouched
        ],
    )
    def test_virtual_path_escaped_braces_left_untouched(self, suffix: str) -> None:
        command = f"echo /tmp/{suffix}"
        assert _fix_for_windows(command) == BashFix(command)

    def test_virtual_path_line_continuation_decoded(self) -> None:
        # ``\`` + newline joins the value; the rewritten single-line word
        # carries the same value.
        result = _fix_for_windows("echo /tmp/a\\\nb")
        assert result.command == f"echo {self._tmp()}/ab"
        assert result.changed

    def test_drive_mount_escapes_decoded_before_quoting(self) -> None:
        result = _fix_for_windows("echo /c/a\\ b")
        assert result.command == 'echo "C:/a b"'
        assert result.changed

    def test_non_windows_is_noop(self) -> None:
        result = _fix_for_platform("cat /tmp/x && echo /c/y", "linux")
        assert result == BashFix("cat /tmp/x && echo /c/y")
        assert not result.changed


class TestBashFixArrayLiterals:
    """Windows path elements inside array literals rewritten for Git Bash.

    Array elements are data, not commands: quote removal would eat their
    backslashes (``arr=(D:\\x\\y)`` would store ``D:xy``), so unquoted
    element words get the same conservative rewrite as ordinary arguments.
    """

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("arr=(D:\\x\\y.txt D:\\a\\b.txt)", "arr=(D:/x/y.txt D:/a/b.txt)"),
            ("arr+=(D:\\x\\y.txt)", "arr+=(D:/x/y.txt)"),
            # Glob metacharacters stay unquoted for pathname expansion.
            ("arr=(C:\\data\\*.csv)", "arr=(C:/data/*.csv)"),
            ("declare -a arr=(D:\\x\\y.txt)", "declare -a arr=(D:/x/y.txt)"),
            ("declare -a arr=(D:\\x\\y.txt plain)", "declare -a arr=(D:/x/y.txt plain)"),
            ("local arr=(D:\\x\\y.txt)", "local arr=(D:/x/y.txt)"),
            ("arr=(D:\\Program\\ Files\\x.txt)", 'arr=("D:/Program Files/x.txt")'),
            ("arr=(D:\\x\\y.txt); echo ${arr[0]}", "arr=(D:/x/y.txt); echo ${arr[0]}"),
        ],
    )
    def test_array_element_paths_rewritten(
        self, source: str, expected: str
    ) -> None:
        result = _fix_for_windows(source)
        assert result.command == expected
        assert result.changed
        assert result.replacements == ()
        assert result.path_changes

    @pytest.mark.parametrize(
        "command",
        [
            "arr=('D:\\x\\y.txt')",  # quoted element is literal data
            'arr=("D:\\x\\y.txt")',
            "array=(rev open pbcopy)",  # element words are data, not commands
            "declare -a x=(rev)",  # no fallback injection for element data
            "declare -a x=(wget xclip xsel)",
            "arr=([k]=D:\\x)",  # associative key=value words stay opaque
            "arr=(a\\nb)",  # ambiguous escape-like element
        ],
    )
    def test_array_elements_stay_data(self, command: str) -> None:
        assert _fix_for_windows(command) == BashFix(command)

    def test_substitution_inside_array_is_scanned(self) -> None:
        result = _fix_for_windows("arr=($(rev <<< abc))")
        assert result.replacements == ("rev",)
        assert result.path_changes == ()


class TestBashFixCommandWordPaths:
    """Windows executable paths in command position rewritten for Git Bash.

    A command word such as ``C:\\tools\\rg.exe`` loses its backslashes to
    Bash quote removal (becoming the nonexistent ``C:toolsrg.exe``), so the
    same conservative path recognition used for argument words applies to
    the command word itself.
    """

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("C:\\Windows\\System32\\where.exe git", "C:/Windows/System32/where.exe git"),
            ("d:\\tools\\run.exe --help", "d:/tools/run.exe --help"),
            ("\\\\server\\share\\tool.exe arg", "//server/share/tool.exe arg"),
            (".\\build\\tool.exe arg", "./build/tool.exe arg"),
            ("..\\scripts\\run.sh", "../scripts/run.sh"),
            ("~\\bin\\tool.exe --help", "~/bin/tool.exe --help"),
            ("\\Users\\me\\tool.exe", "/Users/me/tool.exe"),
            ("build\\dist\\tool.exe arg", "build/dist/tool.exe arg"),
            ("echo a && C:\\x\\tool.exe", "echo a && C:/x/tool.exe"),
            ("echo a; C:\\x\\tool.exe | cat", "echo a; C:/x/tool.exe | cat"),
            ("(C:\\x\\tool.exe)", "(C:/x/tool.exe)"),
            ("{ C:\\x\\tool.exe; }", "{ C:/x/tool.exe; }"),
            ("if C:\\x\\probe.exe; then echo ok; fi", "if C:/x/probe.exe; then echo ok; fi"),
            ("while C:\\x\\poll.exe; do :; done", "while C:/x/poll.exe; do :; done"),
            ("command C:\\x\\tool.exe", "command C:/x/tool.exe"),
            ("env FOO=1 D:\\x\\tool.exe", "env FOO=1 D:/x/tool.exe"),
            ("nohup D:\\x\\tool.exe &", "nohup D:/x/tool.exe &"),
            # Glob metacharacters stay unquoted for pathname expansion.
            ("D:\\x\\*.exe", "D:/x/*.exe"),
            # Normalized words that need it are double-quoted.
            ("D:\\Program\\ Files\\x.exe", '"D:/Program Files/x.exe"'),
            # Command words inside substitutions are rewritten too.
            ("x=$(C:\\x\\tool.exe)", "x=$(C:/x/tool.exe)"),
            ("echo `C:\\x\\tool.exe`", "echo `C:/x/tool.exe`"),
        ],
    )
    def test_command_word_paths_rewritten(
        self, source: str, expected: str
    ) -> None:
        result = _fix_for_windows(source)
        assert result.command == expected
        assert result.changed
        assert result.replacements == ()
        assert result.path_changes

    @pytest.mark.parametrize(
        "command",
        [
            "echo hello",  # plain command word: no backslash
            "git --version",
            "'C:\\x\\tool.exe' arg",  # quoted command word is literal data
            '"C:\\x\\tool.exe" arg',
            "foo\\bar arg",  # single-segment relative path stays ambiguous
            "a\\nb arg",  # too short: ambiguous escape sequence
            r"\a\b",  # single-letter root-relative escapes
            r"x\n\t",  # escape-sequence-like segments
            r"case $f in D:\x) echo ok;; esac",  # case pattern is data
            r"case $f in (D:\x) echo ok;; esac",
        ],
    )
    def test_command_word_non_paths_untouched(self, command: str) -> None:
        assert _fix_for_windows(command) == BashFix(command)

    def test_fallback_name_takes_priority_over_path_rewrite(self) -> None:
        # ``\rev`` is the escaped literal command name ``rev``: the native
        # fallback must be injected, never a path rewrite.
        result = _fix_for_windows("\\rev <<< abc")
        assert result.replacements == ("rev",)
        assert result.path_changes == ()

    def test_command_word_path_change_reports_warning(self) -> None:
        result = _fix_for_windows("C:\\x\\tool.exe")
        assert result.changed
        assert result.replacements == ()
        assert result.path_changes == ("C:\\x\\tool.exe",)
        assert "forward slashes" in result.warning


_CLIPBOARD_PROBE_OK: bool | None = None


def _clipboard_probe_ok() -> bool:
    """Return whether a Windows clipboard round-trip works in this session.

    ``clip.exe`` (and PowerShell ``Set-Clipboard``) fail with
    "ERROR: Access is denied." whenever the process runs outside an
    interactive window station (service context, CI runners, headless
    sessions).  The clipboard fallback round-trip tests below then exercise
    nothing under our control -- the OS clipboard itself is unavailable --
    so they must skip instead of hard-failing.  The probe mirrors exactly
    what those tests need: a ``clip.exe`` write plus a PowerShell read that
    observes the written token.
    """
    global _CLIPBOARD_PROBE_OK
    if _CLIPBOARD_PROBE_OK is None:
        bash = find_bash()
        ok = False
        if bash is not None:
            probe = (
                "printf __kimixclipprobe__ | clip.exe && "
                "powershell.exe -NoProfile -NonInteractive -Command "
                "'[string](Get-Clipboard)' | grep -q __kimixclipprobe__"
            )
            try:
                run = subprocess.run(
                    [bash, "-lc", probe],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                ok = run.returncode == 0
            except subprocess.TimeoutExpired:
                ok = False
        _CLIPBOARD_PROBE_OK = ok
    return _CLIPBOARD_PROBE_OK


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows Git Bash")
class TestBashFixRealGitBash:
    @staticmethod
    def _run(command: str, *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        bash = find_bash()
        assert bash is not None
        fixed = _fix_for_windows(command)
        # The full suite spawns hundreds of Git Bash processes back-to-back;
        # under load a fresh bash.exe can take longer than a 15 s first-time
        # startup (profile parsing + antivirus scanning) even though the
        # rewritten command itself completes in milliseconds.  These tests
        # pass in isolation, so the timeout is a resource-contention flake.
        # Retry once on TimeoutExpired to absorb the transient spike; a
        # genuine hang (a regression in the fixer) still fails after retry.
        for attempt in range(2):
            try:
                return subprocess.run(
                    [bash, "-lc", fixed.command],
                    input=stdin,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                if attempt == 1:
                    raise
                time.sleep(1)
        raise AssertionError("unreachable")

    def test_gtimeout_rewrite_executes(self) -> None:
        result = self._run("gtimeout 2 bash -c 'printf timeout-ok'")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "timeout-ok"

    def test_command_word_windows_path_executes(self) -> None:
        # ``C:\Windows\System32\where.exe`` is a Windows executable path in
        # command position: the rewrite must make it runnable in Git Bash.
        result = self._run(r"C:\Windows\System32\where.exe git")
        assert result.returncode == 0, result.stderr
        assert "git.exe" in result.stdout

    def test_array_literal_windows_paths_execute(self) -> None:
        result = self._run(
            r"arr=(D:\x\y.txt D:\a\b.txt); printf '%s' ${arr[0]}"
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "D:/x/y.txt"

    def test_wget_fallback_downloads_file(self, tmp_path: Path) -> None:
        fixture = tmp_path / "wget_fixture.txt"
        fixture.write_text("wget-fixture\n", encoding="utf-8")
        url = "file:///" + str(fixture).replace("\\", "/")
        out = str(tmp_path / "wget_out.txt").replace("\\", "/")
        result = self._run(f"wget -q -O {out} {url}")
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "wget_out.txt").read_text(encoding="utf-8") == "wget-fixture\n"

    def test_wget_fallback_stdout_mode(self, tmp_path: Path) -> None:
        fixture = tmp_path / "wget_fixture.txt"
        fixture.write_text("wget-fixture\n", encoding="utf-8")
        url = "file:///" + str(fixture).replace("\\", "/")
        result = self._run(f"wget -q -O- {url}")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "wget-fixture\n"

    def test_wget_fallback_rejects_unsupported_option(self) -> None:
        result = self._run("wget --recursive https://example.com")
        assert result.returncode == 1
        assert "unsupported option" in result.stderr

    def test_xclip_fallback_clipboard_roundtrip(self) -> None:
        if not _clipboard_probe_ok():
            pytest.skip("Windows clipboard not accessible in this session")
        result = self._run(
            "printf roundtrip | xclip -selection clipboard"
            " && xclip -selection clipboard -o"
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.rstrip("\r\n") == "roundtrip"

    def test_xsel_fallback_clipboard_roundtrip(self) -> None:
        if not _clipboard_probe_ok():
            pytest.skip("Windows clipboard not accessible in this session")
        result = self._run("printf seltest | xsel -b && xsel -bo")
        assert result.returncode == 0, result.stderr
        assert result.stdout.rstrip("\r\n") == "seltest"

    def test_xclip_fallback_rejects_unsupported_option(self) -> None:
        result = self._run("xclip -loops 3")
        assert result.returncode == 1
        assert "unsupported option" in result.stderr

    def test_gsed_fallback_executes(self) -> None:
        result = self._run("printf 'hello world\\n' | gsed 's/world/git-bash/'")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "hello git-bash\n"

    def test_gnu_g_prefix_fallback_executes(self) -> None:
        result = self._run("gawk 'BEGIN{print 2+3}'")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "5\n"

    def test_tree_fallback_lists_directory(self, tmp_path: Path) -> None:
        root = tmp_path / "tree_root"
        (root / "sub").mkdir(parents=True)
        (root / "a.txt").write_text("a", encoding="utf-8")
        (root / "sub" / "b.txt").write_text("b", encoding="utf-8")
        posix = str(root).replace("\\", "/")
        result = self._run(f"tree -a {posix}")
        assert result.returncode == 0, result.stderr
        assert "a.txt" in result.stdout
        assert "sub" in result.stdout
        assert "b.txt" in result.stdout

    def test_zip_fallback_roundtrip(self, tmp_path: Path) -> None:
        root = tmp_path / "zip_root"
        (root / "sub").mkdir(parents=True)
        (root / "a.txt").write_text("alpha\n", encoding="utf-8")
        (root / "sub" / "b.txt").write_text("beta\n", encoding="utf-8")
        posix_root = str(root).replace("\\", "/")
        posix_zip = str(tmp_path / "out.zip").replace("\\", "/")
        result = self._run(
            f"cd {posix_root} && zip -qr {posix_zip} a.txt sub"
        )
        assert result.returncode == 0, result.stderr
        import zipfile

        names = zipfile.ZipFile(tmp_path / "out.zip").namelist()
        assert "a.txt" in names
        assert "sub/b.txt" in names

    def test_zip_fallback_rejects_unsupported_option(self) -> None:
        result = self._run("zip --encrypt out.zip file.txt")
        assert result.returncode == 1
        assert "unsupported option" in result.stderr

    def test_nc_z_fallback_open_and_closed_ports(self) -> None:
        import socket
        import threading

        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        stop = threading.Event()

        def _accept() -> None:
            while not stop.is_set():
                try:
                    server.settimeout(0.2)
                    conn, _ = server.accept()
                    conn.close()
                except OSError:
                    continue

        thread = threading.Thread(target=_accept, daemon=True)
        thread.start()
        try:
            open_result = self._run(f"nc -z 127.0.0.1 {port}")
            assert open_result.returncode == 0, open_result.stderr
        finally:
            stop.set()
            server.close()
        closed_result = self._run("nc -z -w 2 127.0.0.1 1")
        assert closed_result.returncode == 1

    def test_nc_fallback_rejects_non_scan_mode(self) -> None:
        result = self._run("printf hi | nc 127.0.0.1 80")
        assert result.returncode == 1
        assert "only -z" in result.stderr

    def test_pgrep_fallback_finds_bash(self) -> None:
        result = self._run("pgrep bash")
        assert result.returncode == 0, result.stderr
        lines = result.stdout.split()
        assert lines
        assert all(line.isdigit() for line in lines)

    def test_pgrep_fallback_no_match_returns_one(self) -> None:
        result = self._run("pgrep kimix_no_such_process_name_zzz")
        assert result.returncode == 1

    def test_pkill_fallback_rejects_unsupported_option(self) -> None:
        result = self._run("pkill --signal KILL kimix_no_such_process_name_zzz")
        assert result.returncode == 1
        assert "unsupported option" in result.stderr

    def test_traceroute_fallback_localhost(self) -> None:
        result = self._run("traceroute -n -m 1 127.0.0.1")
        assert result.returncode == 0, result.stderr
        assert "127.0.0.1" in result.stdout

    def test_wl_clipboard_fallback_roundtrip(self) -> None:
        if not _clipboard_probe_ok():
            pytest.skip("Windows clipboard not accessible in this session")
        result = self._run("printf wltest | wl-copy && wl-paste")
        assert result.returncode == 0, result.stderr
        assert result.stdout.rstrip("\r\n") == "wltest"

    def test_python3_fallback_executes(self) -> None:
        result = self._run("python3 --version")
        assert result.returncode == 0, result.stderr
        assert "Python 3" in result.stdout

    def test_say_fallback_rejects_unsupported_option(self) -> None:
        result = self._run("say -v Alex hello")
        assert result.returncode == 1
        assert "unsupported option" in result.stderr

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("set -e; rev <<< abc; printf reached", "cba\nreached"),
            ("set -e; gtimeout 2 true; printf reached", "reached"),
        ],
    )
    def test_missing_native_delegate_survives_errexit(
        self, command: str, expected: str
    ) -> None:
        result = self._run(command)
        assert result.returncode == 0, result.stderr
        assert result.stdout == expected

    @pytest.mark.parametrize(
        "source",
        [
            "work() { rev <<< abc; }; work",
            "function work { rev <<< abc; }; work",
            "function work() { rev <<< abc; }; work",
        ],
    )
    def test_first_function_body_fallback_executes(self, source: str) -> None:
        result = self._run(source)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "cba\n"

    def test_quoted_heredoc_parenthesis_in_substitution_executes_literally(self) -> None:
        command = "printf '%s\\n' \"$(cat <<'EOF'\n)\n$(command rev)\nEOF\n)\""
        result = self._run(command)
        assert result.returncode == 0, result.stderr
        assert result.stdout == ")\n$(command rev)\n"

    def test_arithmetic_shift_in_substitution_does_not_hide_fallback(self) -> None:
        command = 'printf "%s\\n" "$(\n: $((1 << 2))\n)"\nrev <<< abc'
        result = self._run(command)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "\ncba\n"

    def test_case_pattern_in_substitution_runs_fallback(self) -> None:
        command = "printf '%s\\n' \"$(case x in x) rev <<< abc;; esac)\""
        result = self._run(command)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "cba\n"

    @pytest.mark.parametrize(
        ("subject", "pattern"),
        [
            ("$(printf x)", "x"),  # command substitution subject
            ("`printf x`", "x"),  # backquote substitution subject
            ("$((0))", "0"),  # arithmetic expansion subject
        ],
    )
    def test_case_subject_substitution_runs_fallback(
        self, subject: str, pattern: str
    ) -> None:
        result = self._run(f"case {subject} in {pattern}) rev <<< abc;; esac")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "cba\n"

    @pytest.mark.parametrize("terminator", [";;", ";&", ";;&"])
    def test_completed_case_in_substitution_preserves_later_fallback(
        self, terminator: str
    ) -> None:
        command = (
            "printf '%s\\n' \"$(case x in 'x') :"
            f"{terminator} esac)\"; rev <<< abc"
        )
        result = self._run(command)
        assert result.returncode == 0, result.stderr
        assert result.stdout.endswith("cba\n")

    def test_rev_rewrite_executes_for_stdin(self) -> None:
        result = self._run("rev", stdin="abc\n123\n")
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == ["cba", "321"]

    def test_rev_rewrite_executes_for_file(self, tmp_path: Path) -> None:
        source = tmp_path / "rev-input.txt"
        source.write_text("first\nsecond\n", encoding="utf-8")
        path = str(source).replace("\\", "/")
        result = self._run(f"rev {path}")
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == ["tsrif", "dnoces"]

    def test_windows_backslash_cd_executes(self, tmp_path: Path) -> None:
        backslash = str(tmp_path).replace("/", "\\").replace(" ", "\\ ")
        result = self._run(f"cd {backslash} && printf reached")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "reached"

    def test_cd_d_flag_rewrite_executes(self, tmp_path: Path) -> None:
        backslash = str(tmp_path).replace("/", "\\").replace(" ", "\\ ")
        result = self._run(f"cd /d {backslash} && printf ok")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "ok"

    def test_windows_path_with_space_executes(self, tmp_path: Path) -> None:
        target = tmp_path / "sub dir"
        target.mkdir()
        source_path = str(target).replace(" ", "\\ ")
        result = self._run(f"cd {source_path} && printf spaced")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "spaced"

    def test_escape_sequence_words_are_not_rewritten(self) -> None:
        # ``\a\b`` and ``\n\t`` are Bash escapes ("ab nt"), not paths; the
        # fixer must leave them alone so the shell keeps its meaning.
        result = self._run(r"echo \a\b \n\t")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "ab nt\n"

    def test_env_c_path_option_rewrite_executes(self, tmp_path: Path) -> None:
        backslash = str(tmp_path).replace("/", "\\")
        result = self._run(f"env -C {backslash} pwd")
        assert result.returncode == 0, result.stderr
        # Git Bash canonicalizes the user temp directory to its ``/tmp`` MSYS
        # mount, so only the target directory name is compared, not the full
        # spelling.
        assert result.stdout.strip().endswith("/" + tmp_path.name)

    def test_nested_and_chained_rewrites_execute(self) -> None:
        result = self._run("gtimeout 2 bash -c 'printf abc' | rev")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "cba"

    def test_rev_preserves_unicode_characters(self) -> None:
        result = self._run("printf 'aé漢\\n' | rev")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "漢éa\n"

    @pytest.mark.parametrize("option", ["-0", "--zero", "-0 --", "--zero --"])
    def test_rev_zero_delimited_mode(self, option: str) -> None:
        result = self._run(f"printf 'abc\\0def\\0' | rev {option}")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "cba\x00fed\x00"

    def test_rev_missing_file_returns_failure(self) -> None:
        result = self._run("rev /definitely/not/a/kimix-file")
        assert result.returncode != 0
        assert "kimix-file" in result.stderr

    def test_existing_function_takes_precedence_over_fallback(self) -> None:
        result = self._run("rev() { printf custom; }; rev </dev/null")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "custom"

    def test_inline_path_native_executable_takes_precedence(self, tmp_path: Path) -> None:
        native = tmp_path / "rev"
        native.write_text("#!/usr/bin/env bash\nprintf native-inline", encoding="utf-8")
        native.chmod(0o755)
        directory = str(tmp_path).replace("\\", "/")
        if len(directory) >= 3 and directory[1:3] == ":/":
            directory = "/" + directory[0].lower() + directory[2:]
        result = self._run(f"PATH='{directory}':$PATH rev </dev/null")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "native-inline"

    @pytest.mark.parametrize(
        "wrapped",
        [
            "command rev",
            "command -- rev",
            "env rev",
            "env -i rev",
            "env K=V rev",
            "env -u K rev",
            "nohup rev",
            "exec rev",
            "exec -a custom-rev rev",
        ],
    )
    def test_executable_wrappers_run_fallback(self, wrapped: str) -> None:
        result = self._run(f"printf 'abc\\n' | {wrapped}")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "cba\n"

    @pytest.mark.parametrize("wrapped", ["time rev", "time -p rev"])
    def test_time_keyword_runs_fallback(self, wrapped: str) -> None:
        result = self._run(f"{wrapped} <<< abc")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "cba\n"

    @pytest.mark.parametrize(
        ("wrapped", "expected_stdout"),
        [
            ("command 2>/dev/null rev", "cba\n"),
            ("env 2>/dev/null rev", "cba\n"),
            ("nohup 2>/dev/null rev", "cba\n"),
            ("exec 2>/dev/null rev", "cba\n"),
            ("command >$(printf /dev/null) rev", ""),
            ("command > >(cat) rev", "cba\n"),
        ],
    )
    def test_executable_wrapper_survives_redirection(
        self, wrapped: str, expected_stdout: str
    ) -> None:
        result = self._run(f"printf 'abc\\n' | {wrapped}")
        assert result.returncode == 0, result.stderr
        assert "command not found" not in result.stderr.lower()
        assert result.stdout == expected_stdout

    def test_command_default_path_does_not_use_caller_path(self, tmp_path: Path) -> None:
        custom = tmp_path / "rev"
        custom.write_text("#!/usr/bin/env bash\nprintf caller-path", encoding="utf-8")
        custom.chmod(0o755)
        directory = str(tmp_path).replace("\\", "/")
        if len(directory) >= 3 and directory[1:3] == ":/":
            directory = "/" + directory[0].lower() + directory[2:]
        result = self._run(f"PATH='{directory}':$PATH command -p rev")
        assert result.returncode == 127
        assert result.stdout == ""

    @pytest.mark.parametrize(
        "source",
        [
            "coproc rev <<< abc",
            "coproc worker { rev <<< abc; }",
            "coproc worker if rev <<< abc; then :; fi",
        ],
    )
    def test_coproc_runs_fallback(self, source: str) -> None:
        result = self._run(f"{source}; wait; printf '%s' \"${{COPROC_STATUS-}}\"")
        assert result.returncode == 0, result.stderr
        assert "command not found" not in result.stderr.lower()

    def test_conditionals_and_arithmetic_execute_unchanged(self) -> None:
        result = self._run(
            "[[ rev == rev && rev == rev ]] && rev=1 && (( rev )) && printf OK"
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "OK"

    def test_commands_in_expanding_contexts_execute(self) -> None:
        result = self._run(
            "case x in x) :;; esac; values=($(rev <<< abc)); "
            "cat <<EOF\n${values[0]} $(rev <<< def)\nEOF"
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "cba fed\n"

    def test_unknown_unmapped_command_still_fails_normally(self) -> None:
        result = self._run("setsid-kimix-command-that-does-not-exist")
        assert result.returncode == 127
        assert "command not found" in result.stderr.lower()

    def test_copy_fallback_executes(self, tmp_path: Path) -> None:
        source = tmp_path / "copy_src.txt"
        source.write_text("copy-fixture\n", encoding="utf-8")
        src = str(source).replace("\\", "/")
        dst = str(tmp_path / "copy_dst.txt").replace("\\", "/")
        result = self._run(f"copy {src} {dst}")
        assert result.returncode == 0, result.stderr
        assert Path(dst).read_text(encoding="utf-8") == "copy-fixture\n"

    def test_move_fallback_executes(self, tmp_path: Path) -> None:
        source = tmp_path / "move_src.txt"
        source.write_text("move-fixture\n", encoding="utf-8")
        src = str(source).replace("\\", "/")
        dst = str(tmp_path / "move_dst.txt").replace("\\", "/")
        result = self._run(f"move {src} {dst}")
        assert result.returncode == 0, result.stderr
        assert not source.exists()
        assert Path(dst).read_text(encoding="utf-8") == "move-fixture\n"

    @pytest.mark.parametrize("cmd", ["del", "erase"])
    def test_del_and_erase_fallback_executes(self, cmd: str, tmp_path: Path) -> None:
        target = tmp_path / f"{cmd}_target.txt"
        target.write_text("data", encoding="utf-8")
        path = str(target).replace("\\", "/")
        result = self._run(f"{cmd} {path}")
        assert result.returncode == 0, result.stderr
        assert not target.exists()

    @pytest.mark.parametrize("cmd", ["ren", "rename"])
    def test_ren_and_rename_fallback_executes(self, cmd: str, tmp_path: Path) -> None:
        source = tmp_path / f"{cmd}_src.txt"
        source.write_text("rename-fixture\n", encoding="utf-8")
        src = str(source).replace("\\", "/")
        dst = str(tmp_path / f"{cmd}_dst.txt").replace("\\", "/")
        result = self._run(f"{cmd} {src} {dst}")
        assert result.returncode == 0, result.stderr
        assert not source.exists()
        assert Path(dst).read_text(encoding="utf-8") == "rename-fixture\n"

    def test_md_and_rd_fallback_executes(self, tmp_path: Path) -> None:
        directory = str(tmp_path / "md_rd_dir").replace("\\", "/")
        result = self._run(f"md {directory} && rd {directory}")
        assert result.returncode == 0, result.stderr
        assert not (tmp_path / "md_rd_dir").exists()

    def test_chdir_fallback_executes(self) -> None:
        result = self._run("chdir /tmp && pwd")
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().endswith("/tmp")

    def test_mklink_fallback_creates_symlink(self, tmp_path: Path) -> None:
        source = tmp_path / "sym_target.txt"
        source.write_text("symlink-fixture\n", encoding="utf-8")
        src = str(source).replace("\\", "/")
        dst = str(tmp_path / "sym_link.txt").replace("\\", "/")
        result = self._run(f"mklink {dst} {src}")
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "sym_link.txt").exists()
        assert (
            tmp_path / "sym_link.txt"
        ).read_text(encoding="utf-8") == "symlink-fixture\n"

    def test_mklink_hard_link_fallback_executes(self, tmp_path: Path) -> None:
        source = tmp_path / "hard_src.txt"
        source.write_text("hard-link-fixture\n", encoding="utf-8")
        src = str(source).replace("\\", "/")
        dst = str(tmp_path / "hard_dst.txt").replace("\\", "/")
        result = self._run(f"mklink /H {dst} {src}")
        assert result.returncode == 0, result.stderr
        assert Path(dst).read_text(encoding="utf-8") == "hard-link-fixture\n"

    def test_pidof_fallback_finds_bash(self) -> None:
        result = self._run("pidof bash")
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip()
        assert all(p.strip().isdigit() for p in result.stdout.split())

    def test_killall_nonexistent_returns_one(self) -> None:
        result = self._run("killall kimix_no_such_process_name_zzz")
        assert result.returncode == 1

    def test_column_fallback_formats_table(self, tmp_path: Path) -> None:
        source = tmp_path / "column_input.txt"
        source.write_text("alpha beta\ngamma delta\n", encoding="utf-8")
        path = str(source).replace("\\", "/")
        result = self._run(f"column -t {path}")
        assert result.returncode == 0, result.stderr
        assert "alpha" in result.stdout
        assert "beta" in result.stdout


# ============================================================================
# _prepare_bash_cmd
# ============================================================================

class TestPrepareBashCmd:
    def test_noop_on_non_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "linux"):
            assert _prepare_bash_cmd("echo hello") == "echo hello"

    def test_noop_on_darwin(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "darwin"):
            assert _prepare_bash_cmd("echo hello") == "echo hello"

    def test_noop_on_windows_without_backslash(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            assert _prepare_bash_cmd("echo hello") == "echo hello"

    def test_converts_unquoted_backslashes_on_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r"cat kimi-cli\src\kimi_cli\tools\file\bash\bash_tool.py"
            result = _prepare_bash_cmd(cmd)
            assert result == "cat kimi-cli/src/kimi_cli/tools/file/bash/bash_tool.py"

    def test_preserves_single_quotes_on_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = "echo 'hello world'"
            result = _prepare_bash_cmd(cmd)
            assert result == "echo 'hello world'"

    def test_preserves_backslashes_inside_single_quotes_on_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r"echo 'hello\world'"
            result = _prepare_bash_cmd(cmd)
            assert result == r"echo 'hello\world'"

    def test_preserves_backslashes_inside_double_quotes_on_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r'echo "hello\world"'
            result = _prepare_bash_cmd(cmd)
            assert result == r'echo "hello\world"'

    def test_preserves_backslashes_inside_ansi_c_quotes_on_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r"echo $'hello\nworld'"
            result = _prepare_bash_cmd(cmd)
            assert result == r"echo $'hello\nworld'"

    def test_empty_command_on_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            assert _prepare_bash_cmd("") == ""

    def test_pipes_and_redirects_on_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = "echo hello | grep h > out.txt"
            result = _prepare_bash_cmd(cmd)
            assert result == "echo hello | grep h > out.txt"

    def test_drive_letter_path_on_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r"cat C:\Users\test\file.txt"
            result = _prepare_bash_cmd(cmd)
            assert result == "cat C:/Users/test/file.txt"

    def test_relative_paths_on_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            assert _prepare_bash_cmd(r"cd .\subdir") == "cd ./subdir"
            assert _prepare_bash_cmd(r"cd ..\parent") == "cd ../parent"

    def test_multiple_paths_in_one_command_on_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r"diff a\b\c.py x\y\z.py"
            assert _prepare_bash_cmd(cmd) == "diff a/b/c.py x/y/z.py"

    def test_mixed_quoted_and_unquoted_backslashes_on_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r"cat 'src\a.py' src\b.py"
            assert _prepare_bash_cmd(cmd) == r"cat 'src\a.py' src/b.py"

    def test_escaped_quote_inside_double_quotes_on_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r'echo "hello \"world\""'
            assert _prepare_bash_cmd(cmd) == r'echo "hello \"world\""'

    def test_unclosed_single_quote_on_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r"echo 'hello src\file.py"
            assert _prepare_bash_cmd(cmd) == r"echo 'hello src\file.py"

    def test_unclosed_double_quote_on_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r'echo "hello src\file.py'
            assert _prepare_bash_cmd(cmd) == r'echo "hello src\file.py'

    def test_dollar_quote_with_escaped_single_quote_on_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r"echo $'it\'s working'"
            assert _prepare_bash_cmd(cmd) == r"echo $'it\'s working'"

    def test_backslash_before_special_chars_preserved_on_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            # Backslash escapes before bash metacharacters are preserved
            assert _prepare_bash_cmd(r"echo a\|b") == r"echo a\|b"
            assert _prepare_bash_cmd(r"echo a\;b") == r"echo a\;b"
            assert _prepare_bash_cmd(r"echo a\&b") == r"echo a\&b"
            assert _prepare_bash_cmd(r"echo a\>b") == r"echo a\>b"
            assert _prepare_bash_cmd(r"echo a\<b") == r"echo a\<b"

    def test_double_backslash_outside_quotes_on_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            # Each backslash is converted individually (\\ -> //)
            assert _prepare_bash_cmd(r"echo \\path") == "echo //path"

    def test_backslash_at_end_of_string_on_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            assert _prepare_bash_cmd("echo trailing\\") == "echo trailing/"

    def test_pipes_and_redirects_with_paths_on_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r"cat src\a.py | grep x > out\b.txt"
            assert _prepare_bash_cmd(cmd) == "cat src/a.py | grep x > out/b.txt"

    def test_preserves_quoted_path_with_spaces_on_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r'cat "C:\Program Files\app\file.txt"'
            assert _prepare_bash_cmd(cmd) == r'cat "C:\Program Files\app\file.txt"'

    def test_preserves_single_quoted_path_with_spaces_on_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r"cat 'C:\Program Files\app\file.txt'"
            assert _prepare_bash_cmd(cmd) == r"cat 'C:\Program Files\app\file.txt'"

    def test_command_substitution_with_backslashes_on_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            # $(...) is not a quoted region; backslashes inside are converted
            cmd = r"echo $(cat src\file.py)"
            assert _prepare_bash_cmd(cmd) == "echo $(cat src/file.py)"

    def test_backtick_with_backslashes_on_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            # Backticks are not a quoted region; backslashes inside are converted
            cmd = r"echo `cat src\file.py`"
            assert _prepare_bash_cmd(cmd) == "echo `cat src/file.py`"

    def test_find_command_with_escaped_parens_on_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r'find build -maxdepth 4 \( -name "luisa-xir*" -o -name "luisa-spirv*" \) | head -n 20'
            expected = r'find build -maxdepth 4 \( -name "luisa-xir*" -o -name "luisa-spirv*" \) | head -n 20'
            assert _prepare_bash_cmd(cmd) == expected

    def test_backslash_space_preserved_on_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            # Backslash-escaped space must be preserved so the word remains single token
            assert _prepare_bash_cmd(r"echo hello\ world") == r"echo hello\ world"

    def test_backslash_dollar_preserved_on_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            assert _prepare_bash_cmd(r"echo \$HOME") == r"echo \$HOME"

    def test_backslash_star_preserved_on_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            assert _prepare_bash_cmd(r"echo \*") == r"echo \*"

    def test_backslash_backtick_preserved_on_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            assert _prepare_bash_cmd(r"echo \`cmd\`") == r"echo \`cmd\`"

    def test_backslash_brace_preserved_on_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            assert _prepare_bash_cmd(r"echo \{a,b\}") == r"echo \{a,b\}"

    def test_backslash_tilde_preserved_on_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            assert _prepare_bash_cmd(r"echo \~user") == r"echo \~user"

    def test_mixed_paths_and_escapes_on_windows(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r"cat src\tools\file.py && find build \( -name '*.py' \)"
            expected = r"cat src/tools/file.py && find build \( -name '*.py' \)"
            assert _prepare_bash_cmd(cmd) == expected

    def test_escaped_single_quote_outside_quotes_on_windows(self) -> None:
        """\' outside quotes should be preserved and NOT start a single-quoted region."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            # \' → literal ', backslashes after should be converted
            cmd = r"echo \'src\kimix\'"
            expected = r"echo \'src/kimix\'"
            assert _prepare_bash_cmd(cmd) == expected

    def test_escaped_double_quote_outside_quotes_on_windows(self) -> None:
        r"""\" outside quotes should be preserved and NOT start a double-quoted region."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            # \" outside quotes: backslash escapes the double-quote → literal "
            # The " should NOT start a double-quoted region.
            cmd = r'echo \"src\kimix\"'
            expected = r'echo \"src/kimix\"'
            assert _prepare_bash_cmd(cmd) == expected

    def test_escaped_dollar_prevents_ansi_c_detection_on_windows(self) -> None:
        """Escaped dollar before single-quote should NOT trigger ANSI-C processing."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            # \$'text' — the $ is escaped, so 'text' is a separate single-quoted string
            cmd = r"echo \$'text'"
            expected = r"echo \$'text'"
            assert _prepare_bash_cmd(cmd) == expected

    # -- corner cases discovered during review -------------------------------

    def test_double_quoted_escaped_backslash_before_quote_on_windows(self) -> None:
        r"""\\" inside double quotes: \\ is escaped backslash, then " closes the region."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            # Bash: "hello\\" is the quoted region, then world", then "
            cmd = r'"hello\\"world"'
            # \\ inside "..." preserved, then world is outside (no backslashes),
            # then " starts new region
            expected = r'"hello\\"world"'
            assert _prepare_bash_cmd(cmd) == expected

    def test_double_quoted_multiple_escaped_backslashes_on_windows(self) -> None:
        r"""Multiple \\ sequences inside double quotes."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r'"a\\b\\c"'
            expected = r'"a\\b\\c"'
            assert _prepare_bash_cmd(cmd) == expected

    def test_double_quoted_escaped_dollar_on_windows(self) -> None:
        r"""\$ inside double quotes should not affect region detection."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r'"price is \$100"'
            expected = r'"price is \$100"'
            assert _prepare_bash_cmd(cmd) == expected

    def test_double_quoted_with_dollar_ansi_c_inside_on_windows(self) -> None:
        r"""$' inside double quotes should NOT trigger ANSI-C processing."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            # "abc $'def' ghi" — the $' is inside double quotes, treated literally
            cmd = r'"abc $"' + "'def' ghi\""
            # The double-quoted region captures everything from first " to last "
            expected = r'"abc $"' + "'def' ghi\""
            assert _prepare_bash_cmd(cmd) == expected

    def test_backslash_before_hash_preserved_on_windows(self) -> None:
        r"""\# should be preserved as bash comment escape."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            assert _prepare_bash_cmd(r"echo \# not a comment") == r"echo \# not a comment"

    def test_backslash_before_exclamation_preserved_on_windows(self) -> None:
        r"""\! should be preserved as history expansion escape."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            assert _prepare_bash_cmd(r"echo \!test") == r"echo \!test"

    def test_backslash_before_percent_preserved_on_windows(self) -> None:
        r"""\% should be preserved."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            assert _prepare_bash_cmd(r"echo \%percent") == r"echo \%percent"

    def test_backslash_before_equals_preserved_on_windows(self) -> None:
        r"""\= should be preserved as assignment escape."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            assert _prepare_bash_cmd(r"echo a\=b") == r"echo a\=b"

    def test_triple_backslash_outside_quotes_on_windows(self) -> None:
        r"""\\\ outside quotes: \\ → //, then \ before p → /p → ///path."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            assert _prepare_bash_cmd(r"echo \\\path") == "echo ///path"

    def test_backslash_before_newline_preserved_on_windows(self) -> None:
        r"""\<newline> (line continuation) should be preserved."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = "echo hello\\\nworld"
            expected = "echo hello\\\nworld"
            assert _prepare_bash_cmd(cmd) == expected

    def test_ansi_c_with_double_backslash_before_quote_on_windows(self) -> None:
        r"""$'...\\'' — \\ inside ANSI-C, then ' closes the region."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            # $'it\\''s' → $'it\\' + 's' (the \\ produces \, then ' closes)
            cmd = r"echo $'it\\'s working'"
            expected = r"echo $'it\\'s working'"
            assert _prepare_bash_cmd(cmd) == expected

    def test_ansi_c_with_hex_escape_on_windows(self) -> None:
        r"""$'...\x41...' — hex escapes are skipped correctly."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r"echo $'\x41bc'"
            expected = r"echo $'\x41bc'"
            assert _prepare_bash_cmd(cmd) == expected

    def test_ansi_c_with_octal_escape_on_windows(self) -> None:
        r"""$'...\033...' — octal escapes are skipped correctly."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r"echo $'\033[31mred'"
            expected = r"echo $'\033[31mred'"
            assert _prepare_bash_cmd(cmd) == expected

    def test_ansi_c_with_unicode_escape_on_windows(self) -> None:
        r"""$'...\u0041...' — unicode escapes."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r"echo $'\u0041bc'"
            expected = r"echo $'\u0041bc'"
            assert _prepare_bash_cmd(cmd) == expected

    def test_mixed_quotes_complex_on_windows(self) -> None:
        """Complex mix of quote types and backslashes."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            # 'single' preserved, "double" preserved, $'ansi' preserved, src\path → src/path
            cmd = "echo 'single' \"double\" $'ansi' src\\path"
            expected = "echo 'single' \"double\" $'ansi' src/path"
            assert _prepare_bash_cmd(cmd) == expected

    def test_only_backslashes_on_windows(self) -> None:
        """String with only backslashes."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            assert _prepare_bash_cmd("\\\\") == "//"
            assert _prepare_bash_cmd("\\") == "/"
            assert _prepare_bash_cmd("\\\\\\") == "///"

    def test_backslash_before_each_metachar_on_windows(self) -> None:
        """Every metacharacter preceded by backslash is preserved."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            metachars = "()|;&<>$\"`'*?[]{}~!#=% \t\n\r"
            for ch in metachars:
                # Build a command with \X where X is a metachar
                cmd = "echo \\" + ch
                result = _prepare_bash_cmd(cmd)
                # The \X pair should be preserved as \X
                assert ("\\" + ch) in result, f"Failed for \\{repr(ch)}: {result}"

    def test_double_quoted_empty_on_windows(self) -> None:
        """Empty double-quoted region."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            assert _prepare_bash_cmd('echo ""') == 'echo ""'

    def test_ansi_c_empty_on_windows(self) -> None:
        """Empty ANSI-C quoted region."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            assert _prepare_bash_cmd("echo $''") == "echo $''"

    def test_single_quoted_empty_on_windows(self) -> None:
        """Empty single-quoted region."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            assert _prepare_bash_cmd("echo ''") == "echo ''"

    def test_double_quoted_escaped_backslash_at_end_on_windows(self) -> None:
        r"""Double-quoted region with \\ at the very end."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            # "hello\\" — \\ inside, then " closes
            cmd = r'"hello\\"'
            expected = r'"hello\\"'
            assert _prepare_bash_cmd(cmd) == expected

    def test_double_quoted_escaped_backslash_and_quote_on_windows(self) -> None:
        r"""Double-quoted with \\\" — \\ (escaped backslash) then \" (escaped quote)."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            # "hello\\\"world" — \\ → \, \" → " (escaped quote, region continues)
            cmd = r'"hello\\\"world"'
            expected = r'"hello\\\"world"'
            assert _prepare_bash_cmd(cmd) == expected

    # -- corner case: $(...) and backticks inside double quotes ----------------
    # bash runs the content of a command substitution in a subshell where it is
    # parsed unquoted.  So backslashes inside $(...) or `...` must be processed
    # (converted to /) even when the substitution is nested inside "...".

    def test_dq_with_command_substitution_and_backslash_path_on_windows(self) -> None:
        r"""echo "$(cat src\foo\bar)" — backslashes inside $(...) within DQ are converted."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r'echo "$(cat src\foo\bar)"'
            assert _prepare_bash_cmd(cmd) == 'echo "$(cat src/foo/bar)"'

    def test_dq_with_backtick_substitution_and_backslash_path_on_windows(self) -> None:
        """Backticks inside DQ (unescaped) start a command substitution.

        bash runs the content in a subshell, so backslashes inside are
        processed (converted to /) just like at the top level.
        """
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r'echo "`cat src\foo\bar`"'
            assert _prepare_bash_cmd(cmd) == 'echo "`cat src/foo/bar`"'


    def test_dq_with_nested_command_substitution_on_windows(self) -> None:
        r"""Nested $(...) inside DQ — both levels process backslashes."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r'echo "$(cat $(echo src\foo\bar))"'
            assert _prepare_bash_cmd(cmd) == 'echo "$(cat $(echo src/foo/bar))"'

    def test_dq_with_backtick_inside_command_substitution_on_windows(self) -> None:
        r"""Backticks nested inside $(...) within DQ — content is processed."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r'echo "$(cat `echo src\foo`)"'
            assert _prepare_bash_cmd(cmd) == 'echo "$(cat `echo src/foo`)"'

    def test_dq_with_command_substitution_inside_backticks_on_windows(self) -> None:
        r"""$(...) nested inside `...` at top level — content is processed."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r'echo `cat $(echo src\foo\bar)`'
            assert _prepare_bash_cmd(cmd) == 'echo `cat $(echo src/foo/bar)`'

    def test_dq_with_quoted_path_and_command_subst_on_windows(self) -> None:
        r"""Mixed: quoted path (preserved) + $(...) substitution (converted)."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r'echo "literal src\foo" "$(cat src\bar\baz)"'
            assert _prepare_bash_cmd(cmd) == 'echo "literal src\\foo" "$(cat src/bar/baz)"'

    def test_dq_ansi_c_inside_command_substitution_on_windows(self) -> None:
        r"""$'...' inside $(...) within DQ — ANSI-C region is preserved literally."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r'echo "$(echo $'"'"'\n'"'"')"'
            assert _prepare_bash_cmd(cmd) == r'echo "$(echo $'"'"'\n'"'"')"'

    def test_dq_single_quotes_inside_command_substitution_on_windows(self) -> None:
        r"""Single-quoted path inside $(...) within DQ — backslashes preserved."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r'echo "$(cat '"'"'src\foo\bar'"'"')"'
            assert _prepare_bash_cmd(cmd) == r'echo "$(cat '"'"'src\foo\bar'"'"')"'

    def test_dq_escaped_dollar_paren_not_command_substitution_on_windows(self) -> None:
        r"""\$( inside DQ — the $ is escaped, so ( is NOT a command substitution."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r'echo "\$(not a sub) src\file"'
            # \$ makes $ literal; ( ) are regular; src\file is preserved by DQ.
            assert _prepare_bash_cmd(cmd) == r'echo "\$(not a sub) src\file"'

    def test_dq_escaped_backtick_not_substitution_on_windows(self) -> None:
        r"""\` inside DQ — the ` is escaped, so it's a literal backtick, not substitution."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r'echo "\`not a sub\` src\file"'
            # \` makes ` literal; src\file is preserved by DQ.
            assert _prepare_bash_cmd(cmd) == r'echo "\`not a sub\` src\file"'

    def test_dq_empty_command_substitution_on_windows(self) -> None:
        """Empty $(...) inside DQ."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            assert _prepare_bash_cmd('echo "$()"') == 'echo "$()"'

    def test_dq_empty_backticks_on_windows(self) -> None:
        """Empty `` `` `` inside DQ."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            assert _prepare_bash_cmd('echo "``"') == 'echo "``"'

    def test_unterminated_dq_with_command_substitution_on_windows(self) -> None:
        r"""Unterminated DQ that contains $( — passed through to bash to error."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r'echo "$(unterminated'
            assert _prepare_bash_cmd(cmd) == r'echo "$(unterminated'

    def test_unterminated_command_substitution_inside_dq_on_windows(self) -> None:
        r"""$(... with no matching ) inside DQ — passed through."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r'echo "$(no close paren"'
            assert _prepare_bash_cmd(cmd) == r'echo "$(no close paren"'

    def test_unterminated_backticks_inside_dq_on_windows(self) -> None:
        r"""Unterminated ` inside DQ — passed through."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r'echo "`no close"'
            assert _prepare_bash_cmd(cmd) == r'echo "`no close"'

    def test_dq_with_dq_inside_command_substitution_on_windows(self) -> None:
        r"""DQ inside $(...) inside DQ — inner DQ preserves its backslashes."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            # "$(echo "src\foo" rest)" — inner DQ preserves \, rest converted
            cmd = r'echo "$(echo "src\foo" rest\bar)"'
            assert _prepare_bash_cmd(cmd) == r'echo "$(echo "src\foo" rest/bar)"'

    def test_top_level_backtick_with_escaped_backtick_on_windows(self) -> None:
        r"""\` at top level — escaped backtick, literal, not substitution start."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r"echo \`not_sub\`"
            assert _prepare_bash_cmd(cmd) == r"echo \`not_sub\`"

    def test_top_level_nested_backticks_with_path_on_windows(self) -> None:
        """`` `cmd1`cmd2` `` style — backtick region content is processed."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            # Outer backtick runs `cmd src\file`, inner is just text
            cmd = r"echo `cat src\file.txt`"
            assert _prepare_bash_cmd(cmd) == "echo `cat src/file.txt`"

    def test_command_substitution_with_nested_parens_on_windows(self) -> None:
        r"""$(echo (nested) paren) — ) inside parens is balanced correctly."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r"echo $(echo (src\foo\bar))"
            # The ) after "bar" closes the $().  The ) at the end is a stray.
            # Actually: $(echo (src\foo\bar)) — opens $(, then echo (, then
            # content, then ) closes the inner paren, then )) closes the $().
            # Let's just verify it doesn't crash and paths are converted.
            result = _prepare_bash_cmd(cmd)
            assert "src/foo/bar" in result

    def test_dq_ansi_c_immediately_before_closing_quote_on_windows(self) -> None:
        r"""$'...' right before closing " in DQ — must not skip the closing quote."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            # "abc $'def'" — the ANSI-C region ends right before the closing "
            cmd = r'"abc $'"'"'def'"'"'"'
            assert _prepare_bash_cmd(cmd) == r'"abc $'"'"'def'"'"'"'

    def test_dq_backtick_immediately_before_closing_quote_on_windows(self) -> None:
        """Backtick region right before closing " in DQ."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            # "abc `def`" — backtick region ends right before the closing "
            cmd = '"abc `def`"'
            assert _prepare_bash_cmd(cmd) == '"abc `def`"'

    def test_dq_command_subst_immediately_before_closing_quote_on_windows(self) -> None:
        """$(...) right before closing " in DQ."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            # "abc $(echo x)" — command substitution ends right before the closing "
            cmd = '"abc $(echo x)"'
            assert _prepare_bash_cmd(cmd) == '"abc $(echo x)"'

    def test_dq_with_complex_nesting_on_windows(self) -> None:
        r"""Complex nesting: $(echo "$(echo src\foo)" `echo src\bar`)."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r'echo "$(echo "$(echo src\foo)" `echo src\bar`)"'
            # Both $() levels convert paths; inner DQ preserves its \
            expected = r'echo "$(echo "$(echo src/foo)" `echo src/bar`)"'
            assert _prepare_bash_cmd(cmd) == expected

    def test_unc_path_converted_on_windows(self) -> None:
        r"""UNC path \\server\share\file.txt → //server/share/file.txt."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r"cat \\server\share\file.txt"
            assert _prepare_bash_cmd(cmd) == "cat //server/share/file.txt"

    def test_dq_command_subst_with_backslashes_converted_on_windows(self) -> None:
        r"""$(...) nested in double quotes: its content is parsed unquoted, so
        backslash paths inside are converted."""
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            cmd = r'echo "$(cat C:\a\b.txt)"'
            assert _prepare_bash_cmd(cmd) == 'echo "$(cat C:/a/b.txt)"'


# ============================================================================
# Bash.__call__ — integration tests with backslash paths on Windows
# ============================================================================

@pytest.mark.skipif(
    not BASH_AVAILABLE,
    reason="Bash tool is not available on this platform",
)
@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Backslash path handling targets Git Bash on Windows",
)
class TestBashBackslashPaths:
    async def test_cat_with_backslash_path(self, mock_session: MagicMock, tmp_path: Path) -> None:
        f = tmp_path / "test.txt"
        f.write_text("hello slash", encoding="utf-8")
        bash = Bash(session=mock_session)
        # Unquoted Windows backslash path is auto-converted to forward slashes.
        params = BashParams(cmd=f"cat {f}")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "hello slash" in result.output

    async def test_ls_with_backslash_path(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd=r"ls kimi-cli\src\kimi_cli\tools\file\bash")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "bash_tool.py" in result.output

    async def test_cd_with_backslash_path(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd=r"cd kimi-cli\src\kimi_cli\tools\file\bash && pwd")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "bash" in result.output

    async def test_multiple_backslash_paths(self, mock_session: MagicMock, tmp_path: Path) -> None:
        f = tmp_path / "test.txt"
        f.write_text("hello slash", encoding="utf-8")
        bash = Bash(session=mock_session)
        params = BashParams(cmd=f"echo {tmp_path} && cat {f}")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "hello slash" in result.output

    async def test_quoted_backslash_path_preserved(self, mock_session: MagicMock, tmp_path: Path) -> None:
        f = tmp_path / "test.txt"
        f.write_text("hello slash", encoding="utf-8")
        bash = Bash(session=mock_session)
        # Backslashes inside single quotes are preserved; Git Bash resolves them.
        params = BashParams(cmd=f"cat '{f}'")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "hello slash" in result.output

    async def test_double_quoted_backslash_path_preserved(self, mock_session: MagicMock, tmp_path: Path) -> None:
        f = tmp_path / "test.txt"
        f.write_text("hello slash", encoding="utf-8")
        bash = Bash(session=mock_session)
        # Backslashes inside double quotes are preserved; Git Bash resolves them.
        params = BashParams(cmd=f'cat "{f}"')
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "hello slash" in result.output


# ============================================================================
# Bash.description — Windows-specific experience text
# ============================================================================

class TestBashDescription:
    """The Bash tool description carries the verified Windows slash rules."""

    @pytest.fixture
    def mock_session(self) -> MagicMock:
        session = MagicMock()
        session.custom_config.get.return_value = {}
        session.custom_data = {}
        return session

    def _make_tool(self, platform: str, mock_session: MagicMock) -> Bash:
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", platform), patch(
            "kimi_cli.tools.file.bash.bash_tool._should_enable_bash", return_value=True
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool.find_bash",
            return_value=(r"C:\Git\bin\bash.exe" if platform == "win32" else "/bin/bash"),
        ):
            return Bash(session=mock_session)


# ============================================================================
# Bash.__call__
# ============================================================================

@pytest.mark.skipif(
    not BASH_AVAILABLE,
    reason="Bash tool is not available on this platform",
)
class TestBashCall:
    async def test_echo_hello(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo hello")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "hello" in result.output

    async def test_true_command(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="true")
        result = await bash(params)
        assert isinstance(result, ToolOk)

    async def test_false_command(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="false")
        result = await bash(params)
        assert isinstance(result, ToolError)

    async def test_pipefail_surfaces_real_exit_code(
        self, mock_session: MagicMock
    ) -> None:
        """A failing producer piped to a consumer must report the producer's
        non-zero exit (one-shot commands run with ``set -o pipefail``), not the
        consumer's 0 that previously masked crashes behind ``| head``."""
        bash = Bash(session=mock_session)
        params = BashParams(cmd="false | head -1")
        result = await bash(params)
        assert isinstance(result, ToolError)
        assert "exit_code: 1" in result.output

    async def test_pipefail_keeps_success_pipeline(
        self, mock_session: MagicMock
    ) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo hi | head -1")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "hi" in result.output

    async def test_grep_no_match_reports_success(
        self, mock_session: MagicMock, tmp_path: Path
    ) -> None:
        """grep exit 1 (no matches) is a normal outcome: it must be a ToolOk
        with the meaning noted, not a 'failed ... run it again' error."""
        f = tmp_path / "sample.txt"
        f.write_text("nothing here\n", encoding="utf-8")
        posix = str(f).replace("\\", "/")
        bash = Bash(session=mock_session)
        params = BashParams(cmd=f"grep __definitely_missing__ {posix}")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "No matches found" in result.message
        assert "failed" not in result.message.lower()

    async def test_sigpipe_truncation_reports_success(
        self, mock_session: MagicMock
    ) -> None:
        """SIGPIPE (141) from a truncated pipeline is an expected outcome when
        the one-shot shell runs with pipefail."""
        bash = Bash(session=mock_session)
        params = BashParams(cmd="seq 1 100000 | head -1", timeout=30)
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "SIGPIPE" in result.message

    async def test_unknown_command_error(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="no_such_command_12345", timeout=5)
        result = await bash(params)
        assert isinstance(result, ToolError)
        assert "command not found" in result.output or "not found" in result.output.lower()

    async def test_ls_current_dir(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="ls .", timeout=10)
        result = await bash(params)
        assert isinstance(result, ToolOk)

    async def test_echo_with_multiple_args(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo hello world")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "hello world" in result.output

    async def test_echo_with_timeout(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo quick", timeout=30)
        result = await bash(params)
        assert isinstance(result, ToolOk)

    async def test_cat_file(self, mock_session: MagicMock, tmp_path: Path) -> None:
        f = tmp_path / "test.txt"
        f.write_text("hello cat", encoding="utf-8")
        bash = Bash(session=mock_session)
        # Use forward slashes so bash does not interpret backslashes as escapes
        posix_path = str(f).replace("\\", "/")
        params = BashParams(cmd=f"cat {posix_path}")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "hello cat" in result.output

    async def test_pwd(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="pwd")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert len(result.output) > 0

    async def test_whoami(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="whoami")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert len(result.output) > 0


    async def test_timeout(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="sleep 5", timeout=3)
        result = await bash(params)
        assert isinstance(result, ToolError)
        assert "Timeout" in result.brief

    async def test_bash_not_found_fallback(self, mock_session: MagicMock) -> None:
        """When bash is not found, Bash.__init__ raises SkipThisTool."""
        with patch("kimi_cli.tools.file.bash.bash_tool.find_bash", return_value=None):
            with pytest.raises(SkipThisTool):
                Bash(session=mock_session)


# ============================================================================
# Edge cases
# ============================================================================

@pytest.mark.skipif(
    not BASH_AVAILABLE,
    reason="Bash tool is not available on this platform",
)
class TestEdgeCases:
    async def test_command_with_special_chars(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo 'hello\tworld'")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        # Tab may be preserved or converted by echo depending on bash version
        assert "hello" in result.output
        assert "world" in result.output

    async def test_command_with_quotes(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd='echo "quoted text"')
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "quoted text" in result.output


# ============================================================================
# Inactivity timeout behavior
# ============================================================================

@pytest.mark.skipif(
    not BASH_AVAILABLE,
    reason="Bash tool is not available on this platform",
)
class TestBashInactivityTimeout:
    async def test_bash_inactivity_timeout_keeps_background_task(
        self, mock_session: MagicMock
    ) -> None:
        with patch(
            "kimi_cli.tools.background.utils.DEFAULT_INACTIVITY_TIMEOUT", 2.0
        ):
            bash = Bash(session=mock_session)
            params = BashParams(cmd="sleep 120", timeout=90)
            try:
                result = await bash(params)
                assert isinstance(result, ToolError)
                assert result.brief == "Timeout"
                # A command that produced no output is handed off to the
                # background (not killed) so the model can wait for it.
                assert "No output after" in result.message
                assert "task_id" in result.message
                assert "job_output" in result.message
                assert get_all_tasks(mock_session) != {}
            finally:
                for stream in list(get_all_tasks(mock_session).values()):
                    await stream.stop()

    async def test_bash_short_timeout_unchanged(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="sleep 5", timeout=3)
        start = asyncio.get_event_loop().time()
        result = await bash(params)
        elapsed = asyncio.get_event_loop().time() - start
        assert isinstance(result, ToolError)
        assert result.brief == "Timeout"
        assert 2.5 <= elapsed <= 4.0


@pytest.mark.skipif(
    not BASH_AVAILABLE,
    reason="Bash tool is not available on this platform",
)
class TestBashFinishedJobHistory:
    """End-to-end: a finished background bash job stays retrievable via job_output.

    Regression coverage for the reported bug where the first ``job_output``
    read of a completed task dropped it from the active registry, and a
    second read answered ``Task '<id>' not found``.
    """

    @staticmethod
    def _task_id_of(result: ToolOk) -> str:
        import re

        match = re.search(r"task_id: `([^`]+)`", result.output)
        assert match is not None, f"no task_id in: {result.output}"
        return match.group(1)

    async def test_failed_background_job_readable_after_first_completed_get(
        self, mock_session: MagicMock
    ) -> None:
        bash = Bash(session=mock_session)
        started = await bash(
            BashParams(cmd="echo line1; echo 'error: something broke'; exit 1", mode="send")
        )
        assert isinstance(started, ToolOk)
        task_id = self._task_id_of(started)

        to = TaskOutput(session=mock_session)
        # Wait until the command has finished.
        done = await to(TaskOutputParams(job_id=task_id, wait=True, timeout=30))
        assert done.is_error  # exit code 1 → failed
        assert "not found" not in done.message

        # First completed get: reports the failure (and records history).
        first = await to(TaskOutputParams(job_id=task_id, action="get"))
        assert first.is_error
        assert "not found" not in first.message

        # Second get — previously "not found", now served from history.
        second = await to(
            TaskOutputParams(job_id=task_id, wait_for_pattern="error: something")
        )
        assert second.is_error  # the underlying command still failed
        assert "not found" not in second.message
        assert "not found" not in second.output
        assert "error: something broke" in second.output
        assert "wait_matched: true" in second.output
        assert "finished-task history" in second.output

    async def test_successful_background_job_readable_after_first_completed_get(
        self, mock_session: MagicMock
    ) -> None:
        bash = Bash(session=mock_session)
        started = await bash(BashParams(cmd="echo hello_world", mode="send"))
        assert isinstance(started, ToolOk)
        task_id = self._task_id_of(started)

        to = TaskOutput(session=mock_session)
        done = await to(TaskOutputParams(job_id=task_id, wait=True, timeout=30))
        assert not done.is_error

        # The wait above already completed and deregistered the task; the next
        # read must come from the finished-task history.
        again = await to(TaskOutputParams(job_id=task_id, action="get"))
        assert not again.is_error
        assert "not found" not in again.message
        assert "hello_world" in again.output
        assert "finished-task history" in again.output


@pytest.mark.skipif(
    not PWSH_AVAILABLE,
    reason="PowerShell tool is not available on this platform",
)
class TestPowershellInactivityTimeout:
    @pytest.fixture(autouse=True)
    def _force_pwsh_enabled(self) -> Any:
        """Force-enable the Powershell tool for these integration tests.

        With the default Git-Bash-first policy on Windows, the gate
        ``_should_enable_powershell()`` is False whenever Git Bash is
        installed; these tests execute real pwsh commands, so the gate is
        bypassed.
        """
        with patch(
            "kimi_cli.tools.file.bash.pwsh_tool._bash_tool._should_enable_powershell",
            return_value=True,
        ):
            yield

    async def test_pwsh_inactivity_timeout_keeps_background_task(
        self, mock_session: MagicMock
    ) -> None:
        with patch(
            "kimi_cli.tools.background.utils.DEFAULT_INACTIVITY_TIMEOUT", 2.0
        ):
            pwsh = Powershell(session=mock_session)
            params = PowershellParams(cmd="Start-Sleep -Seconds 120", timeout=90)
            try:
                result = await pwsh(params)
                assert isinstance(result, ToolError)
                assert result.brief == "Timeout"
                # A command that produced no output is handed off to the
                # background (not killed) so the model can wait for it.
                assert "No output after" in result.message
                assert "task_id" in result.message
                assert "job_output" in result.message
                assert get_all_tasks(mock_session) != {}
            finally:
                for stream in list(get_all_tasks(mock_session).values()):
                    await stream.stop()

    async def test_pwsh_short_timeout_unchanged(self, mock_session: MagicMock) -> None:
        pwsh = Powershell(session=mock_session)
        params = PowershellParams(cmd="Start-Sleep -Seconds 5", timeout=3)
        start = asyncio.get_event_loop().time()
        result = await pwsh(params)
        elapsed = asyncio.get_event_loop().time() - start
        assert isinstance(result, ToolError)
        assert result.brief == "Timeout"
        assert 2.5 <= elapsed <= 4.0


class TestPowershellBackgroundSendWaitForPatternMocked:
    """mode='send' + wait_for_pattern must block until the pattern appears."""

    @pytest.fixture
    def pwsh_instance(self, mock_session: MagicMock) -> Powershell:
        with patch(
            "kimi_cli.tools.file.bash.pwsh_tool._bash_tool._should_enable_powershell",
            return_value=True,
        ), patch(
            "kimi_cli.tools.file.bash.pwsh_tool.find_pwsh", return_value=r"C:\pwsh\pwsh.exe"
        ):
            return Powershell(session=mock_session)

    @staticmethod
    def _mock_process_task(
        wait_result: tuple[str, bool, float] = ("ready output", True, 1.5),
        alive: bool = True,
    ) -> MagicMock:
        process_task = MagicMock()
        process_task.start = AsyncMock(return_value="pwsh-send-id")
        process_task.thread_is_alive = AsyncMock(return_value=alive)
        process_task.stream = MagicMock()
        process_task.stream.wait_for_output = AsyncMock(return_value=wait_result)
        process_task.stream.success = AsyncMock(return_value=True)
        process_task.stream.exit_code = 0
        process_task.stream.process_elapsed = None
        return process_task

    async def test_send_with_wait_for_pattern_blocks_and_returns_output(
        self, pwsh_instance: Powershell
    ) -> None:
        with patch("kimi_cli.tools.file.bash.pwsh_tool.ProcessTask") as mock_pt:
            mock_pt.return_value = self._mock_process_task()
            result = await pwsh_instance(
                PowershellParams(
                    cmd="Start-Sleep -Seconds 2; Write-Output ready_now",
                    mode="send",
                    wait_for_pattern="ready_now",
                )
            )

        assert isinstance(result, ToolOk)
        assert "pwsh-send-id" in result.output
        assert "ready output" in result.output
        assert "wait_matched: true" in result.output
        mock_pt.return_value.stream.wait_for_output.assert_awaited_once()

    async def test_send_without_wait_for_pattern_still_returns_immediately(
        self, pwsh_instance: Powershell
    ) -> None:
        with patch("kimi_cli.tools.file.bash.pwsh_tool.ProcessTask") as mock_pt:
            mock_pt.return_value = self._mock_process_task()
            result = await pwsh_instance(
                PowershellParams(cmd="Write-Output hi", mode="send")
            )

        assert isinstance(result, ToolOk)
        assert "Running in background. task_id: `pwsh-send-id`" in result.output
        mock_pt.return_value.stream.wait_for_output.assert_not_called()


@pytest.mark.skipif(
    not PWSH_AVAILABLE,
    reason="PowerShell tool is not available on this platform",
)
class TestPowershellBackgroundSendWaitForPatternIntegration:
    @pytest.fixture(autouse=True)
    def _force_pwsh_enabled(self) -> Any:
        with patch(
            "kimi_cli.tools.file.bash.pwsh_tool._bash_tool._should_enable_powershell",
            return_value=True,
        ):
            yield

    async def test_pwsh_send_wait_for_pattern_blocks_until_pattern(
        self, mock_session: MagicMock
    ) -> None:
        pwsh = Powershell(session=mock_session)
        start = time.monotonic()
        result = await pwsh(
            PowershellParams(
                cmd="Start-Sleep -Seconds 2; Write-Output ready_now",
                mode="send",
                wait_for_pattern="ready_now",
                timeout=30,
            )
        )
        elapsed = time.monotonic() - start

        assert isinstance(result, ToolOk)
        assert "ready_now" in result.output
        assert "wait_matched: true" in result.output
        assert elapsed >= 1.5, f"returned too early after {elapsed:.2f}s"


# ============================================================================
# Complex bash commands — pipes, redirects, substitution, etc.
# ============================================================================

@pytest.mark.skipif(
    not BASH_AVAILABLE,
    reason="Bash tool is not available on this platform",
)
class TestComplexCommands:
    @pytest.fixture(autouse=True)
    def _raw_command_output(self) -> Any:
        """These tests assert on raw command output; disable rtk rewriting
        (which wraps output in a metadata envelope) regardless of whether
        an rtk binary is installed on the host."""
        with patch("kimi_cli.tools.common._rtk_available", return_value=False):
            yield

    @staticmethod
    def _cmd_output(result: ToolOk | ToolError) -> str:
        """Extract the raw process output from the session output block.

        The Bash tool wraps process output in a metadata envelope
        (``task_id:``/``status:``/``output: |`` ...); these tests assert on
        the raw command output inside it.
        """
        marker = "output: |\n"
        text = result.output
        if marker not in text:
            return text
        inner = text.split(marker, 1)[1]
        lines: list[str] = []
        for line in inner.splitlines():
            if line.startswith("  "):
                lines.append(line[2:])
            else:
                break
        return "\n".join(lines)

    """Tests for complex bash commands: pipes, redirects, substitution, conditionals, etc."""

    # -- pipes ---------------------------------------------------------------

    async def test_pipe_echo_to_wc(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo hello | wc -l")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "1" in result.output

    async def test_pipe_echo_to_grep(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo -e 'apple\\nbanana\\ncherry' | grep ana")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "banana" in result.output

    async def test_pipe_ls_to_head(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="ls / | head -1")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert len(result.output) > 0

    async def test_multiple_pipes(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo hello | tr 'a-z' 'A-Z' | tr 'A-Z' 'a-z'")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "hello" in result.output

    # -- redirects -----------------------------------------------------------

    async def test_redirect_stdout_to_file(
        self, mock_session: MagicMock, tmp_path: Path
    ) -> None:
        bash = Bash(session=mock_session)
        outfile = tmp_path / "redirected.txt"
        posix = str(outfile).replace("\\", "/")
        params = BashParams(cmd=f"echo redirected_content > {posix}")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert outfile.read_text(encoding="utf-8").strip() == "redirected_content"

    async def test_redirect_append(
        self, mock_session: MagicMock, tmp_path: Path
    ) -> None:
        bash = Bash(session=mock_session)
        outfile = tmp_path / "append.txt"
        posix = str(outfile).replace("\\", "/")
        await bash(BashParams(cmd=f"echo line1 > {posix}"))
        await bash(BashParams(cmd=f"echo line2 >> {posix}"))
        result = await bash(BashParams(cmd=f"cat {posix}"))
        assert isinstance(result, ToolOk)
        lines = self._cmd_output(result).strip().splitlines()
        assert "line1" in lines[0]
        assert "line2" in lines[-1]

    async def test_stderr_redirect(
        self, mock_session: MagicMock, tmp_path: Path
    ) -> None:
        bash = Bash(session=mock_session)
        outfile = tmp_path / "stderr.txt"
        posix = str(outfile).replace("\\", "/")
        # Redirect stderr to file; command fails so we expect ToolError
        params = BashParams(cmd=f"ls nonexisistent 2> {posix}")
        await bash(params)
        content = outfile.read_text(encoding="utf-8").lower()
        assert "nonexisistent" in content or "cannot access" in content or "no such" in content

    # -- command substitution ------------------------------------------------

    async def test_command_substitution(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo $(echo nested)")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "nested" in result.output

    async def test_backtick_substitution(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo `echo backtick`")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "backtick" in result.output

    # -- environment variables -----------------------------------------------

    async def test_env_var_home(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo $HOME")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert len(result.output.strip()) > 0

    async def test_env_var_user(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo $USER")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        # USER may be empty on some systems; just check no error

    # -- semicolon-separated commands ----------------------------------------

    async def test_semicolon_chain(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo first; echo second")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "first" in result.output
        assert "second" in result.output

    async def test_and_or_operators(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="true && echo yes || echo no")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "yes" in result.output

    async def test_and_or_false_branch(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="false && echo yes || echo no")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "no" in result.output

    # -- conditionals --------------------------------------------------------

    async def test_if_statement(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="if true; then echo TRUE; else echo FALSE; fi")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "TRUE" in result.output

    async def test_test_bracket(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="[ 1 -eq 1 ] && echo equal")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "equal" in result.output

    # -- exit codes ----------------------------------------------------------

    async def test_exit_code_success_check(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="true; echo $?")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "0" in result.output

    async def test_exit_code_failure_check(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        # The `echo $?` succeeds (exit 0) so overall ToolOk
        params = BashParams(cmd="false; echo $?")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "1" in result.output

    # -- here-strings / here-docs --------------------------------------------

    async def test_here_string(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="cat <<< 'herestring'")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "herestring" in result.output

    async def test_here_doc(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="cat <<EOF\nheredoc_line\nEOF")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "heredoc_line" in result.output

    # -- globbing ------------------------------------------------------------

    async def test_glob_expansion(self, mock_session: MagicMock, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")
        bash = Bash(session=mock_session)
        posix = str(tmp_path).replace("\\", "/")
        params = BashParams(cmd=f"cd {posix} && ls *.txt")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "a.txt" in result.output
        assert "b.txt" in result.output

    # -- arithmetic expansion ------------------------------------------------

    async def test_arithmetic_expansion(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo $((3 + 4))")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "7" in result.output

    # -- brace expansion -----------------------------------------------------

    async def test_brace_expansion(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo {a,b,c}")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "a b c" in result.output

    # -- sub-shell -----------------------------------------------------------

    async def test_subshell(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="(cd / && pwd)")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert self._cmd_output(result).strip() == "/"

    # -- process substitution -------------------------------------------------

    async def test_process_substitution_diff(
        self, mock_session: MagicMock, tmp_path: Path
    ) -> None:
        f1 = tmp_path / "f1.txt"
        f2 = tmp_path / "f2.txt"
        f1.write_text("same")
        f2.write_text("same")
        bash = Bash(session=mock_session)
        posix1 = str(f1).replace("\\", "/")
        posix2 = str(f2).replace("\\", "/")
        params = BashParams(cmd=f"diff <(cat {posix1}) <(cat {posix2})")
        result = await bash(params)
        # diff returns 0 (success) when files are identical
        assert isinstance(result, ToolOk)

    async def test_process_substitution_diff_differs(
        self, mock_session: MagicMock, tmp_path: Path
    ) -> None:
        f1 = tmp_path / "f1.txt"
        f2 = tmp_path / "f2.txt"
        f1.write_text("one")
        f2.write_text("two")
        bash = Bash(session=mock_session)
        posix1 = str(f1).replace("\\", "/")
        posix2 = str(f2).replace("\\", "/")
        params = BashParams(cmd=f"diff <(cat {posix1}) <(cat {posix2})")
        result = await bash(params)
        # diff returns 1 when files differ — an expected, informative outcome
        # (not a failure): reported as success with the meaning noted.
        assert isinstance(result, ToolOk)
        assert "Files differ" in result.message

    # -- inline env ----------------------------------------------------------

    async def test_inline_env_override(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="MYVAR=42 bash -c 'echo $MYVAR'")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "42" in result.output

    # -- negation ------------------------------------------------------------

    async def test_negation_bang(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="! false; echo $?")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "0" in result.output

    # -- loop ----------------------------------------------------------------

    async def test_for_loop(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="for i in 1 2 3; do echo $i; done")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "1" in result.output
        assert "2" in result.output
        assert "3" in result.output

    async def test_while_loop(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="i=0; while [ $i -lt 3 ]; do echo $i; i=$((i+1)); done")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "0" in result.output
        assert "1" in result.output
        assert "2" in result.output

    # -- temp file with mktemp -----------------------------------------------

    async def test_mktemp(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="mktemp")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "/tmp" in result.output or "/temp" in result.output.lower()

    # -- printf --------------------------------------------------------------

    async def test_printf(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="printf '%s %s' hello world")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "hello world" in result.output

    # -- array ---------------------------------------------------------------

    async def test_array(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="arr=(one two three); echo ${arr[1]}")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "two" in result.output

    # -- string manipulation -------------------------------------------------

    async def test_string_length(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="s=abcdef; echo ${#s}")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "6" in result.output

    async def test_string_substring(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="s=hello; echo ${s:1:3}")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "ell" in result.output

    # -- sed -----------------------------------------------------------------

    async def test_sed_substitution(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo foo | sed 's/foo/bar/'")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "bar" in result.output

    # -- awk -----------------------------------------------------------------

    async def test_awk_field(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo 'a b c' | awk '{print $2}'")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "b" in result.output

    # -- cut -----------------------------------------------------------------

    async def test_cut_delimiter(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo 'a:b:c' | cut -d: -f2")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "b" in result.output

    # -- sort / uniq ---------------------------------------------------------

    async def test_sort_uniq(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo -e 'c\\na\\nb\\na' | sort | uniq")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        lines = self._cmd_output(result).strip().splitlines()
        assert lines == ["a", "b", "c"]

    # -- head / tail ---------------------------------------------------------

    async def test_head_n(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="seq 10 | head -3")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        lines = self._cmd_output(result).strip().splitlines()
        assert len(lines) == 3

    async def test_tail_n(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="seq 10 | tail -3")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        lines = self._cmd_output(result).strip().splitlines()
        assert "8" in lines[0]
        assert "10" in lines[-1]

    # -- tee -----------------------------------------------------------------

    async def test_tee(
        self, mock_session: MagicMock, tmp_path: Path
    ) -> None:
        bash = Bash(session=mock_session)
        outfile = tmp_path / "tee_out.txt"
        posix = str(outfile).replace("\\", "/")
        params = BashParams(cmd=f"echo hello_tee | tee {posix}")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "hello_tee" in result.output
        assert outfile.read_text(encoding="utf-8").strip() == "hello_tee"

    # -- exit with explicit code ---------------------------------------------

    async def test_exit_explicit_code(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="exit 42")
        result = await bash(params)
        # bash -c "exit 42" exits with code 42 -> ToolError
        assert isinstance(result, ToolError)

    # -- chained pipes with special chars ------------------------------------

    async def test_pipe_with_dollar_signs(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo '$HOME' | cat")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        # Single quotes preserve literal $HOME
        assert "$HOME" in result.output

    # -- background process via & --------------------------------------------

    async def test_background_ampersand(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="sleep 1 & wait", timeout=10)
        result = await bash(params)
        assert isinstance(result, ToolOk)

    # -- dirname / basename --------------------------------------------------

    async def test_dirname_basename(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="dirname /usr/bin/bash && basename /usr/bin/bash")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "/usr/bin" in result.output
        assert "bash" in result.output

    # -- xargs ---------------------------------------------------------------

    async def test_xargs(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo 'a b c' | xargs -n1 echo")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "a" in result.output
        assert "b" in result.output
        assert "c" in result.output

    # -- trap ----------------------------------------------------------------

    async def test_trap_does_not_crash(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="trap 'echo trapped' EXIT; echo done")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "done" in result.output
        assert "trapped" in result.output

    # -- backslash escapes before metacharacters -----------------------------

    async def test_find_with_escaped_parens(self, mock_session: MagicMock, tmp_path: Path) -> None:
        bash = Bash(session=mock_session)
        # Create files to search
        (tmp_path / "foo.txt").write_text("foo")
        (tmp_path / "bar.py").write_text("bar")
        (tmp_path / "baz.txt").write_text("baz")
        posix = str(tmp_path).replace("\\", "/")
        # The \(\) grouping must survive _prepare_bash_cmd on Windows
        params = BashParams(cmd=f"find {posix} -maxdepth 1 \\( -name '*.txt' -o -name '*.py' \\) | sort")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "foo.txt" in result.output
        assert "bar.py" in result.output
        assert "baz.txt" in result.output

    async def test_echo_escaped_pipe(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo 'a|b' | cat")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "a|b" in result.output

    async def test_echo_escaped_glob(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo '*'")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "*" in result.output

    async def test_echo_escaped_semicolon(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo 'a;b'")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "a;b" in result.output


# ============================================================================
# BashParams interactive validation
# ============================================================================


# ============================================================================
# Bash fixer integration across execution modes
# ============================================================================


class TestBashFixToolIntegration:
    @pytest.fixture
    def bash_instance(self, mock_session: MagicMock) -> Bash:
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.find_bash",
            return_value=r"C:\Git\bin\bash.exe",
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool._should_enable_bash",
            return_value=True,
        ):
            return Bash(session=mock_session)

    @staticmethod
    def _completed_process_task() -> MagicMock:
        process_task = MagicMock()
        process_task.start = AsyncMock(return_value="bash-fix-id")
        process_task.wait_with_monitor = AsyncMock(return_value=(False, 0.0, False))
        process_task.thread_is_alive = AsyncMock(return_value=False)
        process_task.stream = MagicMock()
        process_task.stream.pop_output = AsyncMock(return_value="fixed output")
        process_task.stream.success = AsyncMock(return_value=True)
        process_task.stream.exit_code = 0
        process_task.stream.process_elapsed = None
        return process_task

    async def test_foreground_command_is_fixed_before_process_creation(
        self, bash_instance: Bash
    ) -> None:
        process_task = self._completed_process_task()
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"), patch(
            "kimix.utils.windows_env.refresh_env_from_registry"
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask",
            return_value=process_task,
        ) as process_task_class:
            result = await bash_instance(BashParams(cmd="gtimeout 2 echo ok"))

        assert isinstance(result, ToolOk)
        command = process_task_class.call_args.args[1][1]
        assert command.endswith("\ngtimeout 2 echo ok")
        assert "gtimeout()" in command
        assert 'timeout "$@"' in command

    async def test_background_command_is_fixed_before_process_creation(
        self, bash_instance: Bash
    ) -> None:
        process_task = MagicMock()
        process_task.start = AsyncMock(return_value="bash-background-id")
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"), patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask",
            return_value=process_task,
        ) as process_task_class:
            result = await bash_instance(
                BashParams(cmd="printf text | pbcopy", mode="send")
            )

        assert isinstance(result, ToolOk)
        command = process_task_class.call_args.args[1][1]
        assert command.endswith("\nprintf text | pbcopy")
        assert "pbcopy()" in command
        assert 'clip.exe "$@"' in command

    async def test_interactive_initial_command_is_fixed(
        self, bash_instance: Bash
    ) -> None:
        process_task = MagicMock()
        process_task.start = AsyncMock(return_value="bash-interactive-id")
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"), patch(
            "kimix.utils.windows_env.refresh_env_from_registry"
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask",
            return_value=process_task,
        ) as process_task_class:
            result = await bash_instance(BashParams(cmd="printf abc | rev", mode="interactive"))

        assert isinstance(result, ToolOk)
        bash_args = process_task_class.call_args.args[1]
        assert bash_args[1].endswith("; exec bash -i")
        command = _decode_startup_payload(process_task_class.call_args)
        assert command.endswith("\nprintf abc | rev")
        for name in ("gtimeout", "rev", "xdg-open", "open", "pbcopy", "pbpaste"):
            assert f"{name}()" in command
            assert f"declare -F {name}" in command

    async def test_existing_interactive_session_input_is_fixed(
        self, bash_instance: Bash
    ) -> None:
        data = TaskData()
        stream = AsyncMock()
        stream.is_started = AsyncMock(return_value=True)
        stream.pop_output = AsyncMock(return_value="")
        stream.input = AsyncMock(return_value=True)
        stream.wait_for_output = AsyncMock(return_value=("", False, 0.01))
        stream.thread_is_alive = AsyncMock(return_value=True)
        stream.success = AsyncMock(return_value=True)
        data.tasks = {"bash_compat": stream}
        bash_instance._session.custom_data["background_task_data"] = data

        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            result = await bash_instance(
                BashParams(cmd="xdg-open README.md", task_id="bash_compat")
            )

        assert isinstance(result, ToolOk)
        sent = stream.input.await_args.args[0]
        assert sent == "xdg-open README.md\n"
        assert "xdg-open()" not in sent

    @pytest.mark.parametrize(
        "fragment",
        [
            "rev",
            "EOF",
            "then",
            "'continued text",
            '"continued text',
            "printf '%s' \"$?\"",
            "body\\",
        ],
    )
    async def test_existing_interactive_session_input_is_not_reparsed_as_program(
        self, bash_instance: Bash, fragment: str
    ) -> None:
        data = TaskData()
        stream = AsyncMock()
        stream.is_started = AsyncMock(return_value=True)
        stream.pop_output = AsyncMock(return_value="")
        stream.input = AsyncMock(return_value=True)
        stream.wait_for_output = AsyncMock(return_value=("", False, 0.01))
        stream.thread_is_alive = AsyncMock(return_value=True)
        stream.success = AsyncMock(return_value=True)
        data.tasks = {"bash_fragment": stream}
        bash_instance._session.custom_data["background_task_data"] = data

        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"):
            result = await bash_instance(
                BashParams(cmd=fragment, task_id="bash_fragment")
            )

        assert isinstance(result, ToolOk)
        stream.input.assert_awaited_once_with(fragment + "\n")

    async def test_compatibility_fix_runs_before_rtk(
        self, bash_instance: Bash
    ) -> None:
        process_task = self._completed_process_task()
        rewrite = MagicMock(side_effect=lambda command, *_args, **_kwargs: (command, False))
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"), patch(
            "kimix.utils.windows_env.refresh_env_from_registry"
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask",
            return_value=process_task,
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool._maybe_rewrite_shell_command_with_rtk",
            rewrite,
        ):
            await bash_instance(BashParams(cmd="gtimeout 2 true"))

        rewritten = rewrite.call_args.args[0]
        assert rewritten.endswith("\ngtimeout 2 true")
        assert "gtimeout()" in rewritten

    async def test_path_normalization_and_compatibility_fix_compose(
        self, bash_instance: Bash
    ) -> None:
        process_task = self._completed_process_task()
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"), patch(
            "kimix.utils.windows_env.refresh_env_from_registry"
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask",
            return_value=process_task,
        ) as process_task_class:
            await bash_instance(
                BashParams(cmd=r"gtimeout 2 cat src\kimix\agent_worker.json")
            )

        command = process_task_class.call_args.args[1][1]
        assert command.endswith(
            "\ngtimeout 2 cat src/kimix/agent_worker.json"
        )

    async def test_forbidden_check_uses_original_command_before_rewrite(
        self, mock_session: MagicMock
    ) -> None:
        mock_session.custom_config.get.return_value = {
            "forbidden_commands": ["gtimeout"]
        }
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.find_bash",
            return_value=r"C:\Git\bin\bash.exe",
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool._should_enable_bash",
            return_value=True,
        ):
            bash = Bash(session=mock_session)

        with patch("kimi_cli.tools.file.bash.bash_tool.fix_bash_command") as fixer:
            result = await bash(BashParams(cmd="gtimeout 2 true"))

        assert isinstance(result, ToolError)
        assert "forbidden" in result.message
        fixer.assert_not_called()

    @pytest.mark.parametrize("mode", ["execute", "send"])
    async def test_forbidden_source_command_is_blocked_in_fresh_modes(
        self, mock_session: MagicMock, mode: str
    ) -> None:
        mock_session.custom_config.get.return_value = {
            "forbidden_commands": ["gtimeout"]
        }
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.find_bash",
            return_value=r"C:\Git\bin\bash.exe",
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool._should_enable_bash",
            return_value=True,
        ):
            bash = Bash(session=mock_session)
        with patch("kimi_cli.tools.file.bash.bash_tool.ProcessTask") as process_task:
            result = await bash(BashParams(cmd="gtimeout 2 true", mode=mode))
        assert isinstance(result, ToolError)
        process_task.assert_not_called()

    async def test_forbidden_source_command_is_blocked_in_continuation(
        self, bash_instance: Bash
    ) -> None:
        bash_instance._forbidden_keywords = ["gtimeout"]
        with patch("kimi_cli.tools.file.bash.bash_tool.fix_bash_command") as fixer:
            result = await bash_instance(
                BashParams(cmd="gtimeout 2 true", task_id="existing")
            )
        assert isinstance(result, ToolError)
        fixer.assert_not_called()

    @pytest.mark.parametrize("fragment", ["printf \\", "x=(printf"])
    async def test_forbidden_policy_rejects_incomplete_continuation_fragment(
        self, bash_instance: Bash, fragment: str
    ) -> None:
        data = TaskData()
        stream = AsyncMock()
        stream.is_started = AsyncMock(return_value=True)
        stream.input = AsyncMock(return_value=True)
        data.tasks = {"existing": stream}
        bash_instance._session.custom_data["background_task_data"] = data
        bash_instance._forbidden_keywords = ["printf BLOCKED"]

        syntax_error = subprocess.CompletedProcess(
            args=[],
            returncode=2,
            stdout="",
            stderr="bash: syntax error: unexpected end of file",
        )
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.subprocess.run",
            return_value=syntax_error,
        ):
            result = await bash_instance(BashParams(cmd=fragment, task_id="existing"))

        assert isinstance(result, ToolError)
        assert result.brief == "Unsafe command fragment"
        stream.input.assert_not_awaited()

    @pytest.mark.parametrize(
        "command",
        [
            "if true; then printf safe; fi",
            "for x in one; do printf safe; done",
            "cat <<EOF\nsafe\nEOF",
        ],
    )
    async def test_forbidden_policy_allows_complete_compound_continuation(
        self, bash_instance: Bash, command: str
    ) -> None:
        data = TaskData()
        stream = AsyncMock()
        stream.is_started = AsyncMock(return_value=True)
        stream.pop_output = AsyncMock(return_value="")
        stream.input = AsyncMock(return_value=True)
        stream.wait_for_output = AsyncMock(return_value=("safe", False, 0.01))
        stream.thread_is_alive = AsyncMock(return_value=True)
        stream.success = AsyncMock(return_value=True)
        data.tasks = {"existing": stream}
        bash_instance._session.custom_data["background_task_data"] = data
        bash_instance._forbidden_keywords = ["printf BLOCKED"]

        syntax_ok = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.subprocess.run",
            return_value=syntax_ok,
        ):
            result = await bash_instance(BashParams(cmd=command, task_id="existing"))

        assert isinstance(result, ToolOk)
        stream.input.assert_awaited_once_with(command + "\n")

    @pytest.mark.parametrize(
        ("source", "forbidden"),
        [("gtimeout 2 true", "timeout"), ("pbpaste", "powershell.exe")],
    )
    async def test_forbidden_generated_command_is_blocked(
        self, bash_instance: Bash, source: str, forbidden: str
    ) -> None:
        bash_instance._forbidden_keywords = [forbidden]
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"), patch(
            "kimix.utils.windows_env.refresh_env_from_registry"
        ), patch("kimi_cli.tools.file.bash.bash_tool.ProcessTask") as process_task:
            result = await bash_instance(BashParams(cmd=source))
        assert isinstance(result, ToolError)
        process_task.assert_not_called()

    async def test_forbidden_rtk_generated_command_is_blocked(
        self, bash_instance: Bash
    ) -> None:
        bash_instance._forbidden_keywords = ["rtk"]
        process_task = self._completed_process_task()
        with patch(
            "kimi_cli.tools.file.bash.bash_tool._maybe_rewrite_shell_command_with_rtk",
            return_value=("rtk git status", True),
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask",
            return_value=process_task,
        ) as process_task_class:
            result = await bash_instance(BashParams(cmd="git status"))
        assert isinstance(result, ToolError)
        process_task_class.assert_not_called()

    @pytest.mark.parametrize(
        ("generated_keyword", "command"),
        [("perl", "rev"), ("rtk", "git status")],
    )
    async def test_continuation_is_not_rewritten_or_blocked_by_generated_text(
        self, bash_instance: Bash, generated_keyword: str, command: str
    ) -> None:
        data = TaskData()
        stream = AsyncMock()
        stream.is_started = AsyncMock(return_value=True)
        stream.pop_output = AsyncMock(return_value="buffered output")
        stream.input = AsyncMock(return_value=True)
        stream.wait_for_output = AsyncMock(return_value=("new output", False, 0.01))
        stream.thread_is_alive = AsyncMock(return_value=True)
        stream.success = AsyncMock(return_value=True)
        data.tasks = {"existing": stream}
        bash_instance._session.custom_data["background_task_data"] = data
        bash_instance._forbidden_keywords = [generated_keyword]

        syntax_ok = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch(
            "kimi_cli.tools.file.bash.bash_tool._maybe_rewrite_shell_command_with_rtk"
        ) as rewrite, patch(
            "kimi_cli.tools.file.bash.bash_tool.subprocess.run",
            return_value=syntax_ok,
        ):
            result = await bash_instance(
                BashParams(cmd=command, task_id="existing")
            )

        assert isinstance(result, ToolOk)
        rewrite.assert_not_called()
        stream.pop_output.assert_awaited_once_with()
        stream.input.assert_awaited_once_with(command + "\n")

    async def test_failed_continuation_delivery_returns_buffered_output(
        self, bash_instance: Bash
    ) -> None:
        data = TaskData()
        stream = AsyncMock()
        stream.is_started = AsyncMock(return_value=True)
        stream.pop_output = AsyncMock(return_value="buffered output")
        stream.input = AsyncMock(return_value=False)
        data.tasks = {"existing": stream}
        bash_instance._session.custom_data["background_task_data"] = data

        result = await bash_instance(
            BashParams(cmd="printf new", task_id="existing")
        )

        assert isinstance(result, ToolError)
        assert result.output == "buffered output"
        stream.pop_output.assert_awaited_once_with()
        stream.input.assert_awaited_once()


# ============================================================================
# Bash interactive argument building
# ============================================================================

class TestBashInteractiveArgumentBuilding:
    @pytest.fixture
    def mock_session(self) -> MagicMock:
        session = MagicMock(spec=Session)
        session.custom_data = {}
        session.custom_config.get.return_value = {}
        return session

    async def test_non_interactive_args(self, mock_session: MagicMock) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.find_bash", return_value=r"C:\Git\bin\bash.exe"), patch(
            "kimi_cli.tools.file.bash.bash_tool._should_enable_bash", return_value=True
        ):
            bash = Bash(session=mock_session)

        with patch("kimi_cli.tools.file.bash.bash_tool.ProcessTask") as mock_pt:
            mock_instance = MagicMock()
            mock_instance.start = MagicMock(return_value=asyncio.Future())
            mock_instance.start.return_value.set_result("bash-test-id")
            mock_instance.wait = MagicMock(return_value=asyncio.Future())
            mock_instance.wait.return_value.set_result(None)
            mock_instance.wait_with_monitor = MagicMock(return_value=asyncio.Future())
            mock_instance.wait_with_monitor.return_value.set_result((False, 0.0, False))
            mock_instance.thread_is_alive = MagicMock(return_value=asyncio.Future())
            mock_instance.thread_is_alive.return_value.set_result(False)
            mock_instance.stream = MagicMock()
            mock_instance.stream.pop_output = MagicMock(return_value=asyncio.Future())
            mock_instance.stream.pop_output.return_value.set_result("mock output")
            mock_instance.stream.success = MagicMock(return_value=asyncio.Future())
            mock_instance.stream.success.return_value.set_result(True)
            mock_instance.stream.exit_code = 0
            mock_instance.stream.process_elapsed = None
            mock_pt.return_value = mock_instance

            params = BashParams(cmd="echo hello")
            result = await bash(params)

            assert isinstance(result, ToolOk)
            args = mock_pt.call_args
            # One-shot commands run with pipefail so pipelines surface the
            # real failing stage's exit code instead of the consumer's 0.
            assert args[0][1] == ["-c", "set -o pipefail; echo hello"]

    async def test_interactive_args_with_cmd(self, mock_session: MagicMock) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.find_bash", return_value=r"C:\Git\bin\bash.exe"), patch(
            "kimi_cli.tools.file.bash.bash_tool._should_enable_bash", return_value=True
        ):
            bash = Bash(session=mock_session)

        with patch("kimi_cli.tools.file.bash.bash_tool.ProcessTask") as mock_pt:
            mock_instance = MagicMock()
            mock_instance.start = MagicMock(return_value=asyncio.Future())
            mock_instance.start.return_value.set_result("bash-interactive-id")
            mock_pt.return_value = mock_instance

            params = BashParams(cmd="echo start", mode="interactive")
            result = await bash(params)

            assert isinstance(result, ToolOk)
            args = mock_pt.call_args
            bash_args = args[0][1]
            assert "-c" in bash_args
            assert bash_args[1].endswith("; exec bash -i")
            decoded = _decode_startup_payload(args)
            assert "echo start" in decoded
            assert args.kwargs.get("append_newline") is True or args[0][4] is True

    async def test_interactive_args_without_cmd(self, mock_session: MagicMock) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.find_bash", return_value=r"C:\Git\bin\bash.exe"), patch(
            "kimi_cli.tools.file.bash.bash_tool._should_enable_bash", return_value=True
        ):
            bash = Bash(session=mock_session)

        with patch("kimi_cli.tools.file.bash.bash_tool.ProcessTask") as mock_pt:
            mock_instance = MagicMock()
            mock_instance.start = MagicMock(return_value=asyncio.Future())
            mock_instance.start.return_value.set_result("bash-interactive-id")
            mock_pt.return_value = mock_instance

            params = BashParams(cmd="", mode="interactive")
            result = await bash(params)

            assert isinstance(result, ToolOk)
            args = mock_pt.call_args
            bash_args = args[0][1]
            assert bash_args[0] == "-c"
            assert bash_args[1].endswith("; exec bash -i")
            decoded = _decode_startup_payload(args)
            assert "gtimeout()" in decoded
            assert "rev()" in decoded
            assert "export -f rev" in decoded

    async def test_interactive_returns_immediately(self, mock_session: MagicMock) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.find_bash", return_value=r"C:\Git\bin\bash.exe"), patch(
            "kimi_cli.tools.file.bash.bash_tool._should_enable_bash", return_value=True
        ):
            bash = Bash(session=mock_session)

        with patch("kimi_cli.tools.file.bash.bash_tool.ProcessTask") as mock_pt:
            mock_instance = MagicMock()
            mock_instance.start = MagicMock(return_value=asyncio.Future())
            mock_instance.start.return_value.set_result("task-456")
            mock_pt.return_value = mock_instance

            params = BashParams(cmd="", mode="interactive")
            result = await bash(params)

            assert isinstance(result, ToolOk)
            assert "task-456" in result.message
            assert "task_id" in result.message
            assert "job_output" in result.message
            mock_instance.wait.assert_not_called()


# ============================================================================
# RTK rewrite path
# ============================================================================

class TestBashRtkRewrite:
    @pytest.fixture
    def mock_session(self) -> MagicMock:
        session = MagicMock(spec=Session)
        session.custom_data = {}
        session.custom_config.get.return_value = {}
        return session

    async def test_bash_rewrites_known_command_when_rtk_available(
        self, mock_session: MagicMock
    ) -> None:
        with patch(
            "kimi_cli.tools.common._rtk_available", return_value=True
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool.find_bash", return_value=r"C:\Git\bin\bash.exe"
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool._should_enable_bash", return_value=True
        ):
            bash = Bash(session=mock_session)

            with patch("kimi_cli.tools.file.bash.bash_tool.ProcessTask") as mock_pt:
                mock_instance = MagicMock()
                mock_instance.start = MagicMock(return_value=asyncio.Future())
                mock_instance.start.return_value.set_result("bash-rtk-id")
                mock_instance.wait = MagicMock(return_value=asyncio.Future())
                mock_instance.wait.return_value.set_result(None)
                mock_instance.wait_with_monitor = MagicMock(return_value=asyncio.Future())
                mock_instance.wait_with_monitor.return_value.set_result((False, 0.0, False))
                mock_instance.thread_is_alive = MagicMock(return_value=asyncio.Future())
                mock_instance.thread_is_alive.return_value.set_result(False)
                mock_instance.stream = MagicMock()
                mock_instance.stream.pop_output = MagicMock(return_value=asyncio.Future())
                mock_instance.stream.pop_output.return_value.set_result("mock output")
                mock_instance.stream.success = MagicMock(return_value=asyncio.Future())
                mock_instance.stream.success.return_value.set_result(True)
                mock_instance.stream.exit_code = 0
                mock_instance.stream.process_elapsed = None
                mock_pt.return_value = mock_instance

                params = BashParams(cmd="git status")
                result = await bash(params)

                assert isinstance(result, ToolOk)
                args = mock_pt.call_args
                # Rewritten to the bare `rtk` prefix (shared bin dir is first
                # on PATH) with the one-shot pipefail prelude.
                assert args[0][1] == ["-c", "set -o pipefail; rtk git status"]

    async def test_bash_read_builtin_not_rewritten(
        self, mock_session: MagicMock
    ) -> None:
        with patch("kimi_cli.tools.common._rtk_available", return_value=True), patch(
            "kimi_cli.tools.file.bash.bash_tool.find_bash", return_value=r"C:\Git\bin\bash.exe"
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool._should_enable_bash", return_value=True
        ):
            bash = Bash(session=mock_session)

        with patch("kimi_cli.tools.file.bash.bash_tool.ProcessTask") as mock_pt:
            mock_instance = MagicMock()
            mock_instance.start = MagicMock(return_value=asyncio.Future())
            mock_instance.start.return_value.set_result("bash-read-id")
            mock_instance.wait = MagicMock(return_value=asyncio.Future())
            mock_instance.wait.return_value.set_result(None)
            mock_instance.wait_with_monitor = MagicMock(return_value=asyncio.Future())
            mock_instance.wait_with_monitor.return_value.set_result((False, 0.0, False))
            mock_instance.thread_is_alive = MagicMock(return_value=asyncio.Future())
            mock_instance.thread_is_alive.return_value.set_result(False)
            mock_instance.stream = MagicMock()
            mock_instance.stream.pop_output = MagicMock(return_value=asyncio.Future())
            mock_instance.stream.pop_output.return_value.set_result("mock output")
            mock_instance.stream.success = MagicMock(return_value=asyncio.Future())
            mock_instance.stream.success.return_value.set_result(True)
            mock_instance.stream.exit_code = 0
            mock_instance.stream.process_elapsed = None
            mock_pt.return_value = mock_instance

            params = BashParams(cmd="read var")
            result = await bash(params)

            assert isinstance(result, ToolOk)
            args = mock_pt.call_args
            assert args[0][1] == ["-c", "set -o pipefail; read var"]


# ============================================================================
# Bash session continuation / wait_for_pattern
# ============================================================================

class TestBashSessionContinuation:
    @pytest.fixture
    def bash_instance(self, mock_session: MagicMock) -> Bash:
        with patch("kimi_cli.tools.file.bash.bash_tool.find_bash", return_value=r"C:\Git\bin\bash.exe"), patch(
            "kimi_cli.tools.file.bash.bash_tool._should_enable_bash", return_value=True
        ):
            return Bash(session=mock_session)

    async def test_continue_nonexistent_task_lists_available(self, bash_instance: Bash) -> None:
        from unittest.mock import AsyncMock

        data = TaskData()
        stream1 = AsyncMock()
        stream1.is_started = AsyncMock(return_value=True)
        stream2 = AsyncMock()
        stream2.is_started = AsyncMock(return_value=False)
        data.tasks = {"bash_alive": stream1, "bash_dead": stream2}
        bash_instance._session.custom_data["background_task_data"] = data

        result = await bash_instance(BashParams(cmd="echo hi", task_id="missing"))
        assert isinstance(result, ToolError)
        assert "missing" in result.message
        assert "bash_alive" in result.message
        assert "bash_dead" not in result.message

    async def test_continue_nonexistent_task_no_tasks(self, bash_instance: Bash) -> None:
        result = await bash_instance(BashParams(cmd="echo hi", task_id="missing"))
        assert isinstance(result, ToolError)
        assert "No running tasks" in result.message

    async def test_invalid_wait_for_pattern_returns_error(self, bash_instance: Bash) -> None:
        result = await bash_instance(BashParams(cmd="echo hi", wait_for_pattern="["))
        assert isinstance(result, ToolError)
        assert "Invalid wait_for_pattern" in result.message

    async def test_continue_session_sends_input_and_returns_block(self, bash_instance: Bash) -> None:
        from unittest.mock import AsyncMock

        data = TaskData()
        stream = AsyncMock()
        stream.is_started = AsyncMock(return_value=True)
        stream.pop_output = AsyncMock(return_value="")
        stream.input = AsyncMock(return_value=True)
        stream.wait_for_output = AsyncMock(return_value=("hello output", True, 0.12))
        stream.thread_is_alive = AsyncMock(return_value=True)
        stream.success = AsyncMock(return_value=True)
        data.tasks = {"bash_42": stream}
        bash_instance._session.custom_data["background_task_data"] = data

        result = await bash_instance(
            BashParams(cmd="echo hello", task_id="bash_42", wait_for_pattern="hello")
        )

        assert isinstance(result, ToolOk)
        assert "bash_42" in result.output
        assert "status: running" in result.output
        assert "wait_matched: true" in result.output
        assert "elapsed_seconds: 0.12" in result.output
        stream.pop_output.assert_awaited_once()
        stream.input.assert_awaited_once_with("echo hello\n")
        stream.wait_for_output.assert_awaited_once()


# ============================================================================
# Bash mode='send' + wait_for_pattern (background wait)
# ============================================================================

class TestBashBackgroundSendWaitForPattern:
    """mode='send' with wait_for_pattern must block until the pattern appears."""

    @pytest.fixture
    def bash_instance(self, mock_session: MagicMock) -> Bash:
        with patch("kimi_cli.tools.file.bash.bash_tool.find_bash", return_value=r"C:\Git\bin\bash.exe"), patch(
            "kimi_cli.tools.file.bash.bash_tool._should_enable_bash", return_value=True
        ):
            return Bash(session=mock_session)

    @staticmethod
    def _mock_process_task(
        wait_result: tuple[str, bool, float] = ("ready output", True, 1.5),
        alive: bool = True,
    ) -> MagicMock:
        process_task = MagicMock()
        process_task.start = AsyncMock(return_value="bash-send-id")
        process_task.thread_is_alive = AsyncMock(return_value=alive)
        process_task.stream = MagicMock()
        process_task.stream.wait_for_output = AsyncMock(return_value=wait_result)
        process_task.stream.success = AsyncMock(return_value=True)
        process_task.stream.exit_code = 0
        process_task.stream.process_elapsed = None
        return process_task

    async def test_send_with_wait_for_pattern_blocks_and_returns_output(
        self, bash_instance: Bash
    ) -> None:
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask"
        ) as mock_pt:
            mock_pt.return_value = self._mock_process_task()
            result = await bash_instance(
                BashParams(cmd="sleep 2; echo ready_now", mode="send", wait_for_pattern="ready_now")
            )

        assert isinstance(result, ToolOk)
        assert "bash-send-id" in result.output
        assert "ready output" in result.output
        assert "wait_matched: true" in result.output
        stream = mock_pt.return_value.stream
        stream.wait_for_output.assert_awaited_once()
        _, kwargs = stream.wait_for_output.call_args
        assert kwargs["pattern"] is not None
        assert kwargs["pattern"].search("ready_now")

    async def test_send_with_wait_for_pattern_task_completed(
        self, bash_instance: Bash
    ) -> None:
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask"
        ) as mock_pt:
            mock_pt.return_value = self._mock_process_task(
                wait_result=("all done", False, 2.0), alive=False
            )
            result = await bash_instance(
                BashParams(cmd="echo hi", mode="send", wait_for_pattern="never_appears", timeout=5)
            )

        assert isinstance(result, ToolOk)
        assert "all done" in result.output
        assert "status: completed" in result.output

    async def test_send_with_invalid_wait_for_pattern_does_not_start_task(
        self, bash_instance: Bash
    ) -> None:
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask"
        ) as mock_pt:
            result = await bash_instance(
                BashParams(cmd="echo hi", mode="send", wait_for_pattern="[")
            )

        assert isinstance(result, ToolError)
        assert "Invalid wait_for_pattern" in result.message
        mock_pt.assert_not_called()

    async def test_send_without_wait_for_pattern_still_returns_immediately(
        self, bash_instance: Bash
    ) -> None:
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask"
        ) as mock_pt:
            mock_pt.return_value = self._mock_process_task()
            result = await bash_instance(BashParams(cmd="echo hi", mode="send"))

        assert isinstance(result, ToolOk)
        assert "Running in background. task_id: `bash-send-id`" in result.output
        mock_pt.return_value.stream.wait_for_output.assert_not_called()


@pytest.mark.skipif(
    not BASH_AVAILABLE,
    reason="Bash tool is not available on this platform",
)
class TestBashBackgroundSendWaitForPatternIntegration:
    async def test_send_wait_for_pattern_blocks_until_pattern(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        start = time.monotonic()
        result = await bash(
            BashParams(
                cmd="sleep 2; echo ready_now",
                mode="send",
                wait_for_pattern="ready_now",
                timeout=30,
            )
        )
        elapsed = time.monotonic() - start

        assert isinstance(result, ToolOk)
        assert "ready_now" in result.output
        assert "wait_matched: true" in result.output
        # The call must have blocked until the pattern appeared (~2s), not
        # returned immediately.
        assert elapsed >= 1.5, f"returned too early after {elapsed:.2f}s"

    async def test_send_without_wait_for_pattern_returns_immediately(
        self, mock_session: MagicMock
    ) -> None:
        bash = Bash(session=mock_session)
        start = time.monotonic()
        result = await bash(BashParams(cmd="echo hi", mode="send", timeout=30))
        elapsed = time.monotonic() - start

        assert isinstance(result, ToolOk)
        assert "Running in background" in result.output
        assert elapsed < 5.0, f"send without pattern should return immediately ({elapsed:.2f}s)"


# ============================================================================
# Bash interactive integration tests
# ============================================================================

@pytest.mark.skipif(
    not BASH_AVAILABLE,
    reason="Bash tool is not available on this platform",
)
class TestBashInteractiveIntegration:
    async def test_interactive_echo(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="", mode="interactive")
        result = await bash(params)
        assert isinstance(result, ToolOk)
        task_id = result.message.split("`")[1]

        task_data = mock_session.custom_data.get("background_task_data")
        assert task_data is not None
        task = task_data.tasks.get(task_id)
        assert task is not None

        try:
            await task.input("echo hello")
            # Interactive Git Bash first sources a ~26 KB compatibility prelude
            # (``bash -c "...; exec bash -i"``) before it can execute input, and
            # under full-suite load that startup can take well over a fixed
            # 0.5 s sleep.  Poll for the echoed output instead, so the test only
            # depends on the echo actually appearing.
            deadline = time.monotonic() + 15.0
            output = ""
            while time.monotonic() < deadline and "hello" not in output:
                output = await task.get_output()
                await asyncio.sleep(0.1)
            assert "hello" in output, repr(output)
        finally:
            # Always tear the session down, even when an assertion fails, so a
            # dangling interactive bash cannot leak into (and slow down) the
            # remaining tests in the suite.
            await task.input("exit")
            await task.wait(timeout=5)
            if await task.thread_is_alive():
                await task.stop()

    async def test_interactive_start_with_wait_for_pattern(self, mock_session: MagicMock) -> None:
        bash = Bash(session=mock_session)
        params = BashParams(cmd="echo hello", mode="interactive", wait_for_pattern="hello", timeout=10)
        result = await bash(params)
        assert isinstance(result, ToolOk)
        assert "bash" in result.output
        assert "status:" in result.output
        assert "wait_matched: true" in result.output
        assert "hello" in result.output

        # Continue with exit to clean up.
        task_id = result.output.split("task_id: ", 1)[1].split("\n", 1)[0]
        try:
            exit_result = await bash(BashParams(cmd="exit", task_id=task_id, timeout=5))
            assert isinstance(exit_result, ToolOk)
            assert "status: completed" in exit_result.output
        finally:
            # If the continuation above fails, stop the session explicitly so
            # no interactive bash leaks into later tests.
            task_data = mock_session.custom_data.get("background_task_data")
            task = task_data.tasks.get(task_id) if task_data is not None else None
            if task is not None and await task.thread_is_alive():
                await task.stop()


# ============================================================================
# MSYSTEM neutralization (Git Bash -> xmake windows/MSVC default)
# ============================================================================


class TestIsGitBashInstall:
    def test_git_bash_inner_bash_detected(self) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.os.path.isfile", return_value=True):
            assert _is_git_bash_install(r"C:\Program Files\Git\usr\bin\bash.exe") is True

    def test_git_bash_wrapper_detected(self) -> None:
        """The ``bin/bash.exe`` launcher (the process the tool actually
        spawns) is also recognized as a Git Bash install."""
        with patch("kimi_cli.tools.file.bash.bash_tool.os.path.isfile", return_value=True):
            assert _is_git_bash_install(r"C:\Program Files\Git\bin\bash.exe") is True

    def test_msys2_bash_rejected(self) -> None:
        """MSYS2 also ships ``usr/bin/bash.exe`` but has no ``cmd/git.exe``
        marker, so its environment must stay untouched."""
        with patch("kimi_cli.tools.file.bash.bash_tool.os.path.isfile", return_value=False):
            assert _is_git_bash_install(r"C:\msys64\usr\bin\bash.exe") is False

    def test_wrapper_without_git_marker_rejected(self) -> None:
        """A ``bin/bash.exe`` that is not backed by a Git for Windows install
        (no ``cmd/git.exe`` marker) is not neutralized."""
        with patch("kimi_cli.tools.file.bash.bash_tool.os.path.isfile", return_value=False):
            assert _is_git_bash_install(r"C:\Program Files\Git\bin\bash.exe") is False

    def test_none_or_garbage_rejected(self) -> None:
        assert _is_git_bash_install(None) is False
        assert _is_git_bash_install("") is False
        assert _is_git_bash_install("/bin/bash") is False

    def test_marker_probe_uses_absolute_path(self) -> None:
        """The ``cmd/git.exe`` marker must be probed with a drive-anchored
        path.  A drive-relative probe ("C:foo") resolves against the process's
        current directory on drive C:, so after a ``chdir`` into a C: temp dir
        the marker lookup fails even for a real Git install and MSYSTEM
        neutralization is silently skipped (order-dependent test failures)."""
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.os.path.isfile",
            return_value=True,
        ) as isfile:
            assert _is_git_bash_install(r"C:\Program Files\Git\bin\bash.exe") is True
        probed = isfile.call_args.args[0]
        assert ntpath.isabs(probed), f"marker probe is drive-relative: {probed!r}"
        assert probed.lower() == r"c:\program files\git\cmd\git.exe"


class TestMsystemNeutralizedCommand:
    def test_win32_git_bash_prepends_prefix(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        with patch(
            "kimi_cli.tools.file.bash.bash_tool._is_git_bash_install",
            return_value=True,
        ):
            cmd = _with_msystem_neutralized("echo hi", r"C:\Program Files\Git\bin\bash.exe")
        assert cmd == "export MSYSTEM=; echo hi"

    def test_win32_non_git_bash_unchanged(self, monkeypatch: Any) -> None:
        """Real MSYS2 shells keep their ``MSYSTEM``; only Git Bash is
        neutralized."""
        monkeypatch.setattr(sys, "platform", "win32")
        with patch(
            "kimi_cli.tools.file.bash.bash_tool._is_git_bash_install",
            return_value=False,
        ):
            cmd = _with_msystem_neutralized("echo hi", r"C:\msys64\usr\bin\bash.exe")
        assert cmd == "echo hi"

    def test_non_windows_unchanged(self, monkeypatch: Any) -> None:
        """The change is strictly Windows-only: on Linux/macOS the command
        is returned unchanged even for a Git-style bash path."""
        monkeypatch.setattr(sys, "platform", "linux")
        cmd = _with_msystem_neutralized("echo hi", r"C:\Program Files\Git\bin\bash.exe")
        assert cmd == "echo hi"


class TestMSystemNeutralizationRealBash:
    @pytest.mark.skipif(
        sys.platform != "win32" or not BASH_AVAILABLE,
        reason="requires a real Git Bash on Windows",
    )
    def test_spawned_bash_sees_empty_msystem(self) -> None:
        """End-to-end: the bash actually chosen by the tool, launched with
        the tool's child environment and the neutralized command, sees an
        empty ``MSYSTEM`` (so xmake detects the native MSVC platform instead
        of mingw)."""
        bash = find_bash()
        assert bash is not None
        cmd = _with_msystem_neutralized(r'printf "%s" "$MSYSTEM"', bash)
        result = subprocess.run(
            [bash, "-c", cmd],
            env=_env_with_rg_bin_path(),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""


# ============================================================================
# Shell enhancement wiring: hardline floor, cwd/workdir, hints, redaction,
# background guidance (WP1-WP5)
# ============================================================================

class TestShellSafetyWiring:
    @pytest.fixture
    def bash_instance(self, mock_session: MagicMock) -> Bash:
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.find_bash",
            return_value=r"C:\Git\bin\bash.exe",
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool._should_enable_bash",
            return_value=True,
        ):
            return Bash(session=mock_session)

    @staticmethod
    def _completed_process_task() -> MagicMock:
        process_task = MagicMock()
        process_task.start = AsyncMock(return_value="bash-enhance-id")
        process_task.wait_with_monitor = AsyncMock(return_value=(False, 0.0, False))
        process_task.thread_is_alive = AsyncMock(return_value=False)
        process_task.stream = MagicMock()
        process_task.stream.pop_output = AsyncMock(return_value="mock output")
        process_task.stream.success = AsyncMock(return_value=True)
        process_task.stream.exit_code = 0
        process_task.stream.process_elapsed = None
        return process_task

    # -- hardline floor ----------------------------------------------------

    async def test_hardline_block_returns_error_before_process_task(
        self, bash_instance: Bash
    ) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.ProcessTask") as mock_pt:
            result = await bash_instance(BashParams(cmd="rm -rf /"))
        assert isinstance(result, ToolError)
        assert result.brief == "Blocked (hardline)"
        assert "hardline" in result.message
        mock_pt.assert_not_called()

    async def test_hardline_block_obfuscated_spelling(
        self, bash_instance: Bash
    ) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.ProcessTask") as mock_pt:
            result = await bash_instance(BashParams(cmd=r"r\m -rf /"))
        assert isinstance(result, ToolError)
        assert result.brief == "Blocked (hardline)"
        mock_pt.assert_not_called()

    async def test_hardline_skipped_when_config_disabled(
        self, mock_session: MagicMock
    ) -> None:
        mock_session.custom_config.get.return_value = {"shell": {"hardline": False}}
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.find_bash",
            return_value=r"C:\Git\bin\bash.exe",
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool._should_enable_bash",
            return_value=True,
        ):
            bash = Bash(session=mock_session)
        process_task = self._completed_process_task()
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask", return_value=process_task
        ) as mock_pt:
            result = await bash(BashParams(cmd="rm -rf /"))
        assert isinstance(result, ToolOk)
        mock_pt.assert_called_once()

    # -- self-kill guard ----------------------------------------------------

    async def test_self_kill_guard_blocks_own_pid(
        self, bash_instance: Bash
    ) -> None:
        with patch("kimi_cli.tools.file.bash.bash_tool.ProcessTask") as mock_pt:
            result = await bash_instance(BashParams(cmd=f"kill -9 {os.getpid()}"))
        assert isinstance(result, ToolError)
        assert result.brief == "Blocked (self-kill guard)"
        assert str(os.getpid()) in result.message
        mock_pt.assert_not_called()

    async def test_self_kill_guard_blocks_own_image_name(
        self, bash_instance: Bash
    ) -> None:
        image = Path(sys.executable).name  # e.g. python.exe hosting the agent
        with patch("kimi_cli.tools.file.bash.bash_tool.ProcessTask") as mock_pt:
            result = await bash_instance(BashParams(cmd=f"taskkill /F /IM {image}"))
        assert isinstance(result, ToolError)
        assert result.brief == "Blocked (self-kill guard)"
        mock_pt.assert_not_called()

    async def test_self_kill_guard_blocks_loop_pid_target(
        self, bash_instance: Bash
    ) -> None:
        # PID reached only through a shell loop variable (the shape that used
        # to slip through: ``for pid in ...; do taskkill /PID $pid ...``).
        cmd = f"for pid in {os.getpid()} 99999; do taskkill /PID $pid /T /F 2>/dev/null; done; echo done"
        with patch("kimi_cli.tools.file.bash.bash_tool.ProcessTask") as mock_pt:
            result = await bash_instance(BashParams(cmd=cmd))
        assert isinstance(result, ToolError)
        assert result.brief == "Blocked (self-kill guard)"
        assert str(os.getpid()) in result.message
        mock_pt.assert_not_called()

    async def test_self_kill_guard_allows_unrelated_loop(
        self, bash_instance: Bash
    ) -> None:
        process_task = self._completed_process_task()
        cmd = "for pid in 999999999 888888888; do taskkill /PID $pid /F; done"
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask", return_value=process_task
        ) as mock_pt:
            result = await bash_instance(BashParams(cmd=cmd))
        assert isinstance(result, ToolOk)
        mock_pt.assert_called_once()

    async def test_self_kill_guard_allows_unrelated_pid(
        self, bash_instance: Bash
    ) -> None:
        process_task = self._completed_process_task()
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask", return_value=process_task
        ) as mock_pt:
            result = await bash_instance(BashParams(cmd="kill 999999999"))
        assert isinstance(result, ToolOk)
        mock_pt.assert_called_once()

    async def test_self_kill_guard_skipped_when_config_disabled(
        self, mock_session: MagicMock
    ) -> None:
        mock_session.custom_config.get.return_value = {"shell": {"self_kill_guard": False}}
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.find_bash",
            return_value=r"C:\Git\bin\bash.exe",
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool._should_enable_bash",
            return_value=True,
        ):
            bash = Bash(session=mock_session)
        process_task = self._completed_process_task()
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask", return_value=process_task
        ) as mock_pt:
            result = await bash(BashParams(cmd=f"kill -9 {os.getpid()}"))
        assert isinstance(result, ToolOk)
        mock_pt.assert_called_once()

    # -- cwd / workdir removed ---------------------------------------------

    def test_cwd_and_workdir_params_removed(self) -> None:
        """Bash no longer exposes ``cwd``/``workdir``/``deduplicate_output``."""
        props = BashParams.model_json_schema()["properties"]
        for gone in ("cwd", "workdir", "deduplicate_output", "token_kill"):
            assert gone not in props, f"{gone} must be removed from BashParams"

    async def test_process_task_runs_without_cwd(self, bash_instance: Bash) -> None:
        """No working directory is passed to the subprocess anymore."""
        process_task = self._completed_process_task()
        with patch("kimi_cli.tools.file.bash.bash_tool.sys.platform", "win32"), patch(
            "kimix.utils.windows_env.refresh_env_from_registry"
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask",
            return_value=process_task,
        ) as mock_pt:
            result = await bash_instance(BashParams(cmd="echo hi"))
        assert isinstance(result, ToolOk)
        assert mock_pt.call_args.args[2] is None

    # -- exit-code meaning / failure hints ---------------------------------

    async def test_completed_failure_block_has_meaning_and_hint(
        self, bash_instance: Bash
    ) -> None:
        process_task = self._completed_process_task()
        process_task.stream.pop_output = AsyncMock(
            return_value="bash: no_such_cmd_xyz: command not found"
        )
        process_task.stream.success = AsyncMock(return_value=False)
        process_task.stream.exit_code = 127
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask", return_value=process_task
        ):
            result = await bash_instance(BashParams(cmd="no_such_cmd_xyz"))
        assert isinstance(result, ToolError)
        assert "exit_code_meaning:" in result.output
        assert "failure_hint:" in result.output
        assert "command was not found" in result.output.lower()
        assert "Hint:" in result.message

    async def test_completed_success_block_has_null_meaning_and_hint(
        self, bash_instance: Bash
    ) -> None:
        process_task = self._completed_process_task()
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask", return_value=process_task
        ):
            result = await bash_instance(BashParams(cmd="echo hi"))
        assert isinstance(result, ToolOk)
        assert "exit_code_meaning: null" in result.output
        assert "failure_hint: null" in result.output

    # -- timeout branch guidance -------------------------------------------

    async def test_timeout_branch_includes_guidance_for_server_command(
        self, bash_instance: Bash
    ) -> None:
        process_task = self._completed_process_task()
        process_task.thread_is_alive = AsyncMock(return_value=True)
        process_task.stream.pop_output = AsyncMock(return_value="")
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask", return_value=process_task
        ):
            result = await bash_instance(BashParams(cmd="npm run dev", timeout=1))
        assert isinstance(result, ToolError)
        assert result.brief == "Timeout"
        assert "Running in background" in result.message
        assert "Long-running command detected" in result.message
        assert "job_output" in result.message

    async def test_timeout_branch_silent_command_keeps_background_task(
        self, bash_instance: Bash
    ) -> None:
        process_task = self._completed_process_task()
        process_task.thread_is_alive = AsyncMock(return_value=True)
        process_task.stream.pop_output = AsyncMock(return_value="")
        process_task.stop = AsyncMock()
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask", return_value=process_task
        ):
            result = await bash_instance(BashParams(cmd="sleep 5", timeout=1))
        assert isinstance(result, ToolError)
        assert result.brief == "Timeout"
        # No output: keep the task and hand off with a `job_output` hint.
        assert "No output after" in result.message
        assert "task_id" in result.message
        assert "job_output" in result.message
        assert "Long-running command detected" not in result.message
        # The still-alive silent command is NOT killed (it stays in the
        # background so the handed-off task_id remains valid).
        process_task.stop.assert_not_awaited()

    async def test_timeout_branch_after_output_kills_and_reports_timeout(
        self, bash_instance: Bash
    ) -> None:
        process_task = self._completed_process_task()
        process_task.thread_is_alive = AsyncMock(return_value=True)
        process_task.stream.pop_output = AsyncMock(return_value="partial output")
        process_task.stop = AsyncMock()
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask", return_value=process_task
        ):
            result = await bash_instance(BashParams(cmd="make", timeout=1))
        assert isinstance(result, ToolError)
        assert result.brief == "Timeout"
        # Output then a stall: the tree is killed and the task removed.
        assert "timed out" in result.message
        process_task.stop.assert_awaited_once()
        assert get_all_tasks(bash_instance._session) == {}

    # -- secret redaction (config-gated) ------------------------------------

    async def test_redaction_applied_in_process_output(
        self, mock_session: MagicMock
    ) -> None:
        mock_session.custom_config.get.return_value = {"shell": {"redact_secrets": True}}
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.find_bash",
            return_value=r"C:\Git\bin\bash.exe",
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool._should_enable_bash",
            return_value=True,
        ):
            bash = Bash(session=mock_session)
        process_task = self._completed_process_task()
        process_task.stream.pop_output = AsyncMock(
            return_value="token=ghp_1234567890123456789012"
        )
        process_task.stream.success = AsyncMock(return_value=True)
        process_task.stream.exit_code = 0
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask", return_value=process_task
        ):
            result = await bash(BashParams(cmd="echo token"))
        assert isinstance(result, ToolOk)
        assert "ghp_" not in result.output
        assert "[REDACTED]" in result.output

    async def test_redaction_disabled_by_config(self, mock_session: MagicMock) -> None:
        mock_session.custom_config.get.return_value = {"shell": {"redact_secrets": False}}
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.find_bash",
            return_value=r"C:\Git\bin\bash.exe",
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool._should_enable_bash",
            return_value=True,
        ):
            bash = Bash(session=mock_session)
        process_task = self._completed_process_task()
        process_task.stream.pop_output = AsyncMock(
            return_value="token=ghp_1234567890123456789012"
        )
        process_task.stream.success = AsyncMock(return_value=True)
        process_task.stream.exit_code = 0
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask", return_value=process_task
        ):
            result = await bash(BashParams(cmd="echo token"))
        assert isinstance(result, ToolOk)
        assert "ghp_1234567890123456789012" in result.output


# ============================================================================
# Original-saved suffix on filtered output
# ============================================================================


class TestBashOriginalSavedSuffix:
    @pytest.fixture
    def bash_instance(self, mock_session: MagicMock) -> Bash:
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.find_bash",
            return_value=r"C:\Git\bin\bash.exe",
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool._should_enable_bash",
            return_value=True,
        ):
            return Bash(session=mock_session)

    @staticmethod
    def _completed_process_task(output: str = "fixed output") -> MagicMock:
        process_task = MagicMock()
        process_task.start = AsyncMock(return_value="bash-suffix-id")
        process_task.wait_with_monitor = AsyncMock(return_value=(False, 0.0, False))
        process_task.thread_is_alive = AsyncMock(return_value=False)
        process_task.stream = MagicMock()
        process_task.stream.pop_output = AsyncMock(return_value=output)
        process_task.stream.success = AsyncMock(return_value=True)
        process_task.stream.exit_code = 0
        process_task.stream.process_elapsed = None
        return process_task

    @staticmethod
    def _random_json_lines(n: int = 2000, seed: int = 999) -> str:
        """High-entropy JSON-ish lines that survive the token filter unchanged
        (no repeats, no near-duplicate patterns) while staying >64KB."""
        import random as _random
        import string as _string

        _random.seed(seed)

        def rand_str(length: int) -> str:
            return "".join(
                _random.choice(
                    _string.ascii_lowercase + _string.digits + " .,;:!?()[]{}-_=+"
                )
                for _ in range(length)
            )

        return "\n".join(
            f'{{"id": {_random.randint(0, 10**9)}, "val": "{rand_str(20)}"}}'
            for _ in range(n)
        )

    @staticmethod
    def _repeated_block_output(seed: int = 123) -> str:
        """Unique high-entropy lines plus a repeated block: the dedup stage
        collapses the block (changing the output, so the token filter saves the
        pre-filter original) while the unique lines keep the result >64KB."""
        import random as _random
        import string as _string

        _random.seed(seed)

        def rand_line() -> str:
            return "".join(
                _random.choice(_string.ascii_lowercase + _string.digits)
                for _ in range(40)
            )

        return "\n".join([rand_line() for _ in range(2500)] + ["ERROR"] * 100)

    async def test_message_includes_original_path_after_dedup(
        self, bash_instance: Bash
    ) -> None:
        process_task = self._completed_process_task(output="ERROR\n" * 10)
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask", return_value=process_task
        ):
            result = await bash_instance(BashParams(cmd="echo hi"))
        assert isinstance(result, ToolOk)
        assert "[original saved to .kimix_cache/tmp_" in result.message

    async def test_message_includes_original_path_after_truncate(
        self, bash_instance: Bash
    ) -> None:
        long_output = "\n".join(f"line_{i}" for i in range(500))
        process_task = self._completed_process_task(output=long_output)
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask", return_value=process_task
        ):
            result = await bash_instance(BashParams(cmd="echo hi", max_lines=10))
        assert isinstance(result, ToolOk)
        assert "[original saved to .kimix_cache/tmp_" in result.message

    async def test_no_suffix_when_filter_unchanged(
        self, bash_instance: Bash
    ) -> None:
        """Dedup is always on, but output with no repeats is left unchanged,
        so no original temp file is created and no suffix is appended."""
        process_task = self._completed_process_task(output="plain output")
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask", return_value=process_task
        ):
            result = await bash_instance(BashParams(cmd="echo hi"))
        assert isinstance(result, ToolOk)
        assert "[original saved to" not in result.message

    async def test_max_lines_folding_sets_output_truncated(
        self, bash_instance: Bash
    ) -> None:
        """max_lines folding drops lines, so the structured block must report
        output_truncated: true (a consumer trusting the flag would otherwise
        assume the full output was returned)."""
        long_output = "\n".join(f"line_{i}" for i in range(500))
        process_task = self._completed_process_task(output=long_output)
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask", return_value=process_task
        ):
            result = await bash_instance(BashParams(cmd="echo hi", max_lines=10))
        assert isinstance(result, ToolOk)
        assert "output_truncated: true" in result.output
        assert "lines omitted" in result.output

    async def test_output_not_truncated_when_filter_unchanged(
        self, bash_instance: Bash
    ) -> None:
        """Short, repeat-free output passes the filter verbatim: the block
        must keep reporting output_truncated: false."""
        process_task = self._completed_process_task(output="plain output")
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask", return_value=process_task
        ):
            result = await bash_instance(BashParams(cmd="echo hi"))
        assert isinstance(result, ToolOk)
        assert "output_truncated: false" in result.output

    async def test_dedup_sets_output_truncated(
        self, bash_instance: Bash
    ) -> None:
        """Dedup collapses repeated lines, which also drops content: the
        block must report output_truncated: true."""
        repeated = "ERROR: timeout\n" * 10
        process_task = self._completed_process_task(output=repeated)
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask", return_value=process_task
        ):
            result = await bash_instance(BashParams(cmd="echo hi"))
        assert isinstance(result, ToolOk)
        assert "output_truncated: true" in result.output

    async def test_message_includes_original_path_after_summarize(
        self, bash_instance: Bash
    ) -> None:
        """A >64KB output that survives the (always-on) token filter unchanged
        is still preserved before summarization replaces it with a summary."""
        long_output = self._random_json_lines()
        assert len(long_output) > 65536
        process_task = self._completed_process_task(output=long_output)
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask", return_value=process_task
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool._summarize_long_output_async",
            new=AsyncMock(return_value="[summary]"),
        ):
            result = await bash_instance(BashParams(cmd="echo hi"))
        assert isinstance(result, ToolOk)
        assert "output_truncated: true" in result.output
        assert "[original saved to .kimix_cache/tmp_" in result.message
        # The saved file must contain the full pre-summary output.
        saved = result.message.split("[original saved to ", 1)[1].rstrip("]")
        import anyio
        async with await anyio.open_file(saved, "r") as f:
            assert await f.read() == long_output

    async def test_summarize_saves_original_when_rtk_rewritten(
        self, bash_instance: Bash
    ) -> None:
        """RTK-rewritten commands skip local dedup; a long output that is not
        rtk-folded must still be preserved before summarization."""
        long_output = self._random_json_lines()
        with patch(
            "kimi_cli.tools.file.bash.bash_tool._summarize_long_output_async",
            new=AsyncMock(return_value="[summary]"),
        ):
            display, _path, truncated, original_path = (
                await bash_instance._process_output(
                    BashParams(cmd="echo hi"),
                    long_output,
                    rtk_rewritten=True,
                )
            )
        assert truncated is True
        assert display == "[summary]"
        assert original_path is not None
        import anyio
        async with await anyio.open_file(original_path, "r") as f:
            assert await f.read() == long_output

    async def test_summarize_keeps_existing_original_path(
        self, bash_instance: Bash
    ) -> None:
        """When the (always-on) dedup already saved the pre-filter original,
        summarization must reuse that path instead of writing a new file."""
        long_output = self._repeated_block_output()
        assert len(long_output) > 65536
        save_calls: list[tuple[str, str | None]] = []

        async def fake_save(output: str, original_path: str | None) -> str | None:
            save_calls.append((output, original_path))
            return original_path

        with patch(
            "kimi_cli.tools.file.bash.bash_tool._save_original_output_async",
            new=fake_save,
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool._summarize_long_output_async",
            new=AsyncMock(return_value="[summary]"),
        ):
            display, _path, truncated, original_path = (
                await bash_instance._process_output(
                    BashParams(cmd="echo hi"), long_output
                )
            )
        assert truncated is True
        assert display == "[summary]"
        assert save_calls, "summarize branch must consult the original saver"
        # The dedup stage collapsed the repeated block, so an original was
        # already saved; the summarize branch must keep it (non-None arg).
        assert save_calls[0][1] is not None
        assert original_path == save_calls[0][1]
        # The saved file is the full pre-filter original, not the summary.
        import anyio
        async with await anyio.open_file(original_path, "r") as f:
            assert await f.read() == long_output


# ============================================================================
# Powershell parity: original saved before summarization
# ============================================================================


class TestPowershellOriginalSavedSuffix:
    @pytest.fixture
    def pwsh_instance(self, mock_session: MagicMock) -> Powershell:
        with patch(
            "kimi_cli.tools.file.bash.pwsh_tool._bash_tool._should_enable_powershell",
            return_value=True,
        ):
            return Powershell(session=mock_session)

    async def test_process_output_saves_original_before_summarize(
        self, pwsh_instance: Powershell
    ) -> None:
        """Powershell mirrors the Bash fix: an unchanged >64KB output is saved
        before the summarization branch replaces it with a summary."""
        long_output = TestBashOriginalSavedSuffix._random_json_lines()
        with patch(
            "kimi_cli.tools.file.bash.pwsh_tool._summarize_long_output_async",
            new=AsyncMock(return_value="[summary]"),
        ):
            display, _path, truncated, original_path = (
                await pwsh_instance._process_output(
                    "echo hi",
                    PowershellParams(cmd="echo hi"),
                    long_output,
                )
            )
        assert truncated is True
        assert display == "[summary]"
        assert original_path is not None
        import anyio
        async with await anyio.open_file(original_path, "r") as f:
            assert await f.read() == long_output


# ============================================================================
# Failed command saved to a temp script file (.sh / .ps1)
# ============================================================================


class TestBashFailedCommandSaved:
    """Long failing bash commands are preserved as `.sh` temp files whose path
    is returned in the tool message; short commands are not saved."""

    @pytest.fixture
    def bash_instance(self, mock_session: MagicMock) -> Bash:
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.find_bash",
            return_value=r"C:\Git\bin\bash.exe",
        ), patch(
            "kimi_cli.tools.file.bash.bash_tool._should_enable_bash",
            return_value=True,
        ):
            return Bash(session=mock_session)

    @staticmethod
    def _failed_process_task(output: str = "boom") -> MagicMock:
        process_task = MagicMock()
        process_task.start = AsyncMock(return_value="bash-cmd-saved-id")
        process_task.wait_with_monitor = AsyncMock(return_value=(False, 0.0, False))
        process_task.thread_is_alive = AsyncMock(return_value=False)
        process_task.stream = MagicMock()
        process_task.stream.pop_output = AsyncMock(return_value=output)
        process_task.stream.success = AsyncMock(return_value=False)
        process_task.stream.exit_code = 1
        process_task.stream.process_elapsed = None
        return process_task

    async def test_long_failed_command_is_saved_to_sh_file(
        self, bash_instance: Bash
    ) -> None:
        long_cmd = "echo start && " + "true && " * 30 + "false"
        assert len(long_cmd) > 50
        process_task = self._failed_process_task()
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask", return_value=process_task
        ):
            result = await bash_instance(BashParams(cmd=long_cmd))
        assert isinstance(result, ToolError)
        assert "[command saved to .kimix_cache/tmp_" in result.message
        saved = result.message.split("[command saved to ", 1)[1].split("]", 1)[0]
        assert saved.endswith(".sh")
        assert Path(saved).read_text(encoding="utf-8") == long_cmd
        assert "Edit the saved script and run it again with this tool (bash) to retry." in result.message

    async def test_short_failed_command_not_saved(self, bash_instance: Bash) -> None:
        process_task = self._failed_process_task()
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask", return_value=process_task
        ):
            result = await bash_instance(BashParams(cmd="false"))
        assert isinstance(result, ToolError)
        assert "[command saved to" not in result.message

    async def test_long_successful_command_not_saved(self, bash_instance: Bash) -> None:
        long_cmd = "echo start && " + "true && " * 30 + "true"
        assert len(long_cmd) > 50
        process_task = MagicMock()
        process_task.start = AsyncMock(return_value="bash-cmd-ok-id")
        process_task.wait_with_monitor = AsyncMock(return_value=(False, 0.0, False))
        process_task.thread_is_alive = AsyncMock(return_value=False)
        process_task.stream = MagicMock()
        process_task.stream.pop_output = AsyncMock(return_value="")
        process_task.stream.success = AsyncMock(return_value=True)
        process_task.stream.exit_code = 0
        process_task.stream.process_elapsed = None
        with patch(
            "kimi_cli.tools.file.bash.bash_tool.ProcessTask", return_value=process_task
        ):
            result = await bash_instance(BashParams(cmd=long_cmd))
        assert isinstance(result, ToolOk)
        assert "[command saved to" not in result.message


class TestPowershellFailedCommandSaved:
    """Long failing PowerShell commands are preserved as `.ps1` temp files whose
    path is returned in the tool message; short commands are not saved."""

    @pytest.fixture
    def pwsh_instance(self, mock_session: MagicMock) -> Powershell:
        with patch(
            "kimi_cli.tools.file.bash.pwsh_tool._bash_tool._should_enable_powershell",
            return_value=True,
        ):
            return Powershell(session=mock_session)

    @staticmethod
    def _failed_process_task(output: str = "boom") -> MagicMock:
        process_task = MagicMock()
        process_task.start = AsyncMock(return_value="pwsh-cmd-saved-id")
        process_task.wait_with_monitor = AsyncMock(return_value=(False, 0.0, False))
        process_task.thread_is_alive = AsyncMock(return_value=False)
        process_task.stream = MagicMock()
        process_task.stream.pop_output = AsyncMock(return_value=output)
        process_task.stream.success = AsyncMock(return_value=False)
        process_task.stream.exit_code = 1
        process_task.stream.process_elapsed = None
        return process_task

    async def test_long_failed_command_is_saved_to_ps1_file(
        self, pwsh_instance: Powershell
    ) -> None:
        long_cmd = "Write-Output 'start'; " + "Write-Host 'x'; " * 20 + "exit 1"
        assert len(long_cmd) > 50
        process_task = self._failed_process_task()
        with patch(
            "kimi_cli.tools.file.bash.pwsh_tool.ProcessTask", return_value=process_task
        ):
            result = await pwsh_instance(PowershellParams(cmd=long_cmd))
        assert isinstance(result, ToolError)
        assert "[command saved to .kimix_cache/tmp_" in result.message
        saved = result.message.split("[command saved to ", 1)[1].split("]", 1)[0]
        assert saved.endswith(".ps1")
        assert Path(saved).read_text(encoding="utf-8") == long_cmd
        assert "Edit the saved script and run it again with this tool (PowerShell) to retry." in result.message

    async def test_short_failed_command_not_saved(
        self, pwsh_instance: Powershell
    ) -> None:
        process_task = self._failed_process_task()
        with patch(
            "kimi_cli.tools.file.bash.pwsh_tool.ProcessTask", return_value=process_task
        ):
            result = await pwsh_instance(PowershellParams(cmd="exit 1"))
        assert isinstance(result, ToolError)
        assert "[command saved to" not in result.message


# ============================================================================
# cmd / command accepts a script file (description mention)
# ============================================================================


class TestCommandParamAcceptsScriptFile:
    def test_bash_cmd_description_mentions_sh_script(self) -> None:
        desc = BashParams.model_json_schema()["properties"]["command"]["description"]
        assert "`.sh` script file" in desc
        assert "executed via bash" in desc

    def test_pwsh_cmd_description_mentions_ps1_script(self) -> None:
        desc = PowershellParams.model_json_schema()["properties"]["command"]["description"]
        assert "`.ps1` script file" in desc
        assert "executed via PowerShell" in desc


# ============================================================================
# Command-operand wrappers (timeout/stdbuf/nice/xargs/gtimeout/watch)
# ============================================================================


class TestBashFixCommandOperandWrappers:
    """Wrappers whose COMMAND operand is scanned as a command context.

    ``timeout``/``stdbuf``/``nice``/``xargs`` are bundled Git Bash executables
    that exec their operand, and ``gtimeout``/``watch`` are fallback names
    with the same shape.  Without operand scanning, ``timeout 5 rev <<< abc``
    fails on Git Bash with ``timeout: failed to run command 'rev'`` even
    though the plain ``rev <<< abc`` form is fixed — a command that works on
    native POSIX bash but not on Windows.
    """

    @pytest.mark.parametrize(
        "source",
        [
            "timeout 5 rev <<< abc",
            "timeout 5s rev <<< abc",
            "timeout --foreground 5 rev <<< abc",
            "timeout -s KILL 5 rev <<< abc",
            "timeout --signal KILL 5 rev <<< abc",
            "timeout --kill-after 1 5 rev <<< abc",
            "timeout -- 5 rev <<< abc",
            "stdbuf -oL rev <<< abc",
            "stdbuf --output=L rev <<< abc",
            "stdbuf -o L -e L rev <<< abc",
            "nice -n 5 rev <<< abc",
            "nice --adjustment 5 rev <<< abc",
            "nice rev <<< abc",
            "xargs rev",
            "xargs -0 rev",
            "xargs -n 2 rev",
            "xargs -I {} rev",
            "printf x | xargs rev",
            "time timeout 5 rev <<< abc",
            "nohup timeout 5 rev <<< abc",
            "env timeout 5 rev <<< abc",
            "timeout 5 python3 --version",
        ],
    )
    def test_operand_fallback_is_recorded(self, source: str) -> None:
        result = _fix_for_windows(source)
        expected = "python3" if "python3" in source else "rev"
        assert result.replacements == (expected,)
        assert result.changed
        assert "/usr/bin/bash -c" in result.command

    def test_timeout_operand_gets_standalone_runner(self) -> None:
        # ``timeout`` execs its operand, so the fallback word must become the
        # self-contained runner, not a function call.
        result = _fix_for_windows("timeout 5 rev <<< abc")
        source = result.command.split("\n")[-1]
        assert source.startswith("timeout 5 /usr/bin/bash -c ")
        assert source.endswith(" -- <<< abc")

    def test_gtimeout_records_both_names_and_runners(self) -> None:
        result = _fix_for_windows("gtimeout 5 rev <<< abc")
        assert result.replacements == ("gtimeout", "rev")
        source = result.command.split("\n")[-1]
        assert source.startswith("gtimeout 5 /usr/bin/bash -c ")
        assert source.endswith(" -- <<< abc")

    @pytest.mark.parametrize(
        "command",
        [
            "timeout 5 make test",
            "timeout 5 true",
            "nice -n 5 true",
            "stdbuf -oL echo ok",
            "xargs echo",
            "find . -name '*.py' | xargs grep foo",
            "timeout 5 bash -c 'a && b'",
        ],
    )
    def test_bundled_operands_are_not_rewritten(self, command: str) -> None:
        assert _fix_for_windows(command) == BashFix(command)

    @pytest.mark.parametrize(
        "command",
        [
            "echo timeout 5 rev",
            "run=timeout",
            "echo watch date",
            "echo 'watch rev'",
            "echo xargs rev",
            "case x in timeout) echo no;; esac",
            "alias watch='tail -f log'",
            "function timeout { :; }",
            "timeout() { :; }",
            "echo hi # timeout 5 rev",
        ],
    )
    def test_wrapper_names_in_data_positions_unchanged(self, command: str) -> None:
        assert _fix_for_windows(command) == BashFix(command)

    def test_xargs_path_valued_option_is_rewritten(self) -> None:
        result = _fix_for_windows(r"xargs -a C:\in.txt rev")
        assert result.replacements == ("rev",)
        assert result.path_changes == (r"C:\in.txt",)
        source = result.command.split("\n")[-1]
        assert source.startswith(r"xargs -a C:/in.txt /usr/bin/bash -c ")

    def test_xargs_inline_path_option_is_rewritten(self) -> None:
        result = _fix_for_windows(r"xargs --arg-file=C:\in.txt rev")
        assert result.path_changes == (r"--arg-file=C:\in.txt",)
        source = result.command.split("\n")[-1]
        assert source.startswith(r"xargs --arg-file=C:/in.txt /usr/bin/bash -c ")

    def test_watch_records_command_operand_names(self) -> None:
        # ``watch`` re-runs its command inside the same shell, so the operand
        # stays a plain word and both fallbacks are defined + exported.
        result = _fix_for_windows("watch -n 1 rev <<< abc")
        assert result.replacements == ("watch", "rev")
        assert result.command.split("\n")[-1] == "watch -n 1 rev <<< abc"

    @pytest.mark.parametrize(
        "source",
        [
            "watch 'rev <<< abc'",
            "watch \"rev <<< abc\"",
            "watch -n 1 'rev <<< abc'",
        ],
    )
    def test_watch_quoted_script_is_scanned(self, source: str) -> None:
        result = _fix_for_windows(source)
        assert result.replacements == ("watch", "rev")
        assert result.command.split("\n")[-1] == source

    def test_watch_quoted_script_inner_path_is_fixed(self) -> None:
        result = _fix_for_windows(r"watch 'cd C:\x && rev'")
        assert result.replacements == ("watch", "rev")
        assert result.path_changes == (r"C:\x",)
        assert result.command.split("\n")[-1] == "watch 'cd C:/x && rev'"

    def test_watch_body_runs_command_through_eval(self) -> None:
        # procps ``watch`` executes its command line via ``sh -c``; the
        # fallback mirrors that with ``eval "$*"`` so quoted command strings
        # and same-shell fallback functions both work.
        result = _fix_for_windows("watch rev")
        assert 'clear; eval "$*"' in result.command

    def test_watch_operand_options_are_consumed(self) -> None:
        # ``-n 1`` is watch's interval, not its command; ``rev`` is.
        result = _fix_for_windows("watch -t -d -n 1 rev <<< abc")
        assert result.replacements == ("watch", "rev")
        assert result.command.split("\n")[-1] == "watch -t -d -n 1 rev <<< abc"

    def test_fallbacks_are_exported_for_nested_shells(self) -> None:
        result = _fix_for_windows("rev <<< abc")
        # Conditional export: the definition's ``command -v`` guard may have
        # found a real executable on PATH (e.g. coreutils ``uptime``), in
        # which case there is no function to export and an unconditional
        # ``export -f`` would pollute stderr with "not a function" noise.
        assert (
            "if declare -F rev >/dev/null; then export -f rev; fi" in result.command
        )

    @pytest.mark.parametrize("platform", ["linux", "darwin", "freebsd"])
    def test_operand_wrappers_noop_on_non_windows(self, platform: str) -> None:
        source = "timeout 5 rev <<< abc; watch -n 1 xargs rev"
        assert _fix_for_platform(source, platform) == BashFix(source)


class TestBashFixShellWrapperUnderCommandWrapper:
    """``bash -c`` operands of command wrappers stay in place.

    Unwrapping ``env bash -c 'a && b'`` would move ``&&`` out of the wrapper's
    argv and break the command, and the wrapper executable cannot invoke the
    shell functions the plain unwrap relies on.  Under an active wrapper the
    inline script is fixed in place and the nested bash inherits the exported
    fallback functions instead.
    """

    @pytest.mark.parametrize(
        ("source", "expected_replacements"),
        [
            ("env bash -c 'rev <<< abc'", ("rev",)),
            # ``sudo`` is a fallback name too: its definition is recorded
            # alongside the wrapped command's.
            ("sudo bash -c 'rev <<< abc'", ("sudo", "rev")),
            ("timeout 5 bash -c 'rev <<< abc'", ("rev",)),
            ("nohup bash -c 'rev <<< abc'", ("rev",)),
        ],
    )
    def test_dash_c_script_is_fixed_in_place(
        self, source: str, expected_replacements: tuple[str, ...]
    ) -> None:
        result = _fix_for_windows(source)
        assert result.replacements == expected_replacements
        assert result.command.split("\n")[-1] == source
        assert "if declare -F rev >/dev/null; then export -f rev; fi" in result.command

    def test_dash_c_script_inner_path_is_fixed_in_place(self) -> None:
        result = _fix_for_windows(r"nohup bash -c 'cd C:\x && rev'")
        assert result.replacements == ("rev",)
        assert result.path_changes == (r"C:\x",)
        assert result.command.split("\n")[-1] == "nohup bash -c 'cd C:/x && rev'"

    def test_assignment_scoped_bash_c_is_untouched(self) -> None:
        # ``FOO=1`` is scoped to the wrapped shell process; unwrapping would
        # make the outer shell expand ``$FOO`` too early.
        source = 'env FOO=1 bash -c \'printf %s "$FOO"\''
        assert _fix_for_windows(source) == BashFix(source)

    def test_bare_shell_prefix_under_wrapper_gets_runner(self) -> None:
        result = _fix_for_windows("env bash rev <<< abc")
        assert result.replacements == ("rev",)
        assert result.shell_wrappers == ("bash",)
        source = result.command.split("\n")[-1]
        assert source.startswith("env /usr/bin/bash -c ")
        assert source.endswith(" -- <<< abc")

    def test_operators_stay_inside_bash_c(self) -> None:
        source = "timeout 5 bash -c 'rev <<< abc && rev <<< xyz'"
        result = _fix_for_windows(source)
        assert result.replacements == ("rev",)
        assert result.command.split("\n")[-1] == source


class TestBashFixRealGitBashOperands:
    """Execution tests: wrapper operands run the fallback on real Git Bash."""

    @staticmethod
    def _run(command: str, *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        bash = find_bash()
        assert bash is not None
        fixed = _fix_for_windows(command)
        for attempt in range(2):
            try:
                return subprocess.run(
                    [bash, "-lc", fixed.command],
                    input=stdin,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                if attempt == 1:
                    raise
                time.sleep(1)
        raise AssertionError("unreachable")

    @pytest.mark.parametrize(
        "command",
        [
            "timeout 5 rev <<< abc",
            "gtimeout 5 rev <<< abc",
            "stdbuf -oL rev <<< abc",
            "nice -n 5 rev <<< abc",
            "nice rev <<< abc",
        ],
    )
    def test_wrapper_operand_runs_fallback(self, command: str) -> None:
        result = self._run(command)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "cba\n"

    def test_xargs_operand_runs_fallback(self, tmp_path: Path) -> None:
        posix = str(tmp_path).replace("\\", "/")
        result = self._run(
            f"cd {posix} && echo cba > f1 && printf 'f1\\n' | xargs -n 1 rev"
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "abc\n"

    def test_xargs_gtimeout_operand_runs_fallback(self, tmp_path: Path) -> None:
        posix = str(tmp_path).replace("\\", "/")
        result = self._run(
            f"cd {posix} && echo cba > f1 && printf 'f1\\n' | xargs -n 1 gtimeout 2 rev"
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "abc\n"

    @pytest.mark.parametrize(
        "command",
        [
            "timeout 3 watch -n 1 rev <<< abc",
            "timeout 3 watch -n 1 'rev <<< abc'",
        ],
    )
    def test_watch_operand_runs_until_killed(self, command: str) -> None:
        result = self._run(command)
        assert "cba" in result.stdout
        assert "command not found" not in result.stderr

    def test_nested_bash_c_inherits_exported_fallback(self) -> None:
        result = self._run("env bash -c 'rev <<< abc'")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "cba\n"

    def test_nohup_bash_c_inherits_exported_fallback(self) -> None:
        result = self._run("nohup bash -c 'rev <<< abc'")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "cba\n"

    def test_timeout_bash_c_script_fixed_in_place(self, tmp_path: Path) -> None:
        backslash = str(tmp_path).replace("/", "\\").replace(" ", "\\ ")
        result = self._run(f"timeout 5 bash -c 'cd {backslash} && rev <<< yz'")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "zy\n"





# ======================================================================
# Round-0 review regressions for the Windows Git Bash compatibility
# scanner (bin/kimix_native/_shell_compat.py, exposed through
# ``bash_fix.fix_bash_command``).
# ======================================================================


def _round0_find_git_bash() -> str | None:
    """Return an executable MSYS/MINGW bash, or None.

    A bare ``bash`` resolved through the subprocess PATH can be the WSL
    launcher (or a Store alias) that does not run Git Bash commands, so the
    candidates are probed with ``uname`` before being trusted.
    """
    candidates = [
        os.environ.get("GIT_BASH"),
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files\Git\bin\bash.exe",
        shutil.which("bash"),
        "/usr/bin/bash",
    ]
    for cand in candidates:
        if not cand or not os.path.exists(cand):
            continue
        try:
            out = subprocess.run(
                [cand, "-c", "uname"], capture_output=True, timeout=15
            ).stdout.decode("utf-8", "replace")
        except Exception:
            continue
        if any(k in out for k in ("MINGW", "MSYS", "CYGWIN")):
            return cand
    return None


_ROUND0_GIT_BASH = _round0_find_git_bash()


def _round0_bash_env() -> dict[str, str]:
    """Environment for the Git Bash subprocesses started by these tests.

    Some earlier tests in this module mutate the process ``PATH``; prepend
    the Git installation's ``usr\bin``/``bin`` directories so the fallback
    bodies always find ``perl`` and the coreutils they delegate to.
    """
    env = os.environ.copy()
    if _ROUND0_GIT_BASH is not None:
        exe_dir = os.path.dirname(_ROUND0_GIT_BASH)
        candidates = [
            exe_dir,
            os.path.normpath(os.path.join(exe_dir, "..", "usr", "bin")),
        ]
        for extra in reversed(candidates):
            if os.path.isdir(extra):
                env["PATH"] = extra + os.pathsep + env.get("PATH", "")
    return env


def _round0_run_fallback(
    body: str, name: str, argv: list[str], timeout: int = 8
) -> subprocess.CompletedProcess:
    """Define ``name`` from a fallback body and call it with ``argv``.

    The body is used verbatim (no native-presence guard) so the test is
    independent of whether this host happens to ship the tool: it exercises
    the *generated fallback code*, which is the thing under review.
    """
    assert _ROUND0_GIT_BASH is not None
    script = f"{name}() {{ {body}; }}\n{name} {subprocess.list2cmdline(argv)}\n"
    return subprocess.run(
        [_ROUND0_GIT_BASH, "-c", script],
        capture_output=True,
        timeout=timeout,
        env=_round0_bash_env(),
    )


class TestBashFixRound0PathGlobQuoting:
    """Path rewrites must not quote away live glob/brace expansion.

    The backslash-path branch of the scanner double-quoted the *entire*
    normalized word as soon as any character forced quoting (an escaped
    space, an ``&``), which pushed ``*``/``?``/``{}`` inside the quotes and
    turned a live pathname/brace expansion into a fixed literal name.
    """

    @staticmethod
    def _unquoted_part(command: str) -> str:
        """Return the characters of ``command`` NOT inside double quotes."""
        out: list[str] = []
        in_q = False
        i = 0
        while i < len(command):
            ch = command[i]
            if in_q:
                if ch == '"':
                    in_q = False
                elif ch == "\\":
                    i += 2
                    continue
            elif ch == '"':
                in_q = True
            else:
                out.append(ch)
            i += 1
        return "".join(out)

    def test_glob_after_escaped_space_stays_unquoted(self) -> None:
        result = fix_bash_command(r"ls D:\a\ b\*.txt")
        assert "*" in self._unquoted_part(result.command), (
            f"glob metacharacter was quoted away, expansion lost: {result.command!r}"
        )

    def test_glob_in_spaced_program_files_stays_unquoted(self) -> None:
        result = fix_bash_command(r"ls C:\Program\ Files\*\x")
        assert "*" in self._unquoted_part(result.command), (
            f"glob metacharacter was quoted away, expansion lost: {result.command!r}"
        )

    def test_brace_expansion_after_escaped_space_stays_unquoted(self) -> None:
        result = fix_bash_command(r"ls D:\a\ b\{x,y}")
        assert "{" in self._unquoted_part(result.command), (
            f"brace metacharacters were quoted away, expansion lost: {result.command!r}"
        )

    def test_question_glob_after_escaped_space_stays_unquoted(self) -> None:
        result = fix_bash_command(r"cp D:\a\ b\?.txt /t")
        assert "?" in self._unquoted_part(result.command), (
            f"single-char glob was quoted away, expansion lost: {result.command!r}"
        )


@pytest.mark.skipif(
    _ROUND0_GIT_BASH is None, reason="no MSYS/MINGW Git Bash available"
)
class TestBashFixRound0FallbackMissingOptionValue:
    """A fallback option arm consuming a value must not hang when the value
    is missing (the option is the last word on the command line).

    The generated option loops used an unguarded ``shift 2``: with ``$# == 1``
    the shift fails and does not reduce ``$#``, so ``while (( $# ))`` spun
    forever.  Real tools print "option requires an argument" and exit
    non-zero instead.
    """

    def test_tree_L_without_value_does_not_hang(self) -> None:
        fixed = fix_bash_command("tree -L")
        assert "tree" in fixed.replacements
        body = _shell_compat._FALLBACK_BODIES["tree"]
        try:
            result = _round0_run_fallback(body, "tree", ["-L"])
        except subprocess.TimeoutExpired:
            pytest.fail(
                "tree fallback with a missing -L value hangs forever "
                "(`shift 2` on a single-arg loop); expected a clean "
                "'option requires an argument' error and a quick non-zero exit"
            )
        assert result.returncode != 0, "expected an argument-error exit status"

    @pytest.mark.parametrize("option", ["-d", "-n"])
    def test_top_option_without_value_does_not_hang(self, option: str) -> None:
        body = _shell_compat._FALLBACK_BODIES["top"]
        try:
            _round0_run_fallback(body, "top", [option])
        except subprocess.TimeoutExpired:
            pytest.fail(
                f"top fallback with a missing {option} value hangs forever "
                "(`shift 2` on a single-arg loop)"
            )


@pytest.mark.skipif(
    _ROUND0_GIT_BASH is None, reason="no MSYS/MINGW Git Bash available"
)
class TestBashFixRound0TreeFallback:
    """``tree dirA dirB`` must list BOTH directories (the positional arm used
    to overwrite a scalar, silently dropping every directory but the last)."""

    def test_tree_lists_all_positional_directories(self, tmp_path) -> None:
        first = tmp_path / "AAA_first_dir"
        second = tmp_path / "BBB_second_dir"
        first.mkdir()
        second.mkdir()
        (first / "infile.txt").write_text("x")
        (second / "other.txt").write_text("y")

        body = _shell_compat._FALLBACK_BODIES["tree"]
        script = (
            f"tree() {{ {body}; }}\n"
            f"tree {subprocess.list2cmdline([first.as_posix(), second.as_posix()])}\n"
        )
        proc = subprocess.run(
            [_ROUND0_GIT_BASH, "-c", script],
            capture_output=True,
            timeout=30,
            env=_round0_bash_env(),
        )
        out = proc.stdout.decode("utf-8", "replace")

        assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
        assert "AAA_first_dir" in out, f"first directory dropped from output:\n{out}"
        assert "infile.txt" in out, f"first directory's contents dropped:\n{out}"
        assert "BBB_second_dir" in out, f"second directory dropped from output:\n{out}"


@pytest.mark.skipif(
    _ROUND0_GIT_BASH is None, reason="no MSYS/MINGW Git Bash available"
)
class TestBashFixRound0WatchInterval:
    """``watch --interval N`` / ``--interval=N`` must be accepted: the
    scanner's own option table lists ``--interval`` as ``-n``'s long form,
    but the fallback body rejected it."""

    def test_watch_table_advertises_interval_option(self) -> None:
        assert "--interval" in _shell_compat._WRAPPER_OPTIONS_WITH_VALUE["watch"]

    def _run_watch(self, args: list[str]) -> subprocess.CompletedProcess | None:
        body = _shell_compat._FALLBACK_BODIES["watch"]
        script = f"watch() {{ {body}; }}\nwatch {subprocess.list2cmdline(args)}\n"
        try:
            return subprocess.run(
                [_ROUND0_GIT_BASH, "-c", script],
                capture_output=True,
                timeout=4,
                env=_round0_bash_env(),
            )
        except subprocess.TimeoutExpired:
            # Timed out inside the repeat loop -> the option was accepted.
            return None

    def test_watch_interval_is_not_rejected(self) -> None:
        result = self._run_watch(["--interval", "0.05", "true"])
        if result is None:
            return
        err = result.stderr.decode("utf-8", "replace")
        assert "unsupported option" not in err, (
            f"watch fallback rejects the documented --interval option: {err!r}"
        )

    def test_watch_interval_attached_is_not_rejected(self) -> None:
        result = self._run_watch(["--interval=0.05", "true"])
        if result is None:
            return
        err = result.stderr.decode("utf-8", "replace")
        assert "unsupported option" not in err, (
            f"watch fallback rejects --interval=N: {err!r}"
        )


class TestBashFixRound0InlineScriptHarvest:
    """Inline-script operands that hold runtime expansions cannot be
    re-emitted, but their statically visible command words (and the commands
    inside their ``$(...)``/backtick substitutions, which run in the current
    shell) must still register fallback definitions for the child shell."""

    def test_single_quote_is_control(self) -> None:
        r = fix_bash_command("bash -c 'rev $HOME'")
        assert r.replacements == ("rev",)
        assert r.command.split("\n")[-1] == "bash -c 'rev $HOME'"

    @pytest.mark.parametrize(
        "cmd, expected_names",
        [
            ('bash -c "rev $HOME"', ("rev",)),
            ('watch "rev <<< $x"', ("watch", "rev")),
            ('eval "rev <<< $x"', ("rev",)),
            ('trap "rev <<< $x" EXIT', ("rev",)),
        ],
    )
    def test_dq_inline_script_registers_fallback(
        self, cmd: str, expected_names: tuple[str, ...]
    ) -> None:
        r = fix_bash_command(cmd)
        assert r.replacements == expected_names
        # The operand text needs no source edit -> command tail is
        # byte-for-byte unchanged (the fix is the definition + export).
        assert r.command.split("\n")[-1] == cmd
        assert "if declare -F rev >/dev/null; then export -f rev; fi" in r.command

    def test_dq_inline_registers_even_under_path_ambiguity(self) -> None:
        r = fix_bash_command(r'eval "cd C:\x && rev <<< $y"')
        assert "rev" in r.replacements
        assert r.command.split("\n")[-1] == r'eval "cd C:\x && rev <<< $y"'

    def test_single_quote_control_substitution(self) -> None:
        r = fix_bash_command("bash -c 'uptime'")
        assert r.replacements == ("uptime",)

    def test_eval_path_is_consistent(self) -> None:
        r = fix_bash_command('eval "$(rev)"')
        assert "rev" in r.replacements

    @pytest.mark.parametrize(
        "cmd, expected_names",
        [
            ('bash -c "$(uptime)"', ("uptime",)),
            ('sh -c "$(rev)"', ("rev",)),
            ('bash -lc "$(uptime)"', ("uptime",)),
            ('bash -c "`uptime`"', ("uptime",)),
            ('nohup bash -c "$(rev)"', ("rev",)),
            ('timeout 5 bash -c "$(uptime)"', ("uptime",)),
            ('bash -c "rev $(uptime)"', ("uptime",)),
        ],
    )
    def test_wrapper_scans_substitutions_in_nonliteral_operand(
        self, cmd: str, expected_names: tuple[str, ...]
    ) -> None:
        r = fix_bash_command(cmd)
        for name in expected_names:
            assert name in r.replacements, f"{name!r} missing from {r.replacements!r}"
        # The operand text cannot be re-emitted (it holds expansions), so the
        # command tail stays byte-for-byte; only the definitions differ.
        assert r.command.split("\n")[-1] == cmd
        for name in expected_names:
            assert f"declare -F {name}" in r.command


@pytest.mark.skipif(
    _ROUND0_GIT_BASH is None, reason="no MSYS/MINGW Git Bash available"
)
class TestBashFixRound0HeredocContinuation:
    """The heredoc trailing-operator move must carry a backslash
    line-continuation (and its continuation lines) with the moved command;
    splitting the logical line corrupts the heredoc body and orphans the
    continuation tail."""

    _INPUT = (
        "cat <<EOF\n"
        "bodyline\n"
        "EOF\n"
        "&& printf 'tail: %s\\n' \\\n"
        "  ARG\n"
    )
    _EXPECTED_STDOUT = b"bodyline\ntail: ARG\n"

    @staticmethod
    def _run_script(script: str) -> subprocess.CompletedProcess:
        import tempfile

        path = os.path.join(tempfile.gettempdir(), "round0_heredoc_move.sh")
        with open(path, "w", newline="") as f:
            f.write(script)
        return subprocess.run(
            [_ROUND0_GIT_BASH, path],
            capture_output=True,
            env=_round0_bash_env(),
        )

    @staticmethod
    def _bash_n(script: str) -> int:
        import tempfile

        path = os.path.join(tempfile.gettempdir(), "round0_heredoc_move_n.sh")
        with open(path, "w", newline="") as f:
            f.write(script)
        return subprocess.run(
            [_ROUND0_GIT_BASH, "-n", path],
            capture_output=True,
            env=_round0_bash_env(),
        ).returncode

    def test_heredoc_operator_move_preserves_line_continuation(self) -> None:
        fixed = fix_bash_command(self._INPUT).command

        # The input itself is a bash syntax error (the pattern being repaired).
        assert self._bash_n(self._INPUT) != 0

        result = self._run_script(fixed)
        assert result.returncode == 0, (
            f"fixed command failed to run:\n{fixed!r}\nstderr={result.stderr!r}"
        )
        assert result.stdout.replace(b"\r\n", b"\n") == self._EXPECTED_STDOUT, (
            f"heredoc body / continuation corrupted by operator move.\n"
            f"fixed={fixed!r}\nstdout={result.stdout!r}"
        )

    def test_heredoc_operator_move_simple_form_still_ok(self) -> None:
        # No line continuation: the plain repair must keep working (guard
        # against a fix that over-corrects and breaks the simple case).
        simple = "cat <<EOF\nbodyline\nEOF\n&& echo hi\n"
        fixed = fix_bash_command(simple).command
        assert self._bash_n(fixed) == 0
        result = self._run_script(fixed)
        assert result.returncode == 0
        assert result.stdout.replace(b"\r\n", b"\n") == b"bodyline\nhi\n"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Git Bash shim only")
class TestBashFixRound0NestedParenDescent:
    """``_process_unquoted`` must convert a Windows path inside ``$(...)``
    even when the substitution contains a nested plain ``(`` subshell group:
    the paren matcher must balance plain parens, not only ``$(`` opens."""

    def test_nested_subshell_descend_converts_path(self) -> None:
        cmd = r'echo "$( (true) ; cat C:\path\to )"'
        converted = _shell_compat._process_unquoted(cmd)
        assert "C:/path/to" in converted, (
            "_process_unquoted failed to convert the Windows path inside the "
            f"command substitution that contains a nested subshell group.\n"
            f"  input : {cmd!r}\n  output: {converted!r}"
        )

    def test_control_plain_substitution_still_converts(self) -> None:
        cmd = r'echo "$(true ; cat C:\path\to )"'
        converted = _shell_compat._process_unquoted(cmd)
        assert "C:/path/to" in converted, f"control regression: {converted!r}"

    @pytest.mark.skipif(
        _ROUND0_GIT_BASH is None, reason="no MSYS/MINGW Git Bash available"
    )
    def test_unconverted_path_is_mangled_by_real_bash(self) -> None:
        # Prove the conversion is required: Git Bash eats the backslashes.
        buggy = r'echo "$( (true) ; printf %s C:\path\to )"'
        r = subprocess.run(
            [_ROUND0_GIT_BASH, "-c", buggy],
            capture_output=True,
            env=_round0_bash_env(),
        )
        out = r.stdout.replace(b"\r\n", b"\n")
        assert b"C:pathto" in out, (
            f"expected bash to mangle the un-quoted backslash path; got {out!r}"
        )


# ---------------------------------------------------------------------------
# Round-1 review regressions for ``_shell_compat.fix_bash_command`` and the
# PowerShell transform subsystem (``pwsh_transform`` / ``fix_pwsh_command``).
# Each class below guards one bug confirmed during the round-1 review with a
# failing test before the fix; see the class docstrings for the details.
# ---------------------------------------------------------------------------


def _round1_which(*names: str) -> str | None:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


_ROUND1_PS7 = _round1_which("pwsh", "pwsh.exe")
_ROUND1_PS51 = _round1_which("powershell", "powershell.exe")


def _round1_run_pwsh(script: str) -> tuple[str, str, int]:
    p = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=60,
        encoding="utf-8",
        errors="replace",
    )
    return p.stdout, p.stderr, p.returncode


def _round1_run_ps(exe: str, script: str) -> tuple[str, str, int]:
    p = subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        timeout=90,
        encoding="utf-8",
        errors="replace",
    )
    return p.stdout, p.stderr, p.returncode


class TestBashFixRound1PgrepDashDash:
    """``pgrep -- PATTERN`` must select the PATTERN, not report it missing.

    The ``pgrep``/``pkill`` fallback bodies parse options with a
    ``while ... case`` loop whose ``--) shift; break;;`` arm terminates option
    scanning.  Before the fix the bodies only ever read their operand from the
    loop's ``*)`` arm, so a pattern placed after ``--`` never reached
    ``__kimix_pat`` and the fallback aborted with "missing pattern".  GNU
    semantics: ``pgrep -- PATTERN`` is exactly ``pgrep PATTERN``.
    """

    pytestmark = pytest.mark.skipif(
        _ROUND0_GIT_BASH is None, reason="requires a real Git Bash"
    )

    def test_dashdash_pattern_is_used(self):
        body = _shell_compat._FALLBACK_BODIES["pgrep"]
        # svchost.exe exists on every Windows host; the test only runs on Git
        # Bash, which implies Windows or MSYS.
        proc = _round0_run_fallback(body, "pgrep", ["--", "svchost"], timeout=40)
        err = proc.stderr.decode("utf-8", "replace")
        assert "missing pattern" not in err, err
        assert proc.returncode == 0, err

    def test_option_then_dashdash_pattern_is_used(self):
        body = _shell_compat._FALLBACK_BODIES["pgrep"]
        proc = _round0_run_fallback(body, "pgrep", ["-l", "--", "svchost"], timeout=40)
        err = proc.stderr.decode("utf-8", "replace")
        assert "missing pattern" not in err, err
        assert proc.returncode == 0, err


class TestBashFixRound1PkillDashDash:
    """``pkill -- PATTERN`` must forward the PATTERN to Stop-Process."""

    pytestmark = pytest.mark.skipif(
        _ROUND0_GIT_BASH is None, reason="requires a real Git Bash"
    )

    def test_dashdash_pattern_reaches_powershell(self):
        body = _shell_compat._FALLBACK_BODIES["pkill"]
        # A name that matches nothing: the *correct* result is a clean
        # exit 1 from the PowerShell fallback (no match), NOT the
        # "missing pattern" abort that proves the operand was dropped.
        proc = _round0_run_fallback(
            body, "pkill", ["--", "zzz_kimix_no_such_proc_xyz"], timeout=40
        )
        err = proc.stderr.decode("utf-8", "replace")
        assert "missing pattern" not in err, (proc.returncode, err)


_ROUND1_NOTEPAD = None
for _cand in (r"C:\Windows\System32\notepad.exe", r"C:\Windows\notepad.exe"):
    if os.path.exists(_cand):
        _ROUND1_NOTEPAD = _cand
        break
del _cand


def _round1_is_running(pid) -> bool:
    if pid is None:
        return False
    r = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue | Measure-Object).Count",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return r.stdout.strip().startswith("1")


class TestBashFixRound1TaskkillImExe:
    """``taskkill /IM <name>.exe`` must actually match/kill.

    ``_TASKKILL_PS`` selected victims with
    ``Get-Process | Where-Object { $_.Name -eq $env:__KIMIX_IM }``, but
    ``taskkill``'s documented ``/IM`` operand includes the extension
    (``notepad.exe``) while ``Get-Process .Name`` strips it (``notepad``), and
    ``-eq`` is case-sensitive where taskkill matching is not.  The canonical
    invocation therefore never matched.  The fix normalises with
    ``[IO.Path]::GetFileNameWithoutExtension`` (quotes cannot be used inside
    the surrounding single-quoted bash ``-Command`` string).
    """

    pytestmark = pytest.mark.skipif(
        _ROUND0_GIT_BASH is None or _ROUND1_NOTEPAD is None,
        reason="requires Git Bash and notepad.exe",
    )

    def _launch(self):
        proc = subprocess.Popen(
            [_ROUND1_NOTEPAD], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        deadline = time.time() + 5
        while time.time() < deadline:
            if _round1_is_running(proc.pid):
                return proc
            time.sleep(0.2)
        return proc

    def _cleanup(self, pid) -> None:
        if _round1_is_running(pid):
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True,
                text=True,
            )

    def test_im_with_exe_suffix_kills(self):
        body = _shell_compat._FALLBACK_BODIES["taskkill"]
        proc = self._launch()
        try:
            assert _round1_is_running(proc.pid)
            res = _round0_run_fallback(
                body, "taskkill", ["/IM", "notepad.exe", "/F"], timeout=40
            )
            err = res.stderr.decode("utf-8", "replace")
            # The canonical taskkill invocation must succeed (exit 0).
            assert res.returncode == 0, (res.returncode, err)
            time.sleep(0.5)
            assert not _round1_is_running(proc.pid), (
                "taskkill /IM notepad.exe reported success but the process "
                f"(pid {proc.pid}) is still running"
            )
        finally:
            self._cleanup(proc.pid)

    def test_im_case_insensitive_no_ext(self):
        body = _shell_compat._FALLBACK_BODIES["taskkill"]
        proc = self._launch()
        try:
            assert _round1_is_running(proc.pid)
            res = _round0_run_fallback(
                body, "taskkill", ["/IM", "Notepad", "/F"], timeout=40
            )
            err = res.stderr.decode("utf-8", "replace")
            assert res.returncode == 0, (res.returncode, err)
            time.sleep(0.5)
            assert not _round1_is_running(proc.pid), (
                f"taskkill /IM Notepad (mixed case) left pid {proc.pid} running"
            )
        finally:
            self._cleanup(proc.pid)


class TestBashFixRound1ProcessUnquotedDqAnsiC:
    """Inside double quotes ``$'`` is *literal* in bash (ANSI-C quoting does
    not apply inside ``"..."``), yet ``$(...)`` substitution *is* active
    there.  ``_process_unquoted``'s double-quote walker used to treat ``$'``
    as an ANSI-C region and skip it verbatim, swallowing a genuine ``$(...)``
    between the ``$'`` and its matching ``'`` — its unquoted backslashes were
    never rewritten to ``/``.  Top-level ``$'...'`` remains a real ANSI-C
    region whose escapes must NOT be converted (last test)."""

    @pytest.mark.parametrize(
        "command, expected",
        [
            # $(...) embedded after a literal $' inside "..." must still be walked.
            ('"a$\'b$(c\\d)e\'f"', '"a$\'b$(c/d)e\'f"'),
            ('"p$\'q$(echo a\\b)\'r"', '"p$\'q$(echo a/b)\'r"'),
            # The backslash must convert even when the closing ' is the last char.
            ('"x$\'$(echo p\\q)\'"', '"x$\'$(echo p/q)\'"'),
        ],
    )
    def test_dq_literal_ansi_c_descends_into_substitution(self, command, expected):
        assert _shell_compat._process_unquoted(command) == expected

    def test_top_level_ansi_c_still_preserved(self):
        # At the *top level* $'...' IS a real ANSI-C region: its backslashes
        # are escapes and must NOT be converted to slashes.
        assert (
            _shell_compat._process_unquoted(r"printf %s $'a\tb'")
            == r"printf %s $'a\tb'"
        )

    def test_dq_backtick_after_literal_ansi_c_descends(self):
        # A backtick substitution after a literal $' inside "..." must also
        # be walked (same root cause: the DQ walker must not treat $' as a
        # region).
        cmd = "\"a$'b`c\\d`e'f\""
        assert _shell_compat._process_unquoted(cmd) == "\"a$'b`c/d`e'f\""


class TestBashFixRound1CoalesceExpression:
    """Null-coalescing (``??``) must yield a *value* expression ``$(if ...)``
    so it stays valid inside parentheses, as a command argument, or before a
    pipeline element — not a bare ``if`` statement wrapped in ``(...)`` which
    is a PowerShell parse error.  The ``_transform_operator`` builder now
    wraps the emitted statement unconditionally (semantically identical at
    statement position)."""

    def test_in_parens_is_wrapped_in_subexpression(self) -> None:
        out = _shell_compat.pwsh_transform('Write-Output ($v ?? "def")')[0]
        # ``$(if (`` still contains the substring ``(if (``, so the invariant
        # to assert is the ``$(`` prefix.
        assert "$(if ($null -ne $v)" in out, out
        assert "??" not in out, out
        assert "if ($null -ne $v)" in out, out

    def test_command_argument_position_wrapped(self) -> None:
        out = _shell_compat.pwsh_transform('Write-Output $v ?? "def"')[0]
        assert "Write-Output $(" in out or "$(" in out, out
        assert "if ($null -ne $v)" in out, out

    def test_before_pipe_wrapped(self) -> None:
        out = _shell_compat.pwsh_transform("$x = 1; $y = 2; $x ?? $y | Write-Host")[0]
        assert "| Write-Host" in out, out
        # the if-expression must be inside a subexpression, not directly piped
        assert "else { $y })|" in out.replace(" ", "") or "$(" in out, out

    def test_inside_bracket_expression_wrapped(self) -> None:
        out = _shell_compat.pwsh_transform("foo ($a ?? $b)")[0]
        assert "$(if ($null -ne $a)" in out, out
        assert "if ($null -ne $a)" in out, out

    @pytest.mark.skipif(not _ROUND1_PS7, reason="pwsh not available")
    def test_semantics_preserved_in_parens(self) -> None:
        for src in [
            '$v = $null; Write-Output ($v ?? "def")',
            '$v = 7; Write-Output ($v ?? "def")',
            '$a = $null; $b = 2; Write-Host ($a ?? $b)',
        ]:
            out = _shell_compat.pwsh_transform(src)[0]
            o = _round1_run_pwsh(src)
            n = _round1_run_pwsh(out)
            assert n[2] == 0, (
                f"transformed code fails to parse: {src!r} -> {out!r}\n{n[1]}"
            )
            assert o[0] == n[0], (src, out, o[0], n[0])


class TestBashFixRound1CoalesceStatement:
    """The statement/assignment coalesce forms must keep working."""

    def test_assignment_form(self) -> None:
        out = _shell_compat.pwsh_transform('$x = $a ?? "default"')[0]
        assert "if ($null -ne $a)" in out
        assert "??" not in out

    @pytest.mark.skipif(not _ROUND1_PS7, reason="pwsh not available")
    def test_semantics_preserved_assignment(self) -> None:
        for src in [
            '$a = $null; $x = $a ?? "d"; Write-Host $x',
            '$a = 9; $x = $a ?? "d"; Write-Host $x',
            '$a = 1; $b = 2; $r = $a ?? $b; Write-Host $r',
        ]:
            out = _shell_compat.pwsh_transform(src)[0]
            o = _round1_run_pwsh(src)
            n = _round1_run_pwsh(out)
            assert n[2] == 0, (
                f"transformed code fails to parse: {src!r} -> {out!r}\n{n[1]}"
            )
            assert o[0] == n[0], (src, out, o[0], n[0])


class TestBashFixRound1PwshFuzz:
    """Robustness guard for the pwsh transform subsystem: the PS7->5.1
    transforms are text scanners that must never raise or loop forever on
    adversarial / malformed input (quoting soup, nested ``$( )``,
    here-strings, backticks, ``?:``/``??`` chains, ``#`` comments, member
    access)."""

    _FRAGS = [
        "?.", "?.Name", "?[", "]?[0]", "??", "??=", "?", ":", "::", "$a", "$b",
        "${x}", "$?", "$$", "$obj", "$obj.Name", "C:", "C:\\x", "$env:PATH",
        "'sq'", '"dq"', "#", "#c", "<#", "#>", "@'", '@"', "'@", '"@', "$(",
        "(", ")", "{", "}", "[", "]", "&&", "||", "|", ";", ",", "Write-Host",
        "if", "foreach", "=", "+", "-", " ", "\t", "\n", "`", "a#b", "f?.bak",
        "$c ? $a : $b", "$a ?? $b", "$obj?.Name", "$arr?[0]", "$x ??= 1", "$null",
        "..", "[Math]::PI", "?:", "???", "?..", "$()", "@(", "> ", "2>&1",
    ]

    @staticmethod
    def _call_with_watchdog(fn, arg, timeout=5.0):
        import threading
        import traceback

        box: dict = {}

        def target():
            try:
                box["result"] = fn(arg)
            except BaseException as exc:  # noqa: BLE001 - record everything
                box["exc"] = exc
                box["tb"] = traceback.format_exc()

        t = threading.Thread(target=target, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            raise AssertionError(f"{fn.__name__} hung on {arg!r}")
        if "exc" in box:
            raise AssertionError(f"{fn.__name__} raised on {arg!r}:\n{box['tb']}")
        return box.get("result")

    @classmethod
    def _corpus(cls) -> list[str]:
        import random

        rng = random.Random(20261013)
        corpus: list[str] = [
            "$x = @'\nhere ?? string ? : a#b\n'@\nWrite-Host ($a ?? $b)",
            "<# block ?? ?. comment #>\ncmd && cmd2",
            "$y = @\"\ninterp $(f?.x) # n\n\"@\n$y?.Length",
            "foreach ($i in 1..3) { $i ?? 0 }",
            "switch ($x) { { $_ ? 1 : 2 } { 'a' } default { $b?.c } }",
            "$o?.a?.b?.c ?? $d?.e ?? $f",
        ]
        bases = ["$a", "$obj.Name", "@{k=1}['k']", "C:\\tmp", "$(gcm)", "$?", "$$"]
        for b in bases:
            for op in ("??", " ? ", "&&", "||"):
                corpus.append(f"{b}{op} ($c ? $a : $b) # tail a#b")
                corpus.append(f"$x = ({b}{op}$y)")
                corpus.append(f"@({b}?.Name, ({b} ?? 0), {b}?[0])")
        for _ in range(1500):
            corpus.append("".join(rng.choice(cls._FRAGS) for _ in range(rng.randint(1, 40))))
        return corpus

    def test_fuzz_no_crash_or_hang(self) -> None:
        corpus = self._corpus()
        assert len(corpus) > 1500
        for code in corpus:
            self._call_with_watchdog(_shell_compat.pwsh_transform, code)
            self._call_with_watchdog(_shell_compat.fix_pwsh_command, code)


class TestBashFixRound1HashBareword:
    """``#`` inside a bareword is NOT a comment.

    PowerShell only starts a comment at a token boundary (``foo#c`` is a
    single bareword token).  ``_PwshScanner`` respected that via
    ``_at_token_start``, but ``_build_region_mask`` (used by ``pwsh_transform``)
    treated *every* ``#`` as a comment opener, masking the rest of the line —
    so any PS7 operator after a glued ``#`` (e.g. in ``file#1.txt``) survived
    into the emitted PS 5.1 command as a hard parse error.  The mask now
    requires a token boundary, mirroring ``_at_token_start``."""

    def test_region_mask_respects_token_boundary_for_hash(self) -> None:
        """A ``#`` glued to an identifier character is NOT a comment."""
        mask = _shell_compat._build_region_mask("foo#c")
        assert list(mask) == [1, 1, 1, 1, 1], f"expected no comment region, got {bytes(mask)!r}"
        mask = _shell_compat._build_region_mask("file#1.txt ?? $x")
        # The whole line is code: '#' is inside 'file#1.txt', not a comment.
        assert all(mask), f"mask should be all-code, got {bytes(mask)!r}"
        # A ' #' preceded by whitespace *is* a comment.
        line2 = "$a ?? $b # real comment"
        mask = _shell_compat._build_region_mask(line2)
        hash_i = line2.index("#")
        assert mask[hash_i] == 0

    def test_glued_hash_does_not_swallow_coalescing_on_same_line(self) -> None:
        src = "Write-Output file#1.txt ($a ?? $b)"
        out = _shell_compat.pwsh_transform(src)[0]
        assert "??" not in out, out
        assert "if ($null -ne $a)" in out, out

    def test_glued_hash_does_not_swallow_ternary_on_same_line(self) -> None:
        src = "Write-Output a#b ($c ? $x : $y)"
        out = _shell_compat.pwsh_transform(src)[0]
        assert "$c ?" not in out, out
        assert "$c?" not in out, out
        assert "if ($c)" in out, out

    def test_glued_hash_does_not_swallow_null_conditional_on_same_line(self) -> None:
        src = "Write-Output a#b ($obj?.Name)"
        out = _shell_compat.pwsh_transform(src)[0]
        assert "?." not in out, out
        assert "if ($null -ne $obj)" in out, out

    @pytest.mark.skipif(not (_ROUND1_PS7 and _ROUND1_PS51), reason="both PowerShell versions required")
    def test_semantics_glued_hash_coalescing(self) -> None:
        """Cross-check: transformed output runs under PS 5.1 and equals PS 7 output."""
        src = "$a = $null; $b = 'B'; Write-Host ($a ?? $b) file#1.txt"
        out = _shell_compat.pwsh_transform(src)[0]
        o_out, _, o_rc = _round1_run_ps(_ROUND1_PS7, src)
        n_out, n_err, n_rc = _round1_run_ps(_ROUND1_PS51, out)
        assert o_rc == 0, (src,)
        assert n_rc == 0, (out, n_err)
        assert o_out == n_out, (src, out, o_out, n_out)


# A statement prologue that defines $c (truthy), $a/$b so every snippet below
# yields a deterministic value.
_ROUND1_TERNARY_SETUP = "$c = [bool]1; $a = 'A'; $b = 'B'; $d = 'D'"

# (source, expected value) — ternaries nested at paren depth > 0.
_ROUND1_TERNARY_CASES = [
    ("($c ? $a : $b)", "A"),
    ("$x = ($c ? $a : $b); $x", "A"),
    ("Write-Output ($c ? $a : $b)", "A"),
    ("$z = @($a, ($c ? $a : $b)); ($z -join ',')", "A,A"),
    ("(($c ? $a : $b).Length)", "1"),
    ("if ($c ? $a : $b) { 'ran' }", "ran"),
]


class TestBashFixRound1TernaryParen:
    """A ternary (``? :``) nested inside parentheses must be transformed.

    PowerShell 7 parses ``($c ? $a : $b)`` (and paren-argument /
    array-element / ``if``-condition forms) as a normal ternary expression;
    ``pwsh_transform`` must downgrade these to PS 5.1 syntax that itself
    parses under Windows PowerShell 5.1.  Before the fix the scanner only
    looked for the ternary separator colon at absolute bracket depth 0, so a
    ternary wrapped in ``(...)`` was left completely untouched.  Colons
    inside ``{ ... }`` braces remain intentionally *not* transformed
    (hashtable / switch / script-block syntax), which
    ``test_brace_ternary_still_skipped`` guards."""

    @pytest.mark.parametrize("src", [c[0] for c in _ROUND1_TERNARY_CASES])
    def test_paren_ternary_no_longer_emits_invalid_source(self, src: str) -> None:
        out = _shell_compat.pwsh_transform(src)[0]
        assert "? $a" not in out and "?$a" not in out, out
        assert "$c ?" not in out, out
        assert "$c?" not in out, out
        # the 'if' must be lifted into a sub-expression so it is valid in an
        # expression/paren position
        assert "$(if" in out, out
        assert "($null" not in out, out  # ternary, not coalescing

    def test_brace_ternary_still_skipped(self) -> None:
        """A ternary inside ``@{ ... }`` braces remains untouched (documented)."""
        src = '$x = @{ key = $a ? "t" : "f" }'
        out = _shell_compat.pwsh_transform(src)[0]
        assert "?" in out  # deliberately not transformed at brace depth > 0

    @pytest.mark.skipif(not _ROUND1_PS51, reason="Windows PowerShell 5.1 not available")
    @pytest.mark.parametrize("src,expected", _ROUND1_TERNARY_CASES)
    def test_transformed_parses_under_ps51(self, src: str, expected: str) -> None:
        out = _shell_compat.pwsh_transform(src)[0]
        _, err, rc = _round1_run_ps(
            _ROUND1_PS51, f"{_ROUND1_TERNARY_SETUP}; {out}"
        )
        assert rc == 0, (
            f"transformed output fails to run under PS 5.1: {src!r} -> {out!r}\n{err}"
        )

    @pytest.mark.skipif(
        not (_ROUND1_PS7 and _ROUND1_PS51),
        reason="both PowerShell versions required",
    )
    @pytest.mark.parametrize("src,expected", _ROUND1_TERNARY_CASES)
    def test_semantics_preserved_original_vs_transformed(self, src: str, expected: str) -> None:
        out = _shell_compat.pwsh_transform(src)[0]
        o_out, o_err, o_rc = _round1_run_ps(_ROUND1_PS7, f"{_ROUND1_TERNARY_SETUP}; {src}")
        n_out, n_err, n_rc = _round1_run_ps(_ROUND1_PS51, f"{_ROUND1_TERNARY_SETUP}; {out}")
        assert o_rc == 0, f"original must run under PS7: {src!r}\n{o_err}"
        assert n_rc == 0, f"transformed must run under PS5.1: {out!r}\n{n_err}"
        assert o_out.strip() == n_out.strip() == expected, (
            src,
            out,
            "PS7 original:",
            o_out.strip(),
            "PS5.1 transformed:",
            n_out.strip(),
        )


class TestBashFixRound1HeredocChain:
    """Heredoc trailing-operator move must carry a CHAIN of operator lines.

    When the model writes a list across several physical lines — terminator
    line followed by ``&& echo A`` and then, on the line after it,
    ``&& echo B`` — every such line must move onto the redirection line(s);
    leaving ``&& echo B`` behind re-creates the exact
    ``syntax error near unexpected token `&&'`` the repair exists to remove."""

    pytestmark = pytest.mark.skipif(
        _ROUND0_GIT_BASH is None, reason="no MSYS/MINGW Git Bash available"
    )

    INPUT = "cat <<EOF\nbody\nEOF\n&& echo A\n&& echo B\n"
    EXPECTED_STDOUT = b"body\nA\nB\n"

    @staticmethod
    def _write_and_bash(script: str, args: list[str]) -> subprocess.CompletedProcess:
        import tempfile

        path = os.path.join(tempfile.gettempdir(), "round1_h_heredoc_chain.sh")
        with open(path, "w", newline="") as f:
            f.write(script)
        return subprocess.run(
            [_ROUND0_GIT_BASH, *args, path],
            capture_output=True,
            env=_round0_bash_env(),
        )

    def test_chained_operator_lines_after_terminator_are_moved(self) -> None:
        fixed = fix_bash_command(self.INPUT).command
        assert self._write_and_bash(fixed, ["-n"]).returncode == 0, (
            f"the second '&&' line after the heredoc terminator was left "
            f"orphaned -> the repaired command is still a syntax error.\n"
            f"fixed={fixed!r}"
        )
        result = self._write_and_bash(fixed, [])
        assert result.returncode == 0, (
            f"repaired chained command failed: {result.stderr!r}\nfixed={fixed!r}"
        )
        assert result.stdout.replace(b"\r\n", b"\n") == self.EXPECTED_STDOUT, (
            f"fixed={fixed!r} stdout={result.stdout!r}"
        )


class TestBashFixRound1HeredocCrlf:
    """Heredoc trailing-operator move must handle CRLF line endings.

    With CRLF input the redirection line ends ``...EOF\\r\\n``; the move must
    still trigger so the trailing ``&&`` is not left orphaned after the
    terminator (a command whose source uses CRLF — which the Bash tool can
    receive verbatim — was previously NOT repaired)."""

    pytestmark = pytest.mark.skipif(
        _ROUND0_GIT_BASH is None, reason="no MSYS/MINGW Git Bash available"
    )

    INPUT = "cat <<EOF\r\nbody\r\nEOF\r\n&& echo tail\r\n"
    EXPECTED_STDOUT = b"body\ntail\n"

    @staticmethod
    def _write_and_bash(script: str, args: list[str]) -> subprocess.CompletedProcess:
        import tempfile

        path = os.path.join(tempfile.gettempdir(), "round1_h_heredoc_crlf.sh")
        with open(path, "w", newline="") as f:
            f.write(script)
        return subprocess.run(
            [_ROUND0_GIT_BASH, *args, path],
            capture_output=True,
            env=_round0_bash_env(),
        )

    def test_crlf_heredoc_operator_is_moved(self) -> None:
        fixed = fix_bash_command(self.INPUT).command
        assert self._write_and_bash(fixed, ["-n"]).returncode == 0, (
            f"fix_bash_command left the CRLF heredoc operator orphaned; the "
            f"repaired command is still a bash syntax error.\nfixed={fixed!r}"
        )
        result = self._write_and_bash(fixed, [])
        assert result.returncode == 0, (
            f"repaired CRLF command failed: {result.stderr!r}\nfixed={fixed!r}"
        )
        assert result.stdout.replace(b"\r\n", b"\n") == self.EXPECTED_STDOUT, (
            f"fixed={fixed!r} stdout={result.stdout!r}"
        )


class TestBashFixRound1MissingOptionMsg:
    """Missing-option-value messages must be getopt-style: the option name
    WITHOUT its ``-``/``--``/``/`` delimiter.

    The round-0 guards printed the full option token (``$1``) for the wget,
    xclip, taskkill and watch arms — e.g. ``-- '-n'`` — while the nc,
    traceroute, tree, column and top arms printed the bare letter.  getopt /
    GNU coreutils never include the delimiter.  All arms now strip it via
    ``${1//[![:alnum:]]/}``."""

    pytestmark = pytest.mark.skipif(
        _ROUND0_GIT_BASH is None, reason="requires a real Git Bash"
    )

    @pytest.mark.parametrize(
        "name, argv",
        [
            ("watch", ["-n"]),
            ("watch", ["--interval"]),
            ("wget", ["-O"]),
            ("wget", ["-T"]),
            ("xclip", ["-selection"]),
            ("xclip", ["-d"]),
            ("taskkill", ["/IM"]),
            ("taskkill", ["/PID"]),
        ],
    )
    def test_missing_option_argument_message_is_getopt_style(self, name, argv):
        import re

        body = _shell_compat._FALLBACK_BODIES[name]
        result = _round0_run_fallback(body, name, argv)
        err = result.stderr.decode("utf-8", "replace")
        assert result.returncode != 0, err
        assert "option requires an argument" in err, (
            f"{name} {argv} did not emit the expected missing-argument error: {err!r}"
        )
        # The message must report the option WITHOUT its delimiter.
        m = re.search(r"option requires an argument --\s+['\"]?([^\s'\"]+)", err)
        assert m is not None, f"could not parse option report from {err!r}"
        reported = m.group(1)
        assert not reported.startswith(("-", "/")), (
            f"{name} {argv}: missing-argument message embeds the option "
            f"delimiter (-- {reported!r}); getopt reports the option "
            f"name/letter only.  Sibling fallbacks report the bare letter."
        )


class TestBashFixRound1TreeDashDash:
    """``tree -- dirA dirB`` must list dirA and dirB, not fall back to ``.``

    The ``tree`` fallback accumulates positional directories into
    ``__kimix_dirs``, but its ``--) shift; break;;`` arm exited the loop
    WITHOUT appending the remaining ``$@`` words, and the empty array then
    defaulted to ``(.)`` — silently dropping every directory that followed
    ``--``.  The arm now appends ``"$@"`` before breaking."""

    pytestmark = pytest.mark.skipif(
        _ROUND0_GIT_BASH is None, reason="requires a real Git Bash"
    )

    def test_tree_dashdash_before_directories_lists_them(self, tmp_path) -> None:
        first = tmp_path / "AAA_first_dir"
        second = tmp_path / "BBB_second_dir"
        first.mkdir()
        second.mkdir()
        (first / "alpha.txt").write_text("x")
        (second / "beta.txt").write_text("y")
        # current directory gets its own marker file to detect '.' fallback.
        cwd = tmp_path / "CCC_cwd"
        cwd.mkdir()
        (cwd / "zzz_cwd_marker.txt").write_text("z")

        body = _shell_compat._FALLBACK_BODIES["tree"]
        script = (
            f"tree() {{ {body}; }}\n"
            f"tree -- {subprocess.list2cmdline([first.as_posix(), second.as_posix()])}\n"
        )
        proc = subprocess.run(
            [_ROUND0_GIT_BASH, "-c", script],
            capture_output=True,
            timeout=30,
            env=_round0_bash_env(),
            cwd=str(cwd),
        )
        out = proc.stdout.decode("utf-8", "replace")

        assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
        assert "AAA_first_dir" in out, f"tree -- dropped the first directory: {out!r}"
        assert "BBB_second_dir" in out, f"tree -- dropped the second directory: {out!r}"
        assert "zzz_cwd_marker.txt" not in out, (
            f"tree -- fell back to listing the current directory ('.') instead "
            f"of the directories after the '--' end-of-options marker: {out!r}"
        )


# ===========================================================================
# Round-2 regression tests (taskkill exit codes; pwsh interpolated/nested
# transforms).  Bugs confirmed by tests that failed on pristine HEAD and
# pass with the Round-2 fixes in bin/kimix_native/_shell_compat.py.
# ===========================================================================

import tempfile

_ROUND2_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROUND2_SHIM_PATH = os.path.join(_ROUND2_REPO_ROOT, "bin", "kimix_native", "_shell_compat.py")


def _round2_load_shim():
    import importlib.util

    spec = importlib.util.spec_from_file_location("_shell_compat_r2", _ROUND2_SHIM_PATH)
    module = importlib.util.module_from_spec(spec)
    # Required so @dataclass can resolve the module's namespace during exec.
    sys.modules["_shell_compat_r2"] = module
    spec.loader.exec_module(module)
    return module


_ROUND2_SHIM = _round2_load_shim()


def _round2_assert_in_repo_copy():
    assert os.path.normcase(os.path.abspath(_ROUND2_SHIM.__file__)) == os.path.normcase(
        os.path.abspath(_ROUND2_SHIM_PATH)
    ), f"loaded shim is not the in-repo copy: {_ROUND2_SHIM.__file__}"


def _round2_transform(code: str) -> str:
    out, _warnings = _ROUND2_SHIM.pwsh_transform(code)
    return out


def _round2_unq(code: str) -> str:
    return _ROUND2_SHIM._process_unquoted(code)


def _round2_run_ps(exe, script, timeout=12):
    p = subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=timeout,
    )
    return p.returncode, p.stdout.strip(), p.stderr.strip()


_ROUND2_PWSH7 = shutil.which("pwsh")
_ROUND2_POWERSHELL51 = (
    os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                 "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
)


def _round2_ps51_parse_errors(script):
    """Return the PS5.1 *parser* errors for *script* (no execution)."""
    fd, path = tempfile.mkstemp(suffix=".ps1")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as f:
        f.write(script)
    cmd = (
        "$e=$null;"
        " [void][System.Management.Automation.Language.Parser]"
        "::ParseFile('%s',[ref]$null,[ref]$e);"
        " $e | ForEach-Object { $_.Message }" % path
    )
    try:
        p = subprocess.run(
            [_ROUND2_POWERSHELL51, "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, text=True, timeout=15,
        )
        return [ln for ln in p.stdout.splitlines() if ln.strip()]
    finally:
        os.unlink(path)


def _round2_ps7_runs_ok(script):
    if _ROUND2_PWSH7 is None:
        return True  # can't disprove validity -> assume the snippet is valid PS7
    rc, _out, err = _round2_run_ps(_ROUND2_PWSH7, script)
    return rc == 0 and "ParserError" not in err


_ROUND2_GIT_BASH_CANDIDATES = [
    r"C:\Program Files\Git\usr\bin\bash.exe",
    r"C:\Program Files (x86)\Git\usr\bin\bash.exe",
]


def _round2_git_bash() -> str:
    for c in _ROUND2_GIT_BASH_CANDIDATES:
        if os.path.exists(c):
            return c
    pytest.skip("Git Bash not found at a known location")


def _round2_shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _round2_run_fallback(name: str, args, bash: str, timeout=60):
    """Invoke the fallback logic directly from its RAW body.

    ``_fallback_definition`` wraps the body with a ``command -v``/native
    delegate guard.  On a host where the native tool exists (``taskkill.exe``
    is always on a Windows PATH) that guard short-circuits to the real
    binary, so the fallback body never runs.  To exercise the fallback
    deterministically we define the function from the body alone, bypassing
    the delegate.
    """
    body = _ROUND2_SHIM._FALLBACK_BODIES[name]
    argv = " ".join(_round2_shell_quote(a) for a in args)
    script = "{name}() {{ {body}; }}\n{name} {argv}\n".format(name=name, body=body, argv=argv)
    fd, path = tempfile.mkstemp(suffix=".sh")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(script)
        proc = subprocess.run([bash, path], capture_output=True, text=True, timeout=timeout)
    finally:
        os.remove(path)
    return proc.returncode, proc.stdout, proc.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only fallbacks")
class TestBashFixRound2Taskkill:
    """taskkill fallback must report failures like taskkill.exe (exit 0 was
    unconditional after the /PID branch, and /IM was silently dropped when
    combined with /PID)."""

    def test_nonexistent_pid_reports_failure(self):
        """``taskkill /PID <nonexistent>`` must exit non-zero (rc=128 on real
        taskkill.exe); the fallback body used to ``exit 0`` unconditionally."""
        _round2_assert_in_repo_copy()
        bash = _round2_git_bash()
        rc, out, err = _round2_run_fallback("taskkill", ["/PID", "999999"], bash)
        assert rc != 0, (
            "taskkill fallback reported success for a non-existent PID "
            f"(rc={rc}, stdout={out!r}, stderr={err!r})"
        )

    def test_combined_pid_im_does_not_swallow_im(self):
        """``taskkill /IM <x> /PID <nonexistent>`` must surface the failure.

        Real taskkill processes both operands; the fallback took the /PID
        branch first, ignored /IM entirely, and reported success despite
        the failed PID kill and the never-evaluated image match.
        """
        _round2_assert_in_repo_copy()
        bash = _round2_git_bash()
        rc, out, err = _round2_run_fallback("taskkill", ["/IM", "zzqqnope.exe", "/PID", "999999"], bash)
        assert rc != 0, (
            "taskkill fallback reported success for a combined /IM+/PID invocation "
            f"where neither operand could succeed (rc={rc}, stderr={err!r})"
        )


class TestBashFixRound2Pwsh:
    """PS7->PS5.1 transform bugs: operators inside ``"$(...)"`` string
    interpolations, ``return``/``throw`` before ``??``, nested ternaries,
    comma-armed ternary false branches, unbalanced ``$'`` in bash DQ, and
    here-string closing lines carrying live code."""

    def test_null_coalescing_inside_interpolated_subexpr(self):
        orig = "$a = $null; \"$($a ?? 'd')\""
        assert _round2_ps7_runs_ok(orig), "precondition: original must be valid PS7"
        out = _round2_transform(orig)
        assert "??" not in out, f"transform left '??' in: {out!r}"
        assert _round2_ps51_parse_errors(out) == [], f"5.1 parse error on transform: {out!r}"

    @pytest.mark.parametrize("kw", ["return", "throw"])
    def test_null_coalescing_after_reserved_word(self, kw):
        orig = f"function f {{ $a = $null; {kw} $a ?? 'd' }}; try {{ f }} catch {{ $_ | Out-Null }}"
        assert _round2_ps7_runs_ok(orig), "precondition: original must be valid PS7"
        out = _round2_transform(orig)
        assert "return $a" not in out and "throw $a" not in out, \
            f"transform folded the {kw!r} keyword into the operand: {out!r}"
        assert _round2_ps51_parse_errors(out) == [], f"5.1 parse error on transform: {out!r}"

    def test_nested_ternary_left_untransformed(self):
        """A ternary nested inside another ternary's branch arm was left raw:
        the outer rewrite places the inner ``? :`` inside ``{ ... }`` (brace
        depth > 0), where the ternary scanner deliberately gives up."""
        orig = "$c = $true; $d = $false; ($c ? ($d ? 1 : 2) : 3)"
        assert _round2_ps7_runs_ok(orig), "precondition: original must be valid PS7"
        out = _round2_transform(orig)
        assert "? " not in out, f"transform left a raw ternary in: {out!r}"
        assert _round2_ps51_parse_errors(out) == [], f"5.1 parse error on transform: {out!r}"

    def test_ternary_inside_interpolated_subexpr(self):
        orig = '$c = $true; "$($c ? "y" : "n")"'
        assert _round2_ps7_runs_ok(orig), "precondition: original must be valid PS7"
        out = _round2_transform(orig)
        assert "?" not in out.replace("$?", ""), \
            f"transform left a raw ternary inside a string subexpr: {out!r}"
        assert _round2_ps51_parse_errors(out) == [], f"5.1 parse error on transform: {out!r}"

    def test_ternary_false_arm_comma_semantics(self):
        """``@($c ? 7 : 8, 5)`` is ONE element in PS7 (comma binds tighter than
        the ternary); the transform used to cut the false arm at the comma,
        emitting two elements."""
        if _ROUND2_PWSH7 is None:
            pytest.skip("needs pwsh 7 for ground truth")
        orig = "$c=$true; @($c ? 7 : 8, 5) | ForEach-Object { $_ }"
        out = _round2_transform(orig)
        assert _round2_ps51_parse_errors(out) == [], f"sanity: 5.1 parse error: {out!r}"
        rc7, o7, _e7 = _round2_run_ps(_ROUND2_PWSH7, orig)
        rc5, o5, _e5 = _round2_run_ps(_ROUND2_POWERSHELL51, out)
        assert rc7 == 0, f"ground truth PS7 failed: {_e7!r}"
        assert o7 == o5, (
            f"semantic divergence: PS7={o7!r} PS5.1(transformed)={o5!r}\n"
            f"transformed: {out!r}"
        )

    def test_lone_dollar_quote_blocks_active_subexpr(self):
        """A lone (unbalanced) ``$'`` inside ``"..."`` made ``_find_dq_end``
        bail and copy the whole DQ region verbatim, losing the ``\\``->``/``
        rewrite of a genuinely active ``$(...)`` after it."""
        balanced = r'''echo "cost $'50' $(echo C:\b)"'''
        assert "C:/b" in _round2_unq(balanced), "regression guard: balanced case already broken?"
        lone = r'''echo "$'x $(echo C:\b)"'''
        noquote = r'''echo "x $(echo C:\b)"'''
        assert "C:/b" in _round2_unq(noquote), "sanity: plain active subexpr must convert"
        out = _round2_unq(lone)
        assert "C:/b" in out, (
            f"lone $' suppressed the backslash rewrite of the active $(): {out!r}"
        )

    def test_herestring_close_delimiter_on_code_line(self):
        """A here-string closing ``"@`` on a line that also carries real code:
        the whole line was treated as inside the region and skipped, leaving
        a ``??`` after the delimiter raw (5.1 parse error)."""
        orig = '$a = $null\n$x = @"\ntext\n"@; $y = $a ?? "d"'
        assert _round2_ps7_runs_ok(orig), "precondition: original must be valid PS7"
        out = _round2_transform(orig)
        assert "??" not in out, f"transform skipped ?? on the here-string close line: {out!r}"
        assert _round2_ps51_parse_errors(out) == [], f"5.1 parse error on transform: {out!r}"


# ===========================================================================
# Round-3 regressions (adversarial re-review: structural heredoc moves,
# pwsh chain associativity/bounds, continuation join, block comments,
# region placeholders).  See git history / review notes for context.
# ===========================================================================

_ROUND3_SHIM = _ROUND2_SHIM


def _round3_run_bash_script(script: str) -> subprocess.CompletedProcess[str]:
    """Run *script* through the real Git Bash found by ``find_bash``."""
    bash = find_bash()
    assert bash is not None
    fd, path = tempfile.mkstemp(suffix=".sh")
    try:
        with os.fdopen(fd, "w", newline="\n", encoding="utf-8") as handle:
            handle.write(script)
        return subprocess.run(
            [bash, "--noprofile", "--norc", path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            stdin=subprocess.DEVNULL,
        )
    finally:
        os.unlink(path)


# The canonical model mistake: heredoc inside $( ... ) (or backticks) with a
# pipeline operator carried on the closing-delimiter line.  The trailing-
# operator repair must keep the heredoc body INSIDE the substitution: the
# closing delimiter may not be dragged up onto the redirection line.
_ROUND3_CMDSUB_PAREN = (
    "x=$(cat <<ZZEND\nfoo\nbar\nZZEND\n| wc -l)\nprintf 'X=[%s]' \"$x\"\n"
)
_ROUND3_CMDSUB_BACKTICK = (
    "printf 'V=%s\\n' `cat <<ZZEND\nfoo\nZZEND\n| tr a-z A-Z`\n"
)


class TestBashFixRound3HeredocCmdSubMove:
    """``_apply_heredoc_operator_move`` must not eject a heredoc body from a
    command substitution when the trailing-operator line also carries the
    substitution's closing delimiter."""

    def test_dollar_paren_body_stays_inside_substitution(self):
        fixed = _ROUND3_SHIM.fix_bash_command(_ROUND3_CMDSUB_PAREN).command
        closing_paren = fixed.index(")")
        terminator = fixed.rindex("ZZEND")
        assert closing_paren > terminator, (
            "heredoc body was ejected outside $( ... ): "
            f"fixed={fixed!r} closing_paren@{closing_paren} terminator@{terminator}"
        )

    def test_backtick_body_stays_inside_substitution(self):
        fixed = _ROUND3_SHIM.fix_bash_command(_ROUND3_CMDSUB_BACKTICK).command
        assert fixed.count("`") == 2
        closing_backtick = fixed.rindex("`")
        terminator = fixed.rindex("ZZEND")
        assert closing_backtick > terminator, (
            "heredoc body was ejected outside the backtick substitution: "
            f"fixed={fixed!r}"
        )


@pytest.mark.skipif(not BASH_AVAILABLE, reason="bash not installed")
class TestBashFixRound3HeredocCmdSubMoveExecution:
    """Execution ground truth for the substitution-aware operator move."""

    def test_dollar_paren_fix_has_no_unterminated_heredoc(self):
        # Correct repair keeps the body inside $( ... ): x counts the two
        # body lines (X=[2]) and bash emits no here-document warning.
        fixed = _ROUND3_SHIM.fix_bash_command(_ROUND3_CMDSUB_PAREN).command
        proc = _round3_run_bash_script(fixed)
        combined = proc.stdout + proc.stderr
        assert "unterminated here-document" not in combined, (
            f"fixed command is malformed: {fixed!r}\noutput={combined!r}"
        )
        assert proc.returncode == 0
        assert "X=[2]" in proc.stdout

    def test_backtick_fix_preserves_heredoc_value(self):
        # Correct repair: the body 'foo' is piped through tr -> 'FOO'.  The
        # buggy move ejects the body, so the substitution is empty and the
        # body lines run as (missing) commands.
        fixed = _ROUND3_SHIM.fix_bash_command(_ROUND3_CMDSUB_BACKTICK).command
        proc = _round3_run_bash_script(fixed)
        combined = proc.stdout + proc.stderr
        assert "command not found" not in combined, (
            f"heredoc body executed as commands: {fixed!r}\noutput={combined!r}"
        )
        assert proc.returncode == 0
        assert "V=FOO" in proc.stdout


# ---------------------------------------------------------------------------
# pwsh chain transform: left associativity and operand bounds
# ---------------------------------------------------------------------------

_ROUND3_PS7 = shutil.which("pwsh") or shutil.which("pwsh.exe")
_ROUND3_PS51 = shutil.which("powershell") or shutil.which("powershell.exe")


def _round3_ps_lines(exe, script: str) -> list[str]:
    rc, out, _err = _round2_run_ps(exe, script, timeout=90)
    return sorted(ln for ln in out.splitlines() if ln.strip())


_round3_chain_skip = pytest.mark.skipif(
    sys.platform != "win32" or not _ROUND3_PS7 or not _ROUND3_PS51,
    reason="requires Windows + pwsh(PS7) + powershell(PS5.1)",
)


@_round3_chain_skip
class TestBashFixRound3ChainLeftAssoc:
    """``A || B && C`` is ``(A || B) && C`` in PS7 (left-associative); the
    sequential ``if`` rewrite must not nest ``&& C`` inside the ``||`` block
    (which silently drops C when A succeeds)."""

    def test_chain_operator_is_left_associative(self):
        src = "Write-Output A || Write-Output B && Write-Output C"
        out = _round2_transform(src)
        expected = _round3_ps_lines(_ROUND3_PS7, src)      # PS7: A then C
        actual = _round3_ps_lines(_ROUND3_PS51, out)       # 5.1 of the shim
        assert actual == expected, (
            f"mixed chain mis-associated: PS7={expected!r} vs "
            f"5.1({out!r})={actual!r}"
        )


@_round3_chain_skip
class TestBashFixRound3ChainScriptBlock:
    """A trailing ``&& C`` outside a ``ForEach-Object { ... }`` scriptblock
    must stay outside: rightmost-first rewriting re-nested it inside the
    braces, running C once per pipeline item."""

    def test_chain_does_not_renest_trailing_operator_into_scriptblock(self):
        src = "1,2 | ForEach-Object { Write-Output I && Write-Output J } && Write-Output C"
        out = _round2_transform(src)
        expected = _round3_ps_lines(_ROUND3_PS7, src)   # PS7: C once
        actual = _round3_ps_lines(_ROUND3_PS51, out)    # buggy: C per item
        assert actual == expected, (
            f"trailing '&& C' re-nested inside ForEach scriptblock: "
            f"PS7={expected!r} vs 5.1({out!r})={actual!r}"
        )


@_round3_chain_skip
class TestBashFixRound3ChainSemicolon:
    """``;`` is a statement separator with lower precedence than ``&&``:
    ``A && B ; C`` runs C unconditionally.  The right operand must stop at
    the ``;``, not swallow it into the generated ``if`` block."""

    def test_chain_does_not_capture_trailing_semicolon_statement(self):
        # A fails (native exit 1) -> B skipped, but C (after ';') must run.
        src = "cmd /c exit 1 && Write-Output B ; Write-Output C"
        out = _round2_transform(src)
        expected = _round3_ps_lines(_ROUND3_PS7, src)   # PS7: ['C']
        actual = _round3_ps_lines(_ROUND3_PS51, out)    # buggy: []
        assert actual == expected, (
            f"trailing ';'-statement swallowed: PS7={expected!r} vs "
            f"5.1({out!r})={actual!r}"
        )


# ---------------------------------------------------------------------------
# Backtick continuation join: an escaped backtick pair at EOL is NOT a
# continuation (the first backtick escapes the second; the newline is real).
# ---------------------------------------------------------------------------


class TestBashFixRound3BacktickContinuation:
    def test_escaped_backtick_at_eol_is_not_a_continuation(self):
        src = "Write-Output a ``\nb"
        joined = _ROUND3_SHIM._join_continuation_lines(src)
        # The real line break must be preserved: 'b' stays a separate statement.
        assert "\n" in joined, repr(joined)
        assert joined.splitlines()[-1].strip() == "b", repr(joined)

    def test_single_backtick_at_eol_is_a_continuation(self):
        # control: the legitimate case still joins.
        src = "Write-Output a `\nb"
        joined = _ROUND3_SHIM._join_continuation_lines(src)
        assert "\n" not in joined, repr(joined)


# ---------------------------------------------------------------------------
# Block comments do NOT nest: the comment ends at the first ``#>``.
# ---------------------------------------------------------------------------


class TestBashFixRound3BlockComment:
    def test_nested_block_comment_leaves_no_raw_coalesce(self):
        src = '<# a <# b #> Write-Output ($x ?? "d")'
        out = _round2_transform(src)
        # The '??' is live code (after the first '#>'); it MUST be downgraded.
        assert "??" not in out, out

    def test_nested_block_comment_multiline_leaves_no_raw_coalesce(self):
        src = '<# a\n <# b #>\n Write-Output ($x ?? "d")'
        out = _round2_transform(src)
        assert "??" not in out, out

    @pytest.mark.skipif(sys.platform != "win32" or not _ROUND3_PS51,
                        reason="requires Windows PowerShell 5.1")
    def test_nested_block_comment_output_parses_in_51(self):
        src = '<# a <# b #> Write-Output ($x ?? "d")'
        out = _round2_transform(src)
        assert _round2_ps51_parse_errors(out) == [], (
            f"5.1 parse error for {out!r}"
        )


# ---------------------------------------------------------------------------
# Region placeholders on mid-line region boundaries must not be folded into
# generated operands (the substitution would re-inject '#>' into the if).
# ---------------------------------------------------------------------------


class TestBashFixRound3RegionPlaceholder:
    def test_multiline_region_delimiter_not_absorbed_into_operand(self):
        src = "<#\nline1\n#> $a ?? 1"
        out = _round2_transform(src)
        # The closing delimiter must never be spliced into the coalesced operand.
        assert "$null -ne #>" not in out, out
        assert "{ #>" not in out, out

    @pytest.mark.skipif(sys.platform != "win32" or not _ROUND3_PS51,
                        reason="requires Windows PowerShell 5.1")
    def test_multiline_region_close_line_parses_in_51(self):
        src = "<#\nline1\n#> $a ?? 1"
        out = _round2_transform(src)
        assert _round2_ps51_parse_errors(out) == [], (
            f"5.1 parse error for {out!r}"
        )


# ---------------------------------------------------------------------------
# Round-4 review regressions
#
# Four independent surfaces were audited (scanner tokenization, structural
# constructs, Windows-path decode/emission + the bash_fix wrapper, and the
# pwsh transform gaps left after rounds 1-3).  Confirmed bugs and their fixes:
#
# R4-B  Brace-less compound function bodies (``f() if/while/until/case/for``)
#       never re-armed command position (the ``function_body_expected``
#       handler only recognised ``{``), so a bare fallback-named command in
#       the compound's first command slot was treated as data and its
#       fallback registration dropped -> ``command not found`` at runtime.
#
# R4-C  Anchored Windows paths with MIXED separators (``D:/x\y``,
#       ``~/Desktop\file.txt``) failed every classifier (the drive regex
#       required a backslash right after the colon; the relative-path branch
#       required >= 2 backslashes) and were emitted byte-for-byte -- but
#       outside quotes Bash removes a backslash before ANY character, so the
#       lone backslash was eaten and two path segments collapsed into one
#       name (wrong file / value corruption).
#
# R4-F1 ``??`` co-located with a ternary on one line: the operand scanner
#       (``_EXPR_STOP`` lacks ``?``/``:``) swallowed the ternary tokens into
#       its operands and re-emitted them inside generated ``{ ... }`` arms
#       where the ternary pass' brace guard refuses to look -> raw ``? :``
#       survived -> 5.1 parse error on valid PS7 input.
#
# R4-F2 ``&&``/``||`` in expression position: a chain inside ``( ... )``
#       (``if ($a && $b)``, ``Write-Host ($a && $b)``) was rewritten to a
#       ``;``-separated statement sequence INSIDE the parens -> 5.1 parse
#       error; an assignment RHS chain (``$x = $true && $false``) assigned
#       only the left segment and leaked the right one to the output stream.
#
# R4-F3 ``??`` right operand cut at a comma: comma binds tighter than ``??``
#       in PS7 (``$a ?? $b, 3`` is ``$a ?? ($b, 3)`` -- ONE element when
#       ``$a`` is non-null), but the transform emitted ``$(if ...), 3`` ->
#       two elements -> observable wrong output.
#
# R4-F4 Chain operators inside an interpolated sub-expression
#       (``"$(a && b)"``) were skipped entirely (raw ``&&`` -> 5.1 parse
#       error).  The stated reason (a line-structural rewrite is "not valid
#       inside an interpolation") is empirically false.
# ---------------------------------------------------------------------------


class TestBashFixRound4CompoundFunctionBody:
    """Bug R4-B: brace-less compound function bodies dropped registrations."""

    @pytest.mark.parametrize(
        "source,expected",
        [
            # case compound body -> the body command is dropped entirely
            ("f() case x in a) tree;; esac", ("tree",)),
            ("f() case x in *) tree;; esac", ("tree",)),
            ("f() case x in a) tree;; b) rev;; esac", ("tree", "rev")),
            # if/while/until: the test command (first slot) is dropped
            ("f() if tree; then rev; fi", ("tree", "rev")),
            ("f() while tree; do rev; done", ("tree", "rev")),
            ("f() until tree; do rev; done", ("tree", "rev")),
            # function-keyword / space forms share the same defect
            ("function f case x in a) tree;; esac", ("tree",)),
            ("function f() case x in a) tree;; esac", ("tree",)),
            ("f () case x in a) tree;; esac", ("tree",)),
            # do-less for-brace body: the first command word is dropped
            ("f() for ((i=0;i<2;i++)) { tree; rev; }", ("tree", "rev")),
            # defect still shows when nested inside a case body
            ("case x in a) f() while tree; do rev; done;; esac", ("tree", "rev")),
        ],
    )
    def test_compound_function_body_registers_body_command(self, source, expected):
        result = _shell_compat.fix_bash_command(source)
        assert result.replacements == expected, (
            f"compound function body dropped fallback registrations "
            f"({result.replacements} != {expected})"
        )
        assert result.command.endswith("\n" + source)

    def test_compound_case_body_nul_and_registration_consistent(self):
        # Redirection target is fixed regardless of command position, and the
        # *command* fallback registration must be fixed too.
        result = _shell_compat.fix_bash_command("f() case x in a) tree > nul;; esac")
        assert result.nul_fixes == ("nul",)
        assert result.replacements == ("tree",)

    @pytest.mark.skipif(not BASH_AVAILABLE, reason="requires Git Bash")
    def test_compound_bodies_really_execute_their_commands(self):
        # Each construct must actually RUN its body/test command (marker echo),
        # so the fallback registration the scanner now emits is genuinely
        # required.
        bash = find_bash()
        for script in (
            "f() if true; then echo ran-if; fi; f",
            "f() while read x; do echo ran-while; done <<< z; f",
            "f() until false; do echo ran-until; break; done; f",
            "f() case a in a) echo ran-case;; esac; f",
        ):
            proc = subprocess.run(
                [bash, "-c", script], capture_output=True, text=True, timeout=30
            )
            assert "ran-" in proc.stdout, (script, proc.stdout, proc.stderr)

    @pytest.mark.skipif(not BASH_AVAILABLE, reason="requires Git Bash")
    def test_missing_registration_breaks_at_runtime(self):
        # ``tree`` is absent on Git Bash; only the prelude fallback makes it
        # run.  Top-level case registers it; the compound-body form must too.
        bash = find_bash()
        top = _shell_compat.fix_bash_command("case x in a) tree / ;; esac")
        assert top.changed and top.replacements == ("tree",)
        comp = _shell_compat.fix_bash_command(
            "f() case x in a) tree /;; esac; x=a; f"
        )
        assert comp.changed and comp.replacements == ("tree",)
        # And the emitted command actually runs (fallback defined).
        proc = subprocess.run(
            [bash, "-c", comp.command], capture_output=True, text=True, timeout=30
        )
        assert "command not found" not in proc.stderr, proc.stderr


class TestBashFixRound4MixedSlashPaths:
    """Bug R4-C: anchored mixed-separator paths (``D:/x\\y``) are rewritten.

    Left untouched, Bash eats the lone backslash (``\X`` -> ``X`` for every
    ``X`` outside quotes) and collapses two segments into one name.
    """

    @pytest.fixture()
    def _scratch(self, tmp_path):
        md = tmp_path / "md"
        md.mkdir()
        (md / "inner.txt").write_text("INTENDED")
        (tmp_path / "mdx.txt").write_text("WRONG")
        return tmp_path

    def test_mixed_slash_drive_path_is_rewritten(self, _scratch):
        cmd = f"cat {_scratch.as_posix()}/md\\inner.txt"
        result = _shell_compat.fix_bash_command(cmd)
        assert "\\inner" not in result.command
        assert f"{_scratch.as_posix()}/md/inner.txt" in result.command
        assert result.path_changes

    def test_mixed_slash_preserves_word_value(self, _scratch):
        result = _shell_compat.fix_bash_command(
            f"echo {_scratch.as_posix()}/md\\x.txt"
        )
        assert result.path_changes
        assert f"{_scratch.as_posix()}/md/x.txt" in result.command

    def test_mixed_slash_home_relative(self):
        result = _shell_compat.fix_bash_command(r"echo ~/Desktop\file.txt")
        assert result.command == "echo ~/Desktop/file.txt"

    @pytest.mark.skipif(not BASH_AVAILABLE, reason="requires Git Bash")
    def test_mixed_slash_real_bash_reads_intended_file(self, _scratch):
        bash = find_bash()
        cmd = f"cat {_scratch.as_posix()}/md\\inner.txt"
        fixed = _shell_compat.fix_bash_command(cmd).command
        proc = subprocess.run(
            [bash, "-c", fixed], capture_output=True, text=True, timeout=30
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "INTENDED"

    @pytest.mark.skipif(not BASH_AVAILABLE, reason="requires Git Bash")
    def test_pure_backslash_anchor_is_fixed_control(self, _scratch):
        # control: the same path in pure-backslash form is rewritten and works
        cmd = f"cat {str(_scratch).replace(chr(92), '/')}\\md\\inner.txt"
        result = _shell_compat.fix_bash_command(cmd)
        assert result.path_changes
        bash = find_bash()
        proc = subprocess.run(
            [bash, "-c", result.command], capture_output=True, text=True, timeout=30
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "INTENDED"


_round4_ps_skip = pytest.mark.skipif(
    sys.platform != "win32" or not _ROUND3_PS7 or not _ROUND3_PS51,
    reason="requires Windows + pwsh(PS7) + powershell(PS5.1)",
)
_round4_ps51_skip = pytest.mark.skipif(
    sys.platform != "win32" or not _ROUND3_PS51,
    reason="requires Windows PowerShell 5.1",
)


class TestBashFixRound4CoalesceTernary:
    """Bug R4-F1: ``??`` operands must not swallow (or trap) ternary tokens."""

    @pytest.mark.parametrize(
        "src",
        [
            "$c=$true;$a=$null;$r = $c ? $a ?? 1 : 2",
            "$c=$false;$a=$null;$r = $c ? 5 : $a ?? 1",
            "$c=$true;$r = $c ? 1 : 2 ?? 3",
            "$a=$null;$c=$true;$r = $a ?? $c ? 2 : 3",
            "$a=$null;$c=$true;$r = $a ?? ($c ? 1 : 2)",
            "$c=$true;$r = ($c ? 1 : 2) ?? 3",
            "$a=$null;$c=$true;$r = $a?.b ?? $c ? 1 : 2",
        ],
    )
    def test_no_raw_ternary_survives(self, src):
        out = _round2_transform(src)
        # any '?' left in the output must be a null-conditional remnant
        # (none of these inputs use one) or a '$?' -- never a bare ternary
        assert " ? " not in out, out
        assert " : " not in out, out

    @_round4_ps51_skip
    @pytest.mark.parametrize(
        "src",
        [
            "$c=$true;$a=$null;$r = $c ? $a ?? 1 : 2",
            "$c=$false;$a=$null;$r = $c ? 5 : $a ?? 1",
            "$c=$true;$r = $c ? 1 : 2 ?? 3",
            "$a=$null;$c=$true;$r = $a ?? $c ? 2 : 3",
            "$a=$null;$c=$true;$r = $a ?? ($c ? 1 : 2)",
            "$c=$true;$r = ($c ? 1 : 2) ?? 3",
            "$a=$null;$c=$true;$r = $a?.b ?? $c ? 1 : 2",
        ],
    )
    def test_parses_in_51(self, src):
        out = _round2_transform(src)
        assert _round2_ps51_parse_errors(out) == [], (
            f"5.1 parse error for {out!r}"
        )

    @_round4_ps_skip
    @pytest.mark.parametrize(
        "src",
        [
            "$c=$true;$a=$null;$r = $c ? $a ?? 1 : 2",
            "$c=$false;$a=$null;$r = $c ? 5 : $a ?? 1",
            "$c=$true;$r = $c ? 1 : 2 ?? 3",
            "$a=$null;$c=$true;$r = $a ?? $c ? 2 : 3",
            "$a=$null;$c=$true;$r = $a ?? ($c ? 1 : 2)",
            "$c=$true;$r = ($c ? 1 : 2) ?? 3",
            "$a=$null;$c=$true;$r = $a?.b ?? $c ? 1 : 2",
        ],
    )
    def test_matches_ps7_output(self, src):
        out = _round2_transform(src)
        expected = _round3_ps_lines(_ROUND3_PS7, src)
        actual = _round3_ps_lines(_ROUND3_PS51, out)
        assert actual == expected, (src, out, expected, actual)


class TestBashFixRound4ChainExpressionPosition:
    """Bug R4-F2: chains in ``( ... )`` / assignment RHS must become values."""

    @pytest.mark.parametrize(
        "src",
        [
            '$a=$true;$b=$false;if ($a && $b) { "T" } else { "F" }',
            '$a=$false;$b=$true;if ($a || $b) { "T" } else { "F" }',
            "$a=$true;$b=$false;Write-Host ($a && $b)",
            '$a=$true;$b=$false;$r = ($a && $b); "R=[$r]"',
        ],
    )
    def test_parses_in_51(self, src):
        out = _round2_transform(src)
        assert _round2_ps51_parse_errors(out) == [], (
            f"5.1 parse error for {out!r}"
        )

    @_round4_ps_skip
    @pytest.mark.parametrize(
        "src",
        [
            '$a=$true;$b=$false;if ($a && $b) { "T" } else { "F" }',
            '$a=$false;$b=$true;if ($a || $b) { "T" } else { "F" }',
            "$a=$true;$b=$false;Write-Host ($a && $b)",
            '$a=$true;$b=$false;$r = ($a && $b); "R=[$r]"',
            '$x = $true && $false; "X=[$x]"',
        ],
    )
    def test_matches_ps7_output(self, src):
        out = _round2_transform(src)
        expected = _round3_ps_lines(_ROUND3_PS7, src)
        actual = _round3_ps_lines(_ROUND3_PS51, out)
        assert actual == expected, (src, out, expected, actual)


class TestBashFixRound4CoalesceComma:
    """Bug R4-F3: comma binds tighter than ``??`` in PS7."""

    @_round4_ps_skip
    def test_comma_rhs_yields_single_element_when_left_not_null(self):
        src = '$a=9;$b=2;$r = $a ?? $b, 3; "N=@($r).Count V=$($r -join \';\')"'
        out = _round2_transform(src)
        expected = _round3_ps_lines(_ROUND3_PS7, src)
        actual = _round3_ps_lines(_ROUND3_PS51, out)
        assert actual == expected, (out, expected, actual)

    @_round4_ps_skip
    def test_comma_rhs_inside_array_subexpression(self):
        src = '$a=9;$r=@($a ?? 1, 2); "V=$r"'
        out = _round2_transform(src)
        expected = _round3_ps_lines(_ROUND3_PS7, src)
        actual = _round3_ps_lines(_ROUND3_PS51, out)
        assert actual == expected, (out, expected, actual)

    def test_command_argument_comma_not_swallowed(self):
        # ``f($a ?? 1, $b)``: the comma separates command ARGUMENTS and must
        # survive the transform.
        out = _round2_transform("$a ?? 1, $b") if False else None  # placeholder
        src = "Write-Output ($a ?? 1), $b"
        out = _round2_transform(src)
        assert "), " in out, out


class TestBashFixRound4ChainInterpolation:
    """Bug R4-F4: chains inside "$ ( ... )" must be rewritten, not skipped."""

    @pytest.mark.parametrize(
        "src",
        [
            '$s = "X$(Write-Output 1 && Write-Output 2)Y"; $s',
            '$a = 0; $s = "V=$(Write-Output a || Write-Output b)W"; $s',
        ],
    )
    def test_no_raw_chain_operator_survives(self, src):
        out = _round2_transform(src)
        assert "&&" not in out, out
        assert "||" not in out, out

    @_round4_ps51_skip
    @pytest.mark.parametrize(
        "src",
        [
            '$s = "X$(Write-Output 1 && Write-Output 2)Y"; $s',
            '$a = 0; $s = "V=$(Write-Output a || Write-Output b)W"; $s',
        ],
    )
    def test_parses_in_51(self, src):
        out = _round2_transform(src)
        assert _round2_ps51_parse_errors(out) == [], (
            f"5.1 parse error for {out!r}"
        )

    @_round4_ps_skip
    @pytest.mark.parametrize(
        "src",
        [
            '$s = "X$(Write-Output 1 && Write-Output 2)Y"; $s',
            '$a = 0; $s = "V=$(Write-Output a || Write-Output b)W"; $s',
        ],
    )
    def test_matches_ps7_output(self, src):
        out = _round2_transform(src)
        expected = _round3_ps_lines(_ROUND3_PS7, src)
        actual = _round3_ps_lines(_ROUND3_PS51, out)
        assert actual == expected, (src, out, expected, actual)


class TestBashFixRound4SubstHashNotComment:
    """Bug R4-A: a ``#`` glued to a command/process-substitution close paren
    is word-internal in Bash (a substitution expands inside a word), NOT a
    comment.  The scanner's ``_comment_starts`` peeks one byte back at a
    bare ``)`` and could not tell a substitution-close paren from a
    control-operator (subshell) paren, so it skipped the rest of the line as
    a comment and dropped every fallback registration / nul rewrite /
    path rewrite in that tail."""

    @pytest.mark.parametrize(
        "command",
        [
            "$(true)#foo; rev --help",
            "$(true)#foo; column --help",
            "<(echo a)#x; rev --help",
            ">(echo a)#x; rev --help",
            "$(echo hi)#z; tree",
            "echo a $(true)#x; findstr y",
            "$(true)$(false)#x; rev --help",
            "$(true)#foo | rev --help",
            "$(true)#foo || rev --help",
        ],
    )
    def test_fallback_after_subst_hash_is_registered(self, command):
        result = _shell_compat.fix_bash_command(command)
        fallbacks = {"rev", "column", "tree", "findstr"}
        assert fallbacks & set(result.replacements), (
            "fallback after a '#'-glued $(...)/<(...)/>(...) close-paren was "
            "silently skipped as a comment; got replacements=%r for %r"
            % (result.replacements, command)
        )

    def test_nul_rewrite_in_subst_hash_tail_is_not_missed(self):
        # '> nul' in the tail must still be rewritten to /dev/null (Bash runs
        # that command, creating an accidental 'nul' file otherwise).
        result = _shell_compat.fix_bash_command(r"$(true)#x; rev > nul")
        assert result.nul_fixes == ("nul",), result.nul_fixes

    def test_path_rewrite_in_subst_hash_tail_is_not_missed(self):
        result = _shell_compat.fix_bash_command(r"$(true)#x; cat D:\a\b")
        assert result.path_changes == (r"D:\a\b",), result.path_changes

    def test_hash_after_space_is_still_a_comment(self):
        # Contrast: '# ' after whitespace genuinely comments out the tail;
        # rev must NOT be registered there.
        result = _shell_compat.fix_bash_command("$(true) #foo; rev --help")
        assert "rev" not in result.replacements

    def test_hash_after_subshell_paren_is_still_a_comment(self):
        # Contrast: after a SUBSHELL ')' the '#' IS a control-operator comment
        # in Bash, so the scanner must match Bash and leave rev unregistered.
        result = _shell_compat.fix_bash_command("(true)#foo; rev --help")
        assert "rev" not in result.replacements

    @pytest.mark.skipif(not BASH_AVAILABLE, reason="requires Git Bash")
    def test_rev_actually_runs_after_subst_hash(self):
        # Define rev so we can observe Bash reaching it as a command word.
        bash = find_bash()
        p = subprocess.run(
            [bash, "-c", "rev() { echo REV_RAN; }; $(true)#foo; rev --help"],
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
        )
        assert "REV_RAN" in p.stdout, p.stdout + p.stderr

    @pytest.mark.skipif(not BASH_AVAILABLE, reason="requires Git Bash")
    def test_missing_registration_breaks_at_runtime_subst_hash(self):
        # With 'rev' defined by the prelude fallback it really runs — the
        # scanner must have registered it.  (Bash also tries '#foo' itself as
        # a command word — the word '$(true)#foo' expands to it — so stderr
        # may contain an unrelated '#foo: command not found'.)
        bash = find_bash()
        fixed = _shell_compat.fix_bash_command("$(true)#foo; rev --help")
        assert "rev" in fixed.replacements
        p = subprocess.run(
            [bash, "-c", fixed.command], capture_output=True, text=True,
            timeout=30, stdin=subprocess.DEVNULL,
        )
        assert "rev: command not found" not in p.stderr, p.stderr


# ===========================================================================
# Round-5 review regressions
# ===========================================================================
# R5-H1  Heredoc trailing-operator move with a construct closer MID-LINE: the
# stay-guard only detached a TRAILING RUN of closers, so
# ``x=$(cat <<EOF ... EOF\n| wc -l); echo q`` moved the whole line up, closing
# the substitution before the heredoc body -> bash "unterminated
# here-document".  The guard now keeps the suffix from the last excess closer
# onward, and joins an operator-led stay tail onto the closer line.
# R5-R1  Backslash-escaped wrapper words (``\command tree``, ``\env tree``)
# only suppress alias lookup but invoke the same wrapper; the wrapper tables
# were keyed on the raw token, so the wrapped command was never registered
# (real bash: still "command not found" after fixing).
# R5-P1  Chained null-coalescing assignment ``$x ??= $y ??= 3`` (right-assoc
# in PS7): the value scan stopped at the inner ``??=``'s ``=`` leaving raw
# ``??`` -> 5.1 ParserError.
# R5-P2  Implicit operator-at-EOL line continuation (``$a ??`` NL ``$b``,
# ``cmd &&`` NL ``next``) without a backtick: the joiner only collapsed
# backticks, so the operator lost its operand -> 5.1 ParserError or lost
# gating semantics.
# R5-P3  ``&&``/``||`` inside ``"$( ... )`` with a QUOTED-LITERAL operand
# (``"res: $($a || 'fb')"``): the interpolation phase-2 boundary scans
# treated the nested literal as a region edge -> empty right operand, the
# literal leaked outside the generated block -> 5.1 ParserError.
# R5-P4  Mixed null-conditional chain ``$a?[$i]?.b``: the ``?.`` pass took
# ``[$i]`` as its base (the ``?``-guard only recognised ``?.``), and the
# later bracket pass misread the raw ``?[`` inside the generated condition.
# R5-P5  Keyword glue: ``return $a ?? 3`` / ``return $c ? 1 : 2`` emitted
# ``return$(...)`` (no space) -> 5.1 parses a command name; the ternary
# brace-guard also left return-operand ternaries raw inside ``{ ... }``.


class TestBashFixRound5HeredocCloserStay:
    """R5-H1: a construct closer mid-line after a heredoc terminator stays."""

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            # ``)`` mid-line: only the closer and its tail stay after the
            # terminator; the pipe target rides up to the redirection line.
            (
                "x=$(cat <<EOF\nbody\nEOF\n| wc -l); echo q >/dev/null",
                "x=$(cat <<EOF | wc -l\nbody\nEOF\n); echo q >/dev/null\n",
            ),
            # Backtick substitution variant.
            (
                "echo `cat <<EOF\nbody\nEOF\n| wc -l` done",
                "echo `cat <<EOF | wc -l\nbody\nEOF\n` done\n",
            ),
            # Two openers, two closers: each closer balances in order.
            (
                "x=$( (cat <<EOF\nbody\nEOF\n| wc -l) ); echo y",
                "x=$( (cat <<EOF | wc -l)\nbody\nEOF\n); echo y\n",
            ),
            # Closer at end of line (the original guard case) still works.
            (
                "x=$(cat <<EOF\nbody\nEOF\n| wc -l)",
                "x=$(cat <<EOF | wc -l\nbody\nEOF\n)\n",
            ),
            # Balanced group in the moved text: only the excess closer stays.
            (
                "x=$(cat <<EOF\nbody\nEOF\n| (wc -l) ); echo y",
                "x=$(cat <<EOF | (wc -l)\nbody\nEOF\n); echo y\n",
            ),
            # Operator-led continuation lines after the closer are joined
            # onto the closer line (an operator-led physical line after a
            # completed command is a bash syntax error).
            (
                "x=$(cat <<EOF\nbody\nEOF\n| wc -l)\n&& echo done",
                "x=$(cat <<EOF | wc -l\nbody\nEOF\n) && echo done\n",
            ),
        ],
    )
    def test_closer_stays_after_terminator(self, source: str, expected: str) -> None:
        assert _fix_for_windows(source).command == expected

    @pytest.mark.parametrize(
        "source",
        [
            # No trailing operator: nothing moves.
            "x=$(cat <<EOF\nbody\nEOF\necho q)",
            # A case-arm terminator line is left untouched.
            "case $x in\na) cat <<EOF\nbody\nEOF\n;;\nesac",
        ],
    )
    def test_no_stay_when_no_excess_closer(self, source: str) -> None:
        assert _fix_for_windows(source).command == source

    def test_no_construct_prefix_whole_line_moves(self) -> None:
        # Without an unclosed opener in the prefix the whole operator line
        # rides up (the original feature behaviour, kept whole).
        source = "cat <<EOF\nbody\nEOF\n| wc -l"
        assert _fix_for_windows(source).command == "cat <<EOF | wc -l\nbody\nEOF\n"

    @pytest.mark.skipif(not BASH_AVAILABLE, reason="bash not installed")
    @pytest.mark.parametrize(
        "source",
        [
            "x=$(cat <<EOF\nbody\nEOF\n| wc -l); echo q >/dev/null",
            "echo `cat <<EOF\nbody\nEOF\n| wc -l` done",
            "x=$( (cat <<EOF\nbody\nEOF\n| wc -l) ); echo y",
            "x=$(cat <<EOF\nbody\nEOF\n| (wc -l) ); echo y",
            "x=$(cat <<EOF\nbody\nEOF\n| wc -l)\n&& echo done",
        ],
    )
    def test_fixed_script_runs_in_real_bash(self, source: str) -> None:
        fixed = _fix_for_windows(source).command
        proc = subprocess.run(
            [find_bash(), "-c", fixed],
            capture_output=True,
            timeout=15,
            encoding="utf-8",
            errors="replace",
        )
        assert proc.returncode == 0, (
            f"fixed heredoc script failed in real bash:\n{fixed}\n{proc.stderr}"
        )
        assert "unterminated here-document" not in proc.stderr


class TestBashFixRound5EscapedWrappers:
    """R5-R1: backslash-escaped wrapper words register their operand."""

    @pytest.mark.parametrize(
        ("source", "expected_names"),
        [
            (r"\command tree", ("tree",)),
            (r"\env tree", ("tree",)),
            (r"\exec tree", ("tree",)),
            (r"\xargs tree", ("tree",)),
            (r"\sudo tree", ("sudo", "tree")),
            (r"\watch tree", ("watch", "tree")),
            (r"\gtimeout 5 tree", ("gtimeout", "tree")),
            (r"\stdbuf -o0 tree", ("tree",)),
            (r"\nice tree", ("tree",)),
        ],
    )
    def test_escaped_wrapper_registers_operand(
        self, source: str, expected_names: tuple[str, ...]
    ) -> None:
        result = _fix_for_windows(source)
        assert result.replacements == expected_names, (
            f"{source!r}: expected registrations {expected_names}, "
            f"got {result.replacements}"
        )

    @pytest.mark.parametrize(
        "source",
        [
            # Shell keywords: escaping suppresses the KEYWORD, so the
            # external command runs and wrapper registration is useless —
            # the word must NOT be treated as a wrapper.
            r"\time tree",
            r"\coproc tree",
            r"\! tree",
        ],
    )
    def test_escaped_reserved_words_untouched(self, source: str) -> None:
        result = _fix_for_windows(source)
        assert "tree" not in result.replacements

    @pytest.mark.skipif(not BASH_AVAILABLE, reason="bash not installed")
    @pytest.mark.parametrize(
        "source",
        [
            r"\command tree",
            r"\env tree",
            r"\xargs tree",
            r"\stdbuf -o0 tree",
            r"\nice tree",
        ],
    )
    def test_escaped_wrapper_fixed_script_runs(self, source: str) -> None:
        fixed = _fix_for_windows(source).command
        proc = subprocess.run(
            [find_bash(), "-c", fixed],
            capture_output=True,
            timeout=15,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
        )
        assert proc.returncode == 0, (
            f"{source!r} -> fixed script failed in real bash:\n{fixed}\n"
            f"{proc.stderr}"
        )


@_round4_ps_skip
class TestBashFixRound5PwshChainedNca:
    """R5-P1: chained ``??=`` (right-associative in PS7) parses and behaves."""

    def test_chained_nca_parses_and_behaves(self):
        src = '$x=$null; $y=$null; $x ??= $y ??= 3; "x=$x y=$y"'
        expected = _round3_ps_lines(_ROUND3_PS7, src)
        assert expected == ["x=3 y=3"]
        out = _round2_transform(src)
        assert _round2_ps51_parse_errors(out) == [], (
            f"5.1 PARSE ERROR on transformed:\n{out!r}"
        )
        actual = _round3_ps_lines(_ROUND3_PS51, out)
        assert actual == expected, (
            f"chained ??= mismatch: PS7={expected!r} vs 5.1={actual!r}\n{out!r}"
        )

    def test_nca_with_coalesce_value(self):
        src = '$x=$null; $y=$null; $x ??= $y ?? 3; "x=$x"'
        expected = _round3_ps_lines(_ROUND3_PS7, src)
        out = _round2_transform(src)
        assert _round2_ps51_parse_errors(out) == [], f"5.1 PARSE ERROR:\n{out!r}"
        assert _round3_ps_lines(_ROUND3_PS51, out) == expected


@_round4_ps_skip
class TestBashFixRound5PwshMultilineContinuation:
    """R5-P2: an operator at end of line continues the statement (no backtick)."""

    @pytest.mark.parametrize(
        "src",
        [
            "$a=$null\n$b=7\n$x = $a ??\n$b\n\"x=$x\"",
            "cmd /c exit 3 &&\nWrite-Output RUN\nWrite-Output END",
            "cmd /c exit 0 ||\nWrite-Output RUN\nWrite-Output END",
            "$x=$null\n$y=$null\n$x ??=\n3\n\"x=$x\"",
        ],
    )
    def test_operator_eol_continuation_preserved(self, src):
        rc, out7, _err = _round2_run_ps(_ROUND3_PS7, src)
        assert rc == 0, f"not a clean PS7 ground truth: {src!r}"
        out = _round2_transform(src)
        assert _round2_ps51_parse_errors(out) == [], (
            f"5.1 PARSE ERROR on transformed:\n{out!r}"
        )
        rc5, out5, _ = _round2_run_ps(_ROUND3_PS51, out)
        assert rc5 == 0 and out5 == out7.strip(), (
            f"multiline continuation mismatch:\n{src!r}\n->\n{out!r}\n"
            f"PS7={out7.strip()!r} vs 5.1={out5!r}"
        )

    def test_explicit_backtick_still_works(self):
        src = "cmd /c exit 1 `\n|| Write-Output FB"
        rc, out7, _ = _round2_run_ps(_ROUND3_PS7, src)
        assert rc == 0
        out = _round2_transform(src)
        _rc5, out5, _ = _round2_run_ps(_ROUND3_PS51, out)
        assert out5 == out7.strip()


@_round4_ps_skip
class TestBashFixRound5PwshInterpLiteralOperand:
    """R5-P3: chains inside ``"$( ... )"`` with a quoted-literal operand."""

    @pytest.mark.parametrize("expr", ["$a || 'fb'", "$a && 'yes'", "$a || 'fb'"])
    def test_interp_literal_operand_parses(self, expr):
        src = '$a="x"; "res: $(%s)"' % expr
        rc, _out, _err = _round2_run_ps(_ROUND3_PS7, src)
        assert rc == 0
        out = _round2_transform(src)
        assert _round2_ps51_parse_errors(out) == [], (
            f"5.1 PARSE ERROR on transformed:\n{out!r}"
        )
        expected = _round3_ps_lines(_ROUND3_PS7, src)
        assert _round3_ps_lines(_ROUND3_PS51, out) == expected


@_round4_ps51_skip
class TestBashFixRound5PwshNullCondBracketDot:
    """R5-P4: mixed null-conditional ``$a?[$i]?.b`` parses under 5.1."""

    def test_bracket_then_dot_parses(self):
        src = '$a = @( @{b=42} ); $i=0; $r = $a?[$i]?.b; "r=$r"'
        out = _round2_transform(src)
        assert _round2_ps51_parse_errors(out) == [], (
            f"5.1 PARSE ERROR on transformed:\n{out!r}"
        )

    def test_dot_then_bracket_parses(self):
        src = '$a = @( @{b=@{c=9}} ); $r = $a?.b?["c"]; "r=$r"'
        out = _round2_transform(src)
        assert _round2_ps51_parse_errors(out) == [], (
            f"5.1 PARSE ERROR on transformed:\n{out!r}"
        )


@_round4_ps_skip
class TestBashFixRound5PwshKeywordGlue:
    """R5-P5: ``return``/``throw`` operands get a space, not a glue."""

    def test_return_coalesce_behaves(self):
        src = "function Get-V(){ $a=$null; return $a ?? 3 }; (Get-V)"
        expected = _round3_ps_lines(_ROUND3_PS7, src)
        assert expected == ["3"]
        out = _round2_transform(src)
        assert "return$(" not in out, f"keyword glued:\n{out!r}"
        assert _round2_ps51_parse_errors(out) == [], f"5.1 PARSE:\n{out!r}"
        assert _round3_ps_lines(_ROUND3_PS51, out) == expected

    def test_return_ternary_inside_braces_behaves(self):
        src = "function Get-V(){ $c=$null; return $c ? 10 : 20 }; (Get-V)"
        expected = _round3_ps_lines(_ROUND3_PS7, src)
        assert expected == ["20"]
        out = _round2_transform(src)
        assert "return$(" not in out, f"keyword glued:\n{out!r}"
        assert _round2_ps51_parse_errors(out) == [], f"5.1 PARSE:\n{out!r}"
        assert _round3_ps_lines(_ROUND3_PS51, out) == expected

    def test_paren_ternary_inside_braces_still_transformed(self):
        src = "function Get-V(){ $c=$true; return ($c ? 10 : 20) }; (Get-V)"
        expected = _round3_ps_lines(_ROUND3_PS7, src)
        out = _round2_transform(src)
        assert _round2_ps51_parse_errors(out) == [], f"5.1 PARSE:\n{out!r}"
        assert _round3_ps_lines(_ROUND3_PS51, out) == expected


@_round4_ps_skip
@pytest.mark.parametrize(
    "src",
    [
        '$x=$null; $x ??= 3; "x=$x"',                # single ??= (contrast)
        '$a=$null\n$b=7\n$x = $a ?? $b\n"x=$x"',     # single-line ?? (contrast)
        '$a="x"; $b="y"; "res: $($a || $b)"',        # interp variable operand
        '$a=@(10,20); $i=0; "v=$($a[$i])"',          # plain index (contrast)
        '$a=$null; $v = $a ?? "fb"; "v=$v"',         # ?? literal RHS statement
        '$x=$true; @($x ? 1 : 2).Count',             # ternary in array literal
        # (hashtable ternaries stay raw by design — pinned by
        # TestBashFixRound1TernaryParen::test_brace_ternary_still_skipped)
    ],
)
def test_round5_pwsh_contrast_ok(src):
    """Contrast cases: the R5 fixes must not disturb previously-correct code."""
    rc, _out, _err = _round2_run_ps(_ROUND3_PS7, src)
    if rc != 0:
        pytest.skip("not a clean PS7 ground truth")
    out = _round2_transform(src)
    assert _round2_ps51_parse_errors(out) == [], (
        f"5.1 PARSE on contrast case:\n{src!r}\n->\n{out!r}"
    )
