"""Bash-quoting repair of a ``cmd /c`` command STRING (one word -> words).

The recorded failure (Unreal Engine build through Git Bash)::

    cd /d/Temp/HarnessDemo && cmd //c "\\"D:\\Epic Games\\UE_5.8\\...\\Build.bat\\"
        HarnessDemoEditor Win64 Development -Project=\\"D:\\temp\\...\\uproject\\""

Bash reads that as ONE word whose value is the cmd.exe command string, quotes
included — but the MSYS runtime has to hand that word to a NATIVE program
(``cmd.exe``), and the only spelling it can emit for a literal ``"`` is ``\\"``.
cmd.exe reads ``\\"`` as a literal backslash plus a quote, so the program token
becomes ``\\"D:\\Epic`` and the build never starts::

    '\\"D:\\Epic Games\\...\\Build.bat\\"' is not recognized as an internal or
    external command

:mod:`kimix_native.shell_compat.cmd_command_string` re-spells the word as the
cmd-level words it always described, so bash quotes each argument for cmd.exe::

    cmd /c "D:\\Epic Games\\...\\Build.bat" HarnessDemoEditor Win64 Development
        -Project="D:\\temp\\...\\uproject" -WaitMutex -NoHotReload

These tests cover the rule at three levels: the pure rule function (edits /
splices), the facade value the Bash tool consumes (command + reports + warning),
and a real Git Bash execution of both spellings.
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
from kimix_native.shell_compat import cmd_command_string as ccs
from kimix_native.shell_compat.post_passes import windows_post_passes


def _fix(command: str, *, platform: str = "win32") -> BashFix:
    with patch.object(bash_fix.sys, "platform", platform):
        return fix_bash_command(command)


#: The recorded session command, verbatim (raw string: ``\"`` is source text).
RECORDED = (
    r'''cd /d/Temp/HarnessDemo && cmd //c "\"D:\Epic Games\UE_5.8\Engine\Build'''
    r'''\BatchFiles\Build.bat\" HarnessDemoEditor Win64 Development'''
    r''' -Project=\"D:\temp\HarnessDemo\HarnessDemo.uproject\" -WaitMutex'''
    r''' -NoHotReload" 2>&1 | tail -40'''
)


# ── 1. the rule over the IR (edits in source coordinates) ────────────────────


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # The recorded shape: the quoted program and the quoted -Project value
        # become the words the author always meant.
        (
            r'cmd //c "\"D:\Epic Games\Build.bat\" Editor -Project=\"D:\x.uproject\""',
            'cmd //c call "D:\\Epic Games\\Build.bat" Editor -Project="D:\\x.uproject"',
        ),
        # A quoted region that needs no quoting for cmd.exe still gets its
        # quotes back: they are what keeps bash from eating the backslashes.
        (
            r'cmd /c "\"\d\b.bat\""',
            'cmd /c "\\d\\b.bat"',
        ),
        # Single-quoted source word: bash already produced the command string,
        # so its ``"`` regions are re-emitted as the same words.
        (
            """cmd /c '"D:\\a b\\p.bat" one two'""",
            'cmd /c call "D:\\a b\\p.bat" one two',
        ),
        # A cmd BUILTIN as the program: the words are just words.
        (
            r'cmd /c "echo \"hi there\""',
            'cmd /c echo "hi there"',
        ),
        # ``^`` INSIDE a quoted region is a literal character to cmd.exe, so it
        # keeps its quotes and survives the split unchanged.
        (
            r'cmd /c "\"a^b\" x"',
            'cmd /c "a^b" x',
        ),
        # Two space-carrying tokens: cmd.exe would strip the outer quote pair of
        # a command string that starts with a quote, so ``call`` moves the
        # command string's first character off the quote.
        (
            r'cmd /c "\"D:\a b\prog.exe\" -P=\"D:\my temp\x.uproject\" tail"',
            'cmd /c call "D:\\a b\\prog.exe" -P="D:\\my temp\\x.uproject" tail',
        ),
        # A token with backslashes but no quotes is double-quoted for bash (an
        # unquoted backslash is an escape: ``C:\a`` would reach cmd as ``C:a``).
        (
            r'cmd /c "\"prog.bat\" -D=C:\a\b"',
            'cmd /c "prog.bat" "-D=C:\\a\\b"',
        ),
        # Only the FIRST command string in the list is a word list; a later
        # plain command keeps its spelling.
        (
            r'cmd /c "\"a b\c.bat\" x" && echo done',
            'cmd /c call "a b\\c.bat" x && echo done',
        ),
        # cmd.exe operators INSIDE the command string stay operators: they are
        # emitted as quoted bash words, reach cmd.exe unquoted, and are still
        # parsed as ``&&`` / ``>`` there.
        (
            r'cmd /c "echo \"a\" && echo \"b\""',
            'cmd /c echo "a" "&&" echo "b"',
        ),
        # A runtime cmd.exe expansion (``%VAR%``) is untouched: cmd.exe expands
        # it in the re-spelled command string exactly as it would have before.
        (
            r'cmd /c "echo \"%TEMP%\""',
            'cmd /c echo "%TEMP%"',
        ),
    ],
)
def test_rule_rewrites_quoted_command_string(source: str, expected: str) -> None:
    fix = ccs.cmd_command_string_fix(source)
    if expected == source:
        assert fix is None
        return
    assert fix is not None
    fixed, programs = fix
    assert fixed == expected
    assert programs and all(program for program in programs)


@pytest.mark.parametrize(
    "source",
    [
        # Already several words: the author spelled the command string as cmd
        # arguments, so there is no bash-only quoting to undo.
        'cmd /c "D:\\a b\\p.bat" one',
        "cmd /c call D:\\a\\b.bat one",
        # No escaped quote inside the word: cmd.exe already sees what the
        # author typed (covered end-to-end by the path rule instead).
        'cmd /c "tools\\build.bat dxc"',
        'cmd /c "dir /b"',
        # No run switch: no command string.
        "cmd /?",
        "cmd /d dir",
        # Runtime expansions: the command string is only known at run time.
        r'cmd /c "\"$prog\" x\""',
        r"cmd /c \"`prog`\" x",
        # cmd.exe's own escape character outside a quoted region: quoting the
        # token for bash would turn ``^x`` into a literal ``^x``.
        r'cmd /c "\"prog.bat\" a^b"',
        # A wider gap than one space cannot be re-emitted between two argv
        # elements.
        r'cmd /c "\"prog.bat\"  x"',
        # Unbalanced / empty program.
        r'cmd /c "\"prog.bat\""  ""',
        # A word that is not wholly quoted has no single value to re-spell (the
        # tolerant parser also reports a span that starts inside the word here).
        r'cmd /c pre"\"prog.bat\" x"',
        r'cmd /c a\ "b\"prog.bat\" x"',
        # The cmd word is an ARGUMENT here, not the command word.
        r'echo cmd /c "\"prog.bat\" x"',
        # Unrelated commands.
        "grep -rn pattern src/generated",
    ],
)
def test_rule_passes_through(source: str) -> None:
    assert ccs.cmd_command_string_fix(source) is None
    assert ccs.cmd_command_string_edits(source) == []


def test_rule_edits_are_word_spans() -> None:
    source = r'cmd //c "\"D:\a b\p.bat\" one"'
    edits = ccs.cmd_command_string_edits(source)
    assert len(edits) == 1
    start, end, replacement = edits[0]
    assert source[start:end] == r'"\"D:\a b\p.bat\" one"'
    assert replacement == 'call "D:\\a b\\p.bat" one'


def test_call_prefix_only_when_the_program_word_carries_a_space() -> None:
    """A quoted command string that starts with a quote must not rely on cmd.exe's
    "exactly two quotes + an existing executable" preserve rule.

    cmd.exe strips the outer quote pair otherwise — a filesystem fact a static
    rewriter cannot know (the file may not exist yet, or be typed wrong), and
    with three or more quoted tokens it always strips.  ``call`` starts the
    command string with a word, so nothing is stripped; a program word without a
    space needs no quotes at all, so no ``call`` is added.
    """
    spaced = ccs.cmd_command_string_fix(r'cmd /c "\"D:\a b\p.bat\" one"')
    assert spaced is not None
    assert spaced[0] == 'cmd /c call "D:\\a b\\p.bat" one'

    plain = ccs.cmd_command_string_fix(r'cmd /c "\"p.bat\" one"')
    assert plain is not None
    assert plain[0] == 'cmd /c "p.bat" one'


def test_rule_reports_the_program_word() -> None:
    fix = ccs.cmd_command_string_fix(r'cmd /c "\"D:\a b\p.bat\" one"')
    assert fix is not None
    assert fix[1] == ("D:\\a b\\p.bat",)


# ── 2. the facade value the Bash tool consumes ───────────────────────────────


def test_facade_repairs_the_recorded_command() -> None:
    result = _fix(RECORDED)
    assert result.command == (
        'cd D:/Temp/HarnessDemo && cmd /c call '
        '"D:\\Epic Games\\UE_5.8\\Engine\\Build\\BatchFiles\\Build.bat" '
        'HarnessDemoEditor Win64 Development '
        '-Project="D:\\temp\\HarnessDemo\\HarnessDemo.uproject" '
        "-WaitMutex -NoHotReload 2>&1 | tail -40"
    )
    assert result.cmd_command_string_fixes == (
        "D:\\Epic Games\\UE_5.8\\Engine\\Build\\BatchFiles\\Build.bat",
    )
    # The doubled-slash workaround spelling is normalized by the flag pass.
    assert result.native_flag_fixes == ("//c",)
    assert result.changed is True
    # The warning explains WHY the command string had to change.
    assert "cmd /c" in result.warning
    assert "re-split" in result.warning.lower()


def test_facade_keeps_the_path_notes_for_other_words() -> None:
    result = _fix(RECORDED)
    # ``/d/Temp/HarnessDemo`` is still a Windows-path rewrite; the command
    # string's own path is not (it is a cmd.exe path and keeps its spelling).
    assert result.path_changes == ("/d/Temp/HarnessDemo",)
    assert result.cmd_path_fixes == ()


@pytest.mark.parametrize(
    "command",
    [
        'cmd /c "tools\\build.bat dxc"',
        'cmd /c "dir /b"',
        "cmd /?",
        r"cmd /c %~dp0tools\build.bat",
        "echo cmd /c tools/build.bat",
        r'cmd /c "\"$prog\" x"',
    ],
)
def test_facade_passthrough_is_byte_for_byte(command: str) -> None:
    result = _fix(command)
    assert result == BashFix(command)
    assert result.command is command or result.command == command
    assert result.cmd_command_string_fixes == ()


def test_non_windows_platform_keeps_the_command() -> None:
    assert _fix(RECORDED, platform="linux") == BashFix(RECORDED)


def test_post_passes_are_no_ops_off_windows() -> None:
    fix = BashFix(RECORDED)
    assert windows_post_passes(fix, platform="linux") is fix


def test_post_passes_keep_identity_when_nothing_changes() -> None:
    fix = BashFix("grep -rn x src")
    assert windows_post_passes(fix, platform="win32") is fix


def test_post_passes_order_normalizes_then_re_splits_then_paths() -> None:
    # ``cmd //c`` is normalized by the flag pass, then the command string is
    # re-split, then the path rule sees plain words (here: a relative path
    # inside the command string, which it quotes for cmd.exe).
    result = windows_post_passes(
        BashFix(r'cmd //c "\"tools/build.bat\" dxc"'), platform="win32"
    )
    assert result.command == 'cmd /c "tools\\build.bat" dxc'
    assert result.native_flag_fixes == ("//c",)
    assert result.cmd_command_string_fixes == ("tools/build.bat",)
    assert result.cmd_path_fixes == ('"tools/build.bat"',)


def test_repair_is_idempotent() -> None:
    once = _fix(RECORDED).command
    assert _fix(once).command == once


def test_facade_repairs_behind_a_degraded_tiling() -> None:
    """A ``case`` pattern quirk degrades the following tiling; the shared walk
    still finds the command string."""
    result = _fix('case $1 in a) x;; esac\ncmd /c "\\"a b/c.bat\\" x"')
    assert result.command.splitlines()[-1] == 'cmd /c call "a b/c.bat" x'
    assert result.cmd_command_string_fixes == ("a b/c.bat",)


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
        # ``cmd`` without a recognized switch opens an interactive shell; with
        # no stdin it exits immediately instead of blocking the test run.
        stdin=subprocess.DEVNULL,
    )


def _program(tmp_path: pathlib.Path, name: str = "sp ace/build.bat") -> pathlib.Path:
    script = tmp_path / name
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_bytes(
        b"@echo off\r\necho build-ok\r\necho prog=[%~0]\r\n"
        b"echo args=[%*]\r\nexit /b 0\r\n"
    )
    return script


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe is Windows-only")
def test_quoted_command_string_runs_after_the_fix(tmp_path: pathlib.Path) -> None:
    """The recorded failure: one double-quoted command string with ``\\"``."""
    script = _program(tmp_path)
    broken = f'cmd /c "\\"{script}\\" flag one"'
    before = _run_bash(broken, tmp_path)
    assert before.returncode != 0

    fixed = _fix(broken).command
    after = _run_bash(fixed, tmp_path)
    assert after.returncode == 0, after.stderr
    assert "build-ok" in after.stdout
    assert "flag one" in after.stdout
    # cmd.exe received the program as ONE token, space included.
    assert f"prog=[{script}]" in after.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe is Windows-only")
def test_quoted_argument_value_runs_after_the_fix(tmp_path: pathlib.Path) -> None:
    """A quoted VALUE with spaces forces the ``call`` spelling (cmd.exe would
    otherwise strip the outer quote pair of the command string)."""
    script = _program(tmp_path)
    broken = f'cmd /c "\\"{script}\\" -Project=\\"{tmp_path / "my temp" / "x.uproject"}\\""'
    before = _run_bash(broken, tmp_path)
    assert before.returncode != 0

    fixed = _fix(broken).command
    assert "call " in fixed
    after = _run_bash(fixed, tmp_path)
    assert after.returncode == 0, after.stderr
    assert "build-ok" in after.stdout
    assert "-Project=" in after.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe is Windows-only")
def test_single_quoted_command_string_runs_after_the_fix(
    tmp_path: pathlib.Path,
) -> None:
    """A single-quoted command string (already a plain cmd.exe spelling)."""
    script = _program(tmp_path, "tools/build.bat")
    broken = f"""cmd /c '"{script}" one'"""
    before = _run_bash(broken, tmp_path)
    assert before.returncode != 0

    fixed = _fix(broken).command
    after = _run_bash(fixed, tmp_path)
    assert after.returncode == 0, after.stderr
    assert "build-ok" in after.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe is Windows-only")
def test_operator_chain_runs_after_the_fix(tmp_path: pathlib.Path) -> None:
    """cmd.exe operators inside the command string stay operators."""
    script = _program(tmp_path)
    broken = f'cmd /c "\\"{script}\\" one && echo chained"'
    before = _run_bash(broken, tmp_path)
    assert before.returncode != 0

    fixed = _fix(broken).command
    after = _run_bash(fixed, tmp_path)
    assert after.returncode == 0, after.stderr
    assert "build-ok" in after.stdout
    assert "chained" in after.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe is Windows-only")
def test_redirection_inside_the_command_string_survives(
    tmp_path: pathlib.Path,
) -> None:
    """A ``>`` inside the command string is re-emitted as a quoted bash word, but
    reaches cmd.exe unquoted and still redirects."""
    script = _program(tmp_path)
    target = tmp_path / "out put.txt"
    broken = f'cmd /c "\\"{script}\\" one > \\"{target}\\""'
    before = _run_bash(broken, tmp_path)
    assert before.returncode != 0
    assert not target.exists()

    fixed = _fix(broken).command
    after = _run_bash(fixed, tmp_path)
    assert after.returncode == 0, after.stderr
    assert target.exists()
    assert "build-ok" in target.read_text(encoding="utf-8", errors="replace")
