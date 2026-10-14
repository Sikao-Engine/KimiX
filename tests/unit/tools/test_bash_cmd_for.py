r"""Regression tests: cmd.exe ``for [%|%]name in (list) do body`` loops.

Agents paste cmd batch ``for`` loops into the Bash tool; Git Bash rejects them
(``syntax error near unexpected token '('`` — a bare ``(`` never follows a
command word).  The scanner (``bin/kimix_native/_shell_compat.py``,
``_try_cmd_for``/``_match_cmd_for``) recognises the strict single-line form and
translates it to ``for name in list; do body; done``:

* the loop token ``%name``/``%%name`` becomes ``name`` and every *whole-word*
  body occurrence becomes ``$name`` (glued ``file_%i`` / ``D:\out\%i`` partials
  and foreign ``%OTHER%``/``%~ni`` tokens bail the whole loop);
* commas are cmd item separators and become spaces (``a,b , c`` -> ``a b c``);
* Windows-path list items get the ordinary path rewrite, and the body is
  scanned as a command context so inner ``2>nul`` / fallback rewrites and
  nested cmd-if (with a bare ``%name`` operand) compose — every edit pair is
  disjoint by construction;
* bare glob items (``*.txt``) pass through (cmd and bash both expand at run time).

Everything out of scope bails byte-for-byte (design
``.kimix_cache/cmd_control_flow_design.md`` §3.2/§4): ``/l /d /r /f`` modifiers,
parenthesised ``do (...)`` bodies, single ``&`` bodies, ``^``/``$``/``!``/quote
items or bodies, multi-line lists/bodies, a missing ``do``/empty body, and real
bash ``for`` loops.  Nested ``for`` loops with *different* variables bail
(the outer body sees the inner ``%j`` as a foreign ``%``) — a documented v1
limitation, kept loud.
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


def _sorted_edits_disjoint(source: str) -> list[tuple[int, int, str]]:
    """Run the scanner and return its edits, asserting pairwise disjointness."""
    sc = _shell_compat._BashFixScanner(source)
    sc._scan_range(0, sc.n)
    edits = sorted(sc.edits)
    for k in range(len(edits) - 1):
        assert edits[k][1] <= edits[k + 1][0], (
            f"overlapping edits {edits[k]} / {edits[k + 1]}"
        )
    return edits


# ── positive: exact structural outputs ───────────────────────────────────────
TRANSLATE_CASES = [
    pytest.param(
        "for %f in (a b) do echo %f",
        "for f in a b; do echo $f; done",
        ("for %f in (a b)",),
        id="single-pct",
    ),
    pytest.param(
        "for %%f in (a b) do echo %%f",
        "for f in a b; do echo $f; done",
        ("for %%f in (a b)",),
        id="double-pct",
    ),
    pytest.param(
        "for %%f in (a,b,c) do echo %%f",
        "for f in a b c; do echo $f; done",
        ("for %%f in (a,b,c)",),
        id="comma-items",
    ),
    pytest.param(
        "for %f in (*.txt *.md) do echo %f",
        "for f in *.txt *.md; do echo $f; done",
        ("for %f in (*.txt *.md)",),
        id="glob-items-kept",
    ),
    pytest.param(
        "for %f in (a) do echo %f %f",
        "for f in a; do echo $f $f; done",
        ("for %f in (a)",),
        id="multiple-loop-var-refs",
    ),
    pytest.param(
        "for %i in (a) do %i",
        "for i in a; do $i; done",
        ("for %i in (a)",),
        id="loop-var-as-command-word",
    ),
    pytest.param(
        "for %%i in (a) do echo %%i && echo x",
        "for i in a; do echo $i && echo x; done",
        ("for %%i in (a)",),
        id="double-ampersand-kept",
    ),
    pytest.param(
        "for %%i in (a) do echo %%i | grep x",
        "for i in a; do echo $i | grep x; done",
        ("for %%i in (a)",),
        id="pipe-kept",
    ),
    pytest.param(
        "for %f in (a) do echo %f\nnext",
        "for f in a; do echo $f; done\nnext",
        ("for %f in (a)",),
        id="body-to-newline-tail",
    ),
    pytest.param(
        "x=$(for %f in (a b) do echo %f)",
        "x=$(for f in a b; do echo $f; done)",
        ("for %f in (a b)",),
        id="inside-command-substitution",
    ),
    pytest.param(
        "bash -c 'for %f in (a) do echo %f'",
        "bash -c 'for f in a; do echo $f; done'",
        ("for %f in (a)",),
        id="inside-bash-c-inline-script",
    ),
]


@pytest.mark.parametrize(("source", "expected", "notes"), TRANSLATE_CASES)
def test_cmd_for_translated(source: str, expected: str, notes: tuple) -> None:
    result = _fix(source)
    assert result.command == expected
    assert result.cmd_conditional_fixes == notes
    assert result.changed


def test_for_path_items_compose() -> None:
    """Windows-path list items get the ordinary path rewrite (comma-free)."""
    result = _fix(r"for %f in (D:\a D:\b\c.txt) do type %f")
    assert result.command == "for f in D:/a D:/b/c.txt; do type $f; done"
    assert result.path_changes == (r"D:\a", r"D:\b\c.txt")
    assert result.cmd_conditional_fixes == (r"for %f in (D:\a D:\b\c.txt)",)


def test_for_nul_and_fallback_compose() -> None:
    result = _fix("for %f in (a) do del %f 2>nul")
    assert result.command.endswith("for f in a; do del $f 2>/dev/null; done")
    assert result.nul_fixes == ("nul",)
    assert "del" in result.replacements
    assert result.cmd_conditional_fixes == ("for %f in (a)",)


def test_for_nested_cmd_if_composes_disjoint() -> None:
    """A nested cmd-if with a bare ``%f`` operand rewrites + stays disjoint."""
    result = _fix("for %f in (a b) do if exist %f (echo f) else (echo n)")
    assert result.command == (
        "for f in a b; do if [[ -e $f ]]; then echo f; else echo n; fi; done"
    )
    assert result.cmd_conditional_fixes == ("for %f in (a b)", "if exist %f")
    _sorted_edits_disjoint(
        "for %f in (a b) do if exist %f (echo f) else (echo n)"
    )


def test_for_composite_edit_disjointness() -> None:
    """§3.2 composition: for-header, list, body var, nested if all non-overlapping."""
    _sorted_edits_disjoint(
        "for %f in (a) do if exist %f (del %f 2>nul) else (echo no)"
    )
    src = "for %i in (a) do if exist %i (x) else (y)"
    _sorted_edits_disjoint(src)


def test_app_layer_command_identical() -> None:
    for case in TRANSLATE_CASES:
        src = case.values[0]
        with patch("kimi_cli.tools.file.bash.bash_fix.sys.platform", "win32"):
            assert bash_fix.fix_bash_command(src).command == _fix(src).command


# ── negative: bails must leave the loop (or only pre-existing arg edits) ──────
BAIL_BYTE_FOR_BYTE = [
    # modifier dialects (reject v1)
    "for /l %i in (1,1,10) do echo %i",
    "for /d %i in (a) do echo %i",
    "for /r %i in (a) do echo %i",
    "for /f %i in (a) do echo %i",
    # loop-var modifiers in the body
    "for %f in (a) do echo %~nf",
    "for %f in (a) do echo %~dpf",
    # glued loop var inside a larger word
    "for %d in (1 2 3) do echo file_%d",
    # foreign % tokens (env refs) in list or body
    "for %f in (a) do echo %OTHER%",
    "for %f in (%OS%) do echo %f",
    # $ / ! / ^ in list
    "for %f in ($HOME) do echo %f",
    "for %f in (a) do echo %f ^ tail",
    "for %f in (!x) do echo %f",
    # quoted items and bodies
    'for %f in ("a b") do echo %f',
    'for %f in (a) do echo "%f"',
    # single backgrounding & in body
    "for %f in (a) do echo %f & echo x",
    # non-identifier loop token
    "for %1 in (a) do echo %1",
    "for %a b in (c) do echo x",
    # no ``do`` / empty body / glued )do
    "for %f in a b do echo %f",
    "for %f in (a) do",
    "for %f in (a) do %f; # c",
    "for %f in (a) doe echo %f",
    # parenthesised do-body (cmd group runs per item; subshell would drift)
    "for %f in (a) do (echo %f)",
    # multi-line list (paren spans a newline)
    "for %f in (a\nb) do echo %f",
    # at a non-command position (data, never code)
    'echo "for %f in (a) do echo %f"',
    "x=for",
    # real bash for loops untouched
    "for i in a b; do echo $i; done",
    "for ((i=0;i<3;i++)); do echo $i; done",
    # nested different-var loop: outer body sees the inner %j as a foreign %
    "for %i in (a) do for %j in (b) do echo %i %j",
]


@pytest.mark.parametrize("command", BAIL_BYTE_FOR_BYTE)
def test_bails_byte_for_byte(command: str) -> None:
    result = _fix(command)
    assert result.command == command, "must stay byte-for-byte"
    assert result.cmd_conditional_fixes == ()


def test_bail_keeps_only_pre_existing_arg_path_rewrite() -> None:
    r"""Glued ``%i`` inside a path argument bails the loop; only the path fires."""
    result = _fix(r"for %i in (*.log) do echo D:\out\%i")
    assert result.command == r"for %i in (*.log) do echo D:/out/%i"
    assert result.cmd_conditional_fixes == ()
    assert result.path_changes == (r"D:\out\%i",)


# ── real-bash validation ─────────────────────────────────────────────────────
@pytest.mark.parametrize("source", [c.values[0] for c in TRANSLATE_CASES])
def test_for_translations_parse_with_bash(source: str) -> None:
    fixed = _fix(source).command
    proc = subprocess.run(
        [_bash_or_skip(), "-n", "-c", fixed], capture_output=True, text=True
    )
    assert proc.returncode == 0, (source, fixed, proc.stderr)


def test_for_comma_and_space_mix_lists_items(tmp_path: Path) -> None:
    r"""``a,b , c``: commas become spaces; the extra spacing collapses via bash
    word splitting (design \u00a73.2), so the loop still lists exactly a b c."""
    result = _fix("for %%d in (a,b , c) do echo %%d")
    assert result.cmd_conditional_fixes == ("for %%d in (a,b , c)",)
    # exact spacing is not preserved (comma->space plus the original spaces);
    # assert the semantic result instead.
    proc = subprocess.run(
        [_bash_or_skip(), "--noprofile", "--norc", "-c", result.command],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["a", "b", "c"]


def test_for_backtick_body_translates_via_recursion() -> None:
    """A body backtick is bash code (design recurses the body), not a cmd-for bail."""
    result = _fix("for %f in (a) do echo `x`")
    assert result.command == "for f in a; do echo `x`; done"
    assert result.cmd_conditional_fixes == ("for %f in (a)",)


def test_for_runs_and_lists_items(tmp_path: Path) -> None:
    """The translated loop actually iterates and the glob/path items expand."""
    (tmp_path / "one.txt").write_text("1", encoding="utf-8")
    (tmp_path / "two.txt").write_text("2", encoding="utf-8")
    fixed = _fix("for %f in (*.txt) do echo %f").command
    assert fixed == "for f in *.txt; do echo $f; done"
    proc = subprocess.run(
        [_bash_or_skip(), "--noprofile", "--norc", "-c", fixed],
        capture_output=True, text=True, cwd=str(tmp_path), timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    lines = sorted(proc.stdout.split())
    assert "one.txt" in lines and "two.txt" in lines


def test_for_comma_split_runs(tmp_path: Path) -> None:
    fixed = _fix("for %%x in (a,b,c) do echo %%x").command
    proc = subprocess.run(
        [_bash_or_skip(), "--noprofile", "--norc", "-c", fixed],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["a", "b", "c"]


@pytest.mark.skipif(sys.platform != "win32", reason="cmd for idiom is Windows-targeted")
def test_for_leaks_no_nul_file(tmp_path: Path) -> None:
    fixed = _fix("for %f in (a.txt) do echo %f >nul").command
    assert fixed == "for f in a.txt; do echo $f >/dev/null; done"
    proc = subprocess.run(
        [_bash_or_skip(), "--noprofile", "--norc", "-c", fixed],
        capture_output=True, text=True, cwd=str(tmp_path), timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    leftovers = [p.name.lower() for p in tmp_path.iterdir()]
    assert "nul" not in leftovers, (
        f"empty nul file created running {fixed!r}; dir holds {leftovers}"
    )
