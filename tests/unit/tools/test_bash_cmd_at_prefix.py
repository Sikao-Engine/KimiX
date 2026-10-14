r"""Tests for the cmd.exe leading ``@`` handling at a Bash command position.

A command word starting with ``@`` is a guaranteed error on Git Bash (no ``@*``
executable exists on the default PATH; the only ``@*`` files are non-executable
system toast icons), so the scanner may rewrite it (design
``.kimix_cache/cmd_control_flow_design.md`` §3.4):

* ``@echo off`` / ``@echo on`` -> ``set +x`` / ``set -x`` (the cmd echo-visibility
  switch's bash counterpart), reported in ``cmd_conditional_fixes``;
* any other ``@word`` -> the leading ``@`` is deleted and the remainder is
  re-dispatched as a command word, so ``@if ...``/``@for ...`` compose with the
  control-flow hooks (with provably disjoint strip/header edits) and ``@rev``
  still gets its fallback prefix.

Bails (byte-for-byte): bare ``@``, ``@@x``, a quoted-leading ``@`` (``"@"dir``),
and the cmd ``@echo.`` blank-line idiom (kept loud rather than stripped to 127
``echo.``).  ``echo off`` (no ``@``) is valid bash and is never touched.

EPOCH-2 NOTE (deviation from the design §5 example list): the §3.4 prose says a
non-``off``/``on`` ``@echo`` "bails to the generic strip", and §1's ground rule
permits turning one guaranteed error into another; the scanner therefore strips
``@ECHO OFF``/``@echo hello`` to ``ECHO OFF``/``echo hello`` (both were 127
errors; ``echo hello`` now faithfully echoes as cmd would).  §5 listed those two
as bare "bails"; this prose-driven choice is recorded in the epoch deliverable.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from kimi_cli.tools.file.bash import bash_fix
from kimi_cli.native_loader import get_compat as _get_compat

_shell_compat = _get_compat("_shell_compat")


def _fix(command: str):
    return _shell_compat.fix_bash_command(command)


def _bash_or_skip() -> str:
    from kimi_cli.tools.file.bash.bash_tool import find_bash

    bash = find_bash()
    if bash is None:
        pytest.skip("no bash executable on this host")
    return str(bash)


# ── @echo off|on ─────────────────────────────────────────────────────────────
ECHO_CASES = [
    ("@echo off", "set +x"),
    ("@echo on", "set -x"),
    ("@echo off >nul", "set +x >/dev/null"),
    ("@echo off>nul", "set +x>/dev/null"),
    ("@echo off; dir", "set +x; dir"),
    ("@echo off && dir", "set +x && dir"),
    ("@echo off\nfoo", "set +x\nfoo"),
    ("@echo off | cat", "set +x | cat"),
]


@pytest.mark.parametrize(("source", "expected"), ECHO_CASES)
def test_at_echo_off_on(source: str, expected: str) -> None:
    result = _fix(source)
    assert result.command == expected
    assert "set +x" in expected or "set -x" in expected
    # @echo off|on is reported as a translated control-flow construct
    assert any(w.startswith("@echo") for w in result.cmd_conditional_fixes)
    assert result.changed


def test_at_echo_boundary_after_word() -> None:
    """A glued trailing word or a single backgrounding & is not the off/on idiom."""
    # ``@echo offline``: next word isn't off/on -> generic strip (deviation note)
    assert _fix("@echo offline").command == "echo offline"
    # ``@echo off & dir``: single & boundary -> generic strip
    assert _fix("@echo off & dir").command == "echo off & dir"
    assert _fix("@echo off & dir").cmd_conditional_fixes == ()


# ── generic leading-@ strip ──────────────────────────────────────────────────
STRIP_CASES = [
    "@dir x",
    "@type foo.txt",
    "@ping 1.1.1.1",
]


@pytest.mark.parametrize("source", STRIP_CASES)
def test_generic_strip(source: str) -> None:
    result = _fix(source)
    assert result.command == source[1:], "the leading @ is deleted"
    assert result.cmd_conditional_fixes == ()  # strip records nothing new


def test_strip_then_nested_control_flow_composes() -> None:
    """@if/@for strip is disjoint from the header edits that follow it."""
    assert _fix(r"@if exist P (A) else (B)").command == (
        "if [[ -e P ]]; then A; else B; fi"
    )
    assert _fix("@for %f in (a) do echo %f").command == (
        "for f in a; do echo $f; done"
    )


def test_strip_then_fallback_prefix_composes() -> None:
    result = _fix("@rev")
    # after the strip the bare ``rev`` word resolves to the fallback function
    assert result.command.splitlines()[-1] == "rev"
    assert "rev" in result.replacements
    # the @ deletion edit and the fallback definition coexist (disjoint)
    assert "@rev" not in result.command


def test_strip_disjoint_edits_no_corruption() -> None:
    """The (ws, ws+1) strip never overlaps the re-dispatched command edits."""
    r = _fix(r"@if exist D:\a (dir D:\a 2>nul)")
    assert r.command == (
        r"if [[ -e D:/a ]]; then dir D:/a 2>/dev/null; fi"
    )
    assert r.nul_fixes == ("nul",)
    assert r.path_changes == (r"D:\a", r"D:\a")


# ── bails (byte-for-byte) ─────────────────────────────────────────────────────
BAIL_CASES = [
    "@",             # bare @
    "@@x",           # @@ prefix
    '"@"dir',        # quoted-leading @
    "'@'dir",
    "@echo.",        # cmd blank-line echo idiom
    "echo off",      # no @: valid bash, never touched
    "echo on",
    "x @ y",         # @ mid-arguments (not a command word)
    "echo a @ b",
    "$@",            # not a command position
    'echo "${arr[@]}"',
    'arr=(@foo)',    # @ inside array data
]


@pytest.mark.parametrize("source", BAIL_CASES)
def test_at_bails_byte_for_byte(source: str) -> None:
    result = _fix(source)
    assert result.command == source
    assert result.cmd_conditional_fixes == ()


def test_at_prefix_is_command_position_only() -> None:
    """A @ that is an argument is left alone; a @ at a fresh command line is not."""
    assert _fix("git add @file").command == "git add @file"  # arg (git@file syntax)
    assert _fix("cd @dir").command == "cd @dir"
    # but as a command word it strips:
    assert _fix("; @dir").command == "; dir"
    assert _fix("&& @dir").command == "&& dir"


# ── real bash ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("source", ["@echo off", "@echo on", "@dir"])
def test_at_outputs_parse_with_bash(source: str) -> None:
    fixed = _fix(source).command
    proc = subprocess.run(
        [_bash_or_skip(), "-n", "-c", fixed], capture_output=True, text=True
    )
    assert proc.returncode == 0, (source, fixed, proc.stderr)


@pytest.mark.skipif(sys.platform != "win32", reason="cmd @ idiom is Windows-targeted")
def test_at_echo_off_runs_silently(tmp_path: Path) -> None:
    """set +x is a silent no-op under bash -c, like cmd @echo off."""
    fixed = _fix("@echo off & echo hello").command  # strip -> echo off & echo hello
    assert fixed == "echo off & echo hello"
    proc = subprocess.run(
        [_bash_or_skip(), "--noprofile", "--norc", "-c", _fix("@echo off; echo hello").command],
        capture_output=True, text=True, cwd=str(tmp_path), timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "hello" in proc.stdout
    leftovers = [p.name.lower() for p in tmp_path.iterdir()]
    assert "nul" not in leftovers


def test_app_layer_command_identical() -> None:
    for src in [e[0] for e in ECHO_CASES] + STRIP_CASES:
        with patch("kimi_cli.tools.file.bash.bash_fix.sys.platform", "win32"):
            assert bash_fix.fix_bash_command(src).command == _fix(src).command
