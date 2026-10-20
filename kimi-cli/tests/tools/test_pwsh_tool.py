"""Tests for the Powershell tool (pwsh_tool) and its enablement refactor.

Covers every function in ``kimi_cli.tools.file.bash.pwsh_tool`` plus the
refactored enablement rule: the tool is enabled whenever a PowerShell
executable exists on the platform (pwsh 7+ preferred, Windows PowerShell 5.1
as fallback) and raises ``SkipThisTool`` when neither is found.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest
from kimi_cli.session import Session
from kimi_cli.tools import SkipThisTool
from kimi_cli.tools.file.bash import shell_common
from kimi_cli.tools.file.bash.pwsh_tool import (
    _PWSH_CONSOLE_INIT,
    Powershell,
    PowershellParams,
    _common_fixed_pwsh_paths,
    _is_versioned_pwsh7_install,
    _is_windows_apps_stub,
    _print_warning,
    _pwsh_major_version,
    _where_candidates,
    find_pwsh,
)
from kosong.tooling import ToolError, ToolOk


@pytest.fixture(autouse=True)
def _clear_find_pwsh_cache():
    """Reset the ``find_pwsh`` lru_cache around every test."""
    find_pwsh.cache_clear()
    yield
    find_pwsh.cache_clear()


# ── Module-level helpers ──────────────────────────────────────────────────────


def test_print_warning_writes_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    _print_warning("something happened")
    captured = capsys.readouterr()
    assert "WARNING: something happened" in captured.err
    assert captured.out == ""


def test_pwsh_major_version_parses_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: "7\n")
    assert _pwsh_major_version("/usr/bin/pwsh") == 7


@pytest.mark.parametrize(
    "side_effect",
    [
        OSError("not found"),
        subprocess.TimeoutExpired("pwsh", 3),
        ValueError("bad int"),
    ],
)
def test_pwsh_major_version_failure_returns_none(
    monkeypatch: pytest.MonkeyPatch, side_effect: Exception
) -> None:
    def _boom(*a: Any, **k: Any) -> str:
        raise side_effect

    monkeypatch.setattr(subprocess, "check_output", _boom)
    assert _pwsh_major_version("/missing/pwsh") is None


def test_is_windows_apps_stub() -> None:
    assert _is_windows_apps_stub(r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\pwsh.exe")
    assert not _is_windows_apps_stub(r"C:\Program Files\PowerShell\7\pwsh.exe")
    assert not _is_windows_apps_stub("/usr/local/bin/pwsh")


def test_where_candidates_filters_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    result = subprocess.CompletedProcess(
        args=["where.exe", "pwsh.exe"],
        returncode=0,
        stdout=(
            r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\pwsh.exe" + "\n"
            r"C:\Program Files\PowerShell\7\pwsh.exe" + "\n"
        ),
        stderr="",
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: result)
    assert _where_candidates("pwsh.exe") == [r"C:\Program Files\PowerShell\7\pwsh.exe"]


def test_where_candidates_nonzero_return(monkeypatch: pytest.MonkeyPatch) -> None:
    result = subprocess.CompletedProcess(args=["where.exe"], returncode=1, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: result)
    assert _where_candidates("pwsh.exe") == []


def test_where_candidates_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a: Any, **k: Any) -> Any:
        raise OSError("nope")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert _where_candidates("pwsh.exe") == []


def test_common_fixed_pwsh_paths_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    paths = _common_fixed_pwsh_paths()
    assert any(p.endswith(r"PowerShell\7\pwsh.exe") for p in paths)


def test_common_fixed_pwsh_paths_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    paths = _common_fixed_pwsh_paths()
    assert "/usr/bin/pwsh" in paths


def test_is_versioned_pwsh7_install_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert _is_versioned_pwsh7_install(r"C:\Program Files\PowerShell\7\pwsh.exe")
    assert not _is_versioned_pwsh7_install(r"C:\other\pwsh.exe")


@pytest.mark.skipif(sys.platform == "win32", reason="os.path.normpath is Windows-flavoured on win32")
def test_is_versioned_pwsh7_install_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert _is_versioned_pwsh7_install("/opt/microsoft/powershell/7/pwsh")
    assert not _is_versioned_pwsh7_install("/usr/local/bin/pwsh")


def test_find_pwsh_fixed_path_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    fake = str(tmp_path / "PowerShell" / "7" / "pwsh.exe")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        "kimi_cli.tools.file.bash.pwsh_tool._common_fixed_pwsh_paths", lambda: [fake]
    )
    monkeypatch.setattr("os.path.exists", lambda p: p == fake)
    assert find_pwsh() == fake


def test_find_pwsh_via_which_with_version_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    fake = str(tmp_path / "bin" / "pwsh")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("kimi_cli.tools.file.bash.pwsh_tool._common_fixed_pwsh_paths", lambda: [])
    monkeypatch.setattr("shutil.which", lambda name: fake if name == "pwsh" else None)
    monkeypatch.setattr("os.path.exists", lambda p: p == fake)
    monkeypatch.setattr("kimi_cli.tools.file.bash.pwsh_tool._pwsh_major_version", lambda p: 7)
    assert find_pwsh() == fake


def test_find_pwsh_rejects_old_major(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    fake = str(tmp_path / "bin" / "pwsh")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("kimi_cli.tools.file.bash.pwsh_tool._common_fixed_pwsh_paths", lambda: [])
    monkeypatch.setattr("shutil.which", lambda name: fake if name == "pwsh" else None)
    monkeypatch.setattr("os.path.exists", lambda p: p == fake)
    monkeypatch.setattr("kimi_cli.tools.file.bash.pwsh_tool._pwsh_major_version", lambda p: 5)
    assert find_pwsh() is None


def test_find_pwsh_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("kimi_cli.tools.file.bash.pwsh_tool._common_fixed_pwsh_paths", lambda: [])
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert find_pwsh() is None


# ── PowershellParams ──────────────────────────────────────────────────────────


def test_params_defaults() -> None:
    p = PowershellParams(command="Get-Location")
    assert p.command == "Get-Location"
    assert p.timeout == 30
    assert p.mode == "execute"
    assert p.task_id is None
    assert p.wait_for_pattern is None


def test_params_cmd_alias() -> None:
    p = PowershellParams(cmd="Get-Date")
    assert p.command == "Get-Date"


def test_params_timeout_ms_alias_converted_to_seconds() -> None:
    p = PowershellParams(command="Get-Date", timeoutMs=90_000)
    assert p.timeout == 90


def test_params_run_in_background_maps_to_send_mode() -> None:
    p = PowershellParams(command="Get-Date", run_in_background=True)
    assert p.mode == "send"


def test_params_empty_command_rejected() -> None:
    with pytest.raises(ValueError):
        PowershellParams(command="")


def test_params_empty_command_rejected_with_task_id() -> None:
    with pytest.raises(ValueError):
        PowershellParams(command="", task_id="abc")


def test_params_task_id_with_command_ok() -> None:
    p = PowershellParams(command="Get-Date", task_id="abc")
    assert p.task_id == "abc"


# ── Static helpers ────────────────────────────────────────────────────────────


def test_validate_command_ok() -> None:
    assert Powershell._validate_command_for_ps("Get-ChildItem") is None
    assert Powershell._validate_command_for_ps('Write-Output "hello"') is None


def test_validate_command_empty() -> None:
    assert Powershell._validate_command_for_ps("") is not None
    assert Powershell._validate_command_for_ps("   ") is not None


def test_validate_command_unbalanced_quotes() -> None:
    assert Powershell._validate_command_for_ps('Write-Output "oops') is not None


def test_maybe_encode_command_short() -> None:
    cmd, param, encoded = Powershell._maybe_encode_command("Get-Date")
    assert cmd == "Get-Date"
    assert param == "-C"
    assert encoded is False


def test_maybe_encode_command_long() -> None:
    long_cmd = "Write-Output " + "x" * 9000
    cmd, param, encoded = Powershell._maybe_encode_command(long_cmd)
    assert param == "-Enc"
    assert encoded is True
    # UTF-16 LE base64 round-trip
    import pybase64 as base64

    assert base64.b64decode(cmd).decode("utf-16-le") == long_cmd


# ── Tool construction / enablement (the refactor under test) ──────────────────


def _make_tool(session: Session) -> Powershell:
    return Powershell(session)


def test_tool_enabled_when_pwsh_exists(session: Session) -> None:
    tool = _make_tool(session)
    assert tool._pwsh_path is not None or tool._pwsh_fallback_path is not None


def test_tool_skip_when_no_powershell_anywhere(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "kimi_cli.tools.file.bash.pwsh_tool.find_pwsh", lambda: None
    )
    monkeypatch.setattr(shell_common, "find_powershell_fallback", lambda: None)
    with pytest.raises(SkipThisTool):
        Powershell(session)


def test_tool_fallback_to_powershell_5_1(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "kimi_cli.tools.file.bash.pwsh_tool.find_pwsh", lambda: None
    )
    monkeypatch.setattr(
        shell_common,
        "find_powershell_fallback",
        lambda: r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    )
    tool = Powershell(session)
    assert tool._pwsh_path is None
    assert tool._pwsh_fallback_path is not None
    assert "falling back to Windows PowerShell 5.1" in capsys.readouterr().err


def test_tool_description_mentions_pid(session: Session) -> None:
    tool = _make_tool(session)
    assert str(__import__("os").getpid()) in tool.description


# ── Safety gates ──────────────────────────────────────────────────────────────


def test_hardline_blocked(session: Session) -> None:
    tool = _make_tool(session)
    result = tool._hardline_blocked("rm -rf /")
    assert isinstance(result, ToolError)
    assert "hardline" in result.message.lower()


def test_hardline_disabled_by_config(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    tool = _make_tool(session)
    tool._hardline_enabled = False
    assert tool._hardline_blocked("rm -rf /") is None


def test_self_kill_blocked(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _make_tool(session)
    blocked = tool._self_kill_blocked(f"Stop-Process -Id {__import__('os').getpid()} -Force")
    assert isinstance(blocked, ToolError)
    assert "self-kill" in blocked.message.lower()


def test_self_kill_disabled_by_config(session: Session) -> None:
    tool = _make_tool(session)
    tool._self_kill_guard_enabled = False
    assert tool._self_kill_blocked("Stop-Process -Id 1") is None


@pytest.mark.asyncio
async def test_forbidden_command_blocked(session: Session) -> None:
    tool = _make_tool(session)
    tool._forbidden_keywords = ["invoke-webrequest"]
    params = PowershellParams(command="Invoke-WebRequest http://example.com")
    result = await tool(params)
    assert isinstance(result, ToolError)
    assert "forbidden" in result.message.lower()


# ── Pattern compilation ───────────────────────────────────────────────────────


def test_compile_pattern_valid(session: Session) -> None:
    tool = _make_tool(session)
    pattern = tool._compile_pattern("hello.*")
    assert pattern is not None and pattern.search("hello world")


def test_compile_pattern_invalid(session: Session) -> None:
    tool = _make_tool(session)
    result = tool._compile_pattern("([unclosed")
    assert isinstance(result, ToolError)


def test_compile_pattern_none(session: Session) -> None:
    tool = _make_tool(session)
    assert tool._compile_pattern(None) is None


# ── Live integration (pwsh available on the dev machine) ─────────────────────


@pytest.mark.asyncio
async def test_call_simple_command(session: Session) -> None:
    tool = _make_tool(session)
    result = await tool(PowershellParams(command='Write-Output "hello-pwsh"'))
    assert isinstance(result, ToolOk)
    assert "hello-pwsh" in result.output


@pytest.mark.asyncio
async def test_call_failing_command_reports_error(session: Session) -> None:
    tool = _make_tool(session)
    result = await tool(PowershellParams(command="exit 3"))
    assert isinstance(result, ToolError)
    assert "exit_code" in result.output


@pytest.mark.asyncio
async def test_call_empty_command_rejected_early(session: Session) -> None:
    tool = _make_tool(session)
    # Bypass param validation (which rejects empty commands earlier) to reach
    # the tool's own guard.
    params = PowershellParams.model_construct(command="")
    result = await tool(params)
    assert isinstance(result, ToolError)
    assert "No command specified" in result.message


@pytest.mark.asyncio
async def test_call_hardline_blocked_without_spawning(session: Session) -> None:
    tool = _make_tool(session)
    result = await tool(PowershellParams(command="rm -rf /"))
    assert isinstance(result, ToolError)
    assert "hardline" in result.message.lower()


@pytest.mark.asyncio
async def test_call_background_mode(session: Session) -> None:
    from kimi_cli.tools.background.utils import get_all_tasks, remove_task_id

    tool = _make_tool(session)
    result = await tool(
        PowershellParams(command='Write-Output "bg-hello"', run_in_background=True)
    )
    assert isinstance(result, ToolOk)
    assert "task_id" in result.output
    # Clean up: find the task and stop it.
    tasks = get_all_tasks(session)
    for tid, stream in list(tasks.items()):
        await stream.stop()
        remove_task_id(session, tid)


@pytest.mark.asyncio
async def test_call_with_workdir(session: Session, tmp_path: Any) -> None:
    tool = _make_tool(session)
    result = await tool(
        PowershellParams(command="Write-Output (Get-Location).Path", workdir=str(tmp_path))
    )
    assert isinstance(result, ToolOk)
    assert str(tmp_path) in result.output


@pytest.mark.asyncio
async def test_call_wait_for_pattern(session: Session) -> None:
    tool = _make_tool(session)
    result = await tool(
        PowershellParams(
            command='Write-Output "marker-line"',
            wait_for_pattern="marker",
            timeout=20,
        )
    )
    assert isinstance(result, ToolOk)
    assert "marker-line" in result.output


@pytest.mark.asyncio
async def test_call_invalid_wait_pattern(session: Session) -> None:
    tool = _make_tool(session)
    result = await tool(
        PowershellParams(command="Get-Date", wait_for_pattern="([bad")
    )
    assert isinstance(result, ToolError)
    assert "Invalid wait_for_pattern" in result.message


# ── Session continuation (_continue_session) ──────────────────────────────────


@pytest.mark.asyncio
async def test_continue_session_unknown_task_id(session: Session) -> None:
    tool = _make_tool(session)
    result = await tool(PowershellParams(command="Get-Date", task_id="no-such-task"))
    assert isinstance(result, ToolError)
    assert "not found" in result.message.lower()


@pytest.mark.asyncio
async def test_interactive_session_start_continue_exit(session: Session) -> None:
    import regex as re

    from kimi_cli.tools.background.utils import get_all_tasks, remove_task_id

    tool = _make_tool(session)
    started = await tool(PowershellParams(command="", mode="interactive"))
    assert isinstance(started, ToolOk)
    match = re.search(r"task_id: `([^`]+)`", started.message)
    assert match is not None
    task_id = match.group(1)

    try:
        continued = await tool(
            PowershellParams(
                command='Write-Output "from-interactive-session"',
                task_id=task_id,
                timeout=20,
            )
        )
        assert isinstance(continued, ToolOk)
        assert "from-interactive-session" in continued.output

        ended = await tool(PowershellParams(command="exit", task_id=task_id, timeout=20))
        assert isinstance(ended, ToolOk)
    finally:
        stream = get_all_tasks(session).get(task_id)
        if stream is not None:
            await stream.stop()
            remove_task_id(session, task_id)
