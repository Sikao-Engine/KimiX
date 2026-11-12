"""cmd.exe command-string path repair (``cmd /c tools/build.bat``).

cmd.exe splits its command string at every ``/``, so a POSIX-spelled relative
path is read as a program plus a switch::

    cmd.exe /c .kimix_cache/tools/build.bat dxc
    '.kimix_cache' is not recognized as an internal or external command

and the batch script never runs.  :mod:`kimix_native.shell_compat.cmd_script_paths`
rewrites the words cmd.exe must resolve as paths to their quoted backslash
spelling (``cmd /c ".kimix_cache\\tools\\build.bat" dxc``) — quoted because bash
removes bare backslashes as escapes before cmd.exe ever sees them.

These tests cover the rule at three levels: the pure rule function (edits /
splices), the facade value the Bash tool consumes (command + reports), and a
real Git Bash execution of the reported failure shape.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
from unittest.mock import patch

import pytest

from kimi_cli.tools.file.bash import bash_fix
from kimi_cli.tools.file.bash.bash_fix import BashFix, fix_bash_command
from kimi_cli.tools.file.bash.bash_tool import find_bash
from kimix_native.shell_compat import cmd_script_paths as csp
from kimix_native.shell_compat.post_passes import windows_post_passes


def _fix(command: str, *, platform: str = "win32") -> BashFix:
    with patch.object(bash_fix.sys, "platform", platform):
        return fix_bash_command(command)


# ── 1. the rule over the IR (edits in source coordinates) ────────────────────


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("cmd /c tools/build.bat dxc", 'cmd /c "tools\\build.bat" dxc'),
        (
            "cmd.exe /c .kimix_cache/tools/build.bat",
            'cmd.exe /c ".kimix_cache\\tools\\build.bat"',
        ),
        # The flag pass normalizes ``//c`` later in the pipeline; the path
        # rule accepts both spellings and only rewrites the word.
        ("cmd //c tools/build.bat", 'cmd //c "tools\\build.bat"'),
        ("cmd /c call tools/run.cmd", 'cmd /c call "tools\\run.cmd"'),
        ("cmd /c dir .kimix_cache/tools", 'cmd /c dir ".kimix_cache\\tools"'),
        ("cmd /s /c tools/build.bat", 'cmd /s /c "tools\\build.bat"'),
        ("x=$(cmd /c a/b.bat)", 'x=$(cmd /c "a\\b.bat")'),
        ('cmd /c "./tools/build.bat" dxc', 'cmd /c ".\\tools\\build.bat" dxc'),
        (
            "if cmd /c x/y.bat; then echo y; fi",
            'if cmd /c "x\\y.bat"; then echo y; fi',
        ),
        ("cmd /c tool/name.exe --flag=x/y", 'cmd /c "tool\\name.exe" --flag=x/y'),
        # Builtins that take TWO path operands (switches may sit between).
        (
            "cmd /c copy /b a/b.txt c/d.txt",
            'cmd /c copy /b "a\\b.txt" "c\\d.txt"',
        ),
        # ``start`` keeps its operands; an explicit title word is not a path.
        (
            'cmd /c start "" tools/run.exe',
            'cmd /c start "" "tools\\run.exe"',
        ),
        # Every cmd.exe command node in the list is handled, not just the first.
        ("cmd /c echo ok && cmd /c run/x.cmd", 'cmd /c echo ok && cmd /c "run\\x.cmd"'),
    ],
)
def test_rule_rewrites_relative_paths(source: str, expected: str) -> None:
    fix = csp.cmd_script_path_fix(source)
    if expected == source:
        assert fix is None
        return
    assert fix is not None
    fixed, words = fix
    assert fixed == expected
    assert words and all(word in source for word in words)


@pytest.mark.parametrize(
    "source",
    [
        # A quoted command STRING is already cmd syntax.
        r'cmd /c "tools\build.bat dxc"',
        # Switches and data words are not paths.
        "cmd /c echo a/b",
        "cmd /?",
        "cmd /d dir",
        "cmd /c dir /s /b",
        # Drive-absolute paths: cmd.exe resolves forward slashes there.
        "cmd /c C:/dev/x/y.bat",
        # Runtime expansions and globs cannot be re-emitted statically.
        r"cmd /c %~dp0tools\build.bat",
        "cmd /c tools/*.bat",
        "cmd /c %TEMP%/x.bat",
        # bash-only single-quote quoting: the quotes would reach cmd.exe.
        "cmd /c 'a b/c.bat'",
        # ``start`` reads its first quoted operand as the window TITLE, so its
        # operands keep their spelling (quoting would run nothing).
        "cmd /c start tools/run.exe",
        # The cmd word is an ARGUMENT here, not the command word.
        "echo cmd /c tools/build.bat",
        # No run switch: nothing to do.
        "cmd /c",
        # Unrelated commands.
        "grep -rn pattern src/generated",
    ],
)
def test_rule_passes_through(source: str) -> None:
    assert csp.cmd_script_path_fix(source) is None
    assert csp.cmd_script_path_edits(source) == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # A ``case`` pattern quirk degrades the tiling of the FOLLOWING simple
        # command into a pipeline of bare WORD nodes; the rule regroups it.
        (
            "case $1 in a) x;; esac\ncmd /c z/w.bat",
            'case $1 in a) x;; esac\ncmd /c "z\\w.bat"',
        ),
        (
            "case $1 in *) break;; esac\nrev C:/a && cmd /c z/w.bat",
            'case $1 in *) break;; esac\nrev C:/a && cmd /c "z\\w.bat"',
        ),
    ],
)
def test_rule_handles_degraded_tilings(source: str, expected: str) -> None:
    fix = csp.cmd_script_path_fix(source)
    assert fix is not None
    assert fix[0] == expected


def test_facade_repairs_cmd_behind_a_fallback_prelude() -> None:
    """The fallback prelude contains a ``case``: the tiling degrades, the repair
    must still reach the user's ``cmd`` line."""
    result = _fix("rev C:/a && cmd /c z/w.bat")
    assert result.command.splitlines()[-1] == 'rev C:/a && cmd /c "z\\w.bat"'
    assert result.cmd_path_fixes == ("z/w.bat",)


def test_rule_edits_are_word_spans() -> None:
    source = "cmd /c tools/build.bat dxc"
    edits = csp.cmd_script_path_edits(source)
    assert len(edits) == 1
    start, end, replacement = edits[0]
    assert source[start:end] == "tools/build.bat"
    assert replacement == '"tools\\build.bat"'


# ── 2. the facade value the Bash tool consumes ───────────────────────────────


@pytest.mark.parametrize(
    ("source", "expected", "fixes"),
    [
        ("cmd /c tools/build.bat dxc", 'cmd /c "tools\\build.bat" dxc', ("tools/build.bat",)),
        # The recorded session command.
        (
            "cd /d/DirectXShaderCompiler && cmd.exe /c .kimix_cache/tools/build.bat"
            " dxc 2>&1 | tail -25",
            'cd D:/DirectXShaderCompiler && cmd.exe /c ".kimix_cache\\tools\\build.bat"'
            " dxc 2>&1 | tail -25",
            (".kimix_cache/tools/build.bat",),
        ),
        ("cmd //c tools/build.bat", 'cmd /c "tools\\build.bat"', ("tools/build.bat",)),
    ],
)
def test_facade_reports_the_rewrite(
    source: str, expected: str, fixes: tuple[str, ...]
) -> None:
    result = _fix(source)
    assert result.command == expected
    assert result.cmd_path_fixes == fixes
    assert result.changed
    # The warning surfaces the reason to the model.
    assert "backslash" in result.warning
    assert "cmd /c" in result.warning


@pytest.mark.parametrize(
    "command",
    [
        r'cmd /c "tools\build.bat dxc"',
        "cmd /c echo a/b",
        "cmd /c C:/dev/x/y.bat",
        "cmd /?",
        r"cmd /c %~dp0tools\build.bat",
        "cmd /c 'a b/c.bat'",
        "echo cmd /c tools/build.bat",
    ],
)
def test_facade_passthrough_is_byte_for_byte(command: str) -> None:
    result = _fix(command)
    assert result == BashFix(command)
    assert result.command is command or result.command == command
    assert result.cmd_path_fixes == ()


def test_windows_relative_spelling_is_requoted() -> None:
    """``cmd /c .\\sub\\t.bat`` looks right but bash eats the backslashes."""
    result = _fix(r"cmd.exe /c .\sub\t.bat dxc")
    assert result.command == 'cmd.exe /c ".\\sub\\t.bat" dxc'
    assert result.cmd_path_fixes == ("./sub/t.bat",)
    # The forward-slash claim of the earlier normalization no longer holds.
    assert result.path_changes == ()


def test_non_windows_platform_keeps_the_command() -> None:
    command = "cmd /c tools/build.bat"
    assert _fix(command, platform="linux") == BashFix(command)


def test_post_passes_are_no_ops_off_windows() -> None:
    fix = BashFix("cmd /c tools/build.bat")
    assert windows_post_passes(fix, platform="linux") is fix


def test_post_passes_keep_identity_when_nothing_changes() -> None:
    fix = BashFix("grep -rn x src")
    assert windows_post_passes(fix, platform="win32") is fix


def test_post_passes_order_normalizes_then_rewrites() -> None:
    # ``cmd //c`` is normalized by the flag pass, then the command-string rule
    # rewrites the relative path; both reports are kept.
    result = windows_post_passes(BashFix("cmd //c tools/build.bat"), platform="win32")
    assert result.command == 'cmd /c "tools\\build.bat"'
    assert result.native_flag_fixes == ("//c",)
    assert result.cmd_path_fixes == ("tools/build.bat",)


# ── 3. real Git Bash execution ───────────────────────────────────────────────


def _run_bash(command: str, cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
    bash = find_bash()
    assert bash is not None
    return subprocess.run(
        [bash, "-c", command],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        errors="replace",  # cmd.exe's localized errors are not UTF-8
        timeout=120,
        check=False,
    )


def test_relative_batch_path_runs_after_the_fix(tmp_path: pathlib.Path) -> None:
    """The reported failure: ``cmd /c .kimix_cache/tools/build.bat``.

    The POSIX-spelled relative path is read by cmd.exe as a switch; after the
    rewrite the same command runs the batch file.
    """
    if sys.platform != "win32":
        pytest.skip("cmd.exe is Windows-only")
    tools = tmp_path / ".kimix_cache" / "tools"
    tools.mkdir(parents=True)
    (tools / "build.bat").write_bytes(b"@echo off\r\necho build-ok\r\nexit /b 0\r\n")

    broken = "cmd.exe /c .kimix_cache/tools/build.bat"
    before = _run_bash(broken, tmp_path)
    assert before.returncode != 0

    fixed = _fix(broken).command
    assert fixed == 'cmd.exe /c ".kimix_cache\\tools\\build.bat"'
    after = _run_bash(fixed, tmp_path)
    assert after.returncode == 0, after.stderr
    assert "build-ok" in after.stdout


def test_backslash_spelling_runs_after_the_fix(tmp_path: pathlib.Path) -> None:
    """A Windows-style relative path is quoted so bash cannot eat the ``\\``."""
    if sys.platform != "win32":
        pytest.skip("cmd.exe is Windows-only")
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "greet.bat").write_bytes(b"@echo off\r\necho greeted %1\r\nexit /b 0\r\n")

    fixed = _fix(r"cmd.exe /c .\tools\greet.bat world").command
    assert fixed == 'cmd.exe /c ".\\tools\\greet.bat" world'
    result = _run_bash(fixed, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "greeted world" in result.stdout
