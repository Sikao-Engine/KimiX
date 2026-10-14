r"""Regression tests: the generalized cmd.exe ``if`` header (defined/errorlevel/
compare/arithmetic).

The landed fix translated ``if [not] exist P (A) else (B)`` (see
``tests/unit/tools/test_bash_cmd_conditional.py``). Epoch 2 generalizes the
header predicate — reusing the *identical* parenthesised-branch machinery — to
four more cmd condition kinds, all of which are guaranteed Git Bash syntax
errors at a command position (a command word is never followed by a bare ``(``):

* ``if [not] defined V (A) [else (B)]``   -> ``if [[ [!] -n ${V+x} ]]``
* ``if [not] errorlevel N (A) [else (B)]`` -> ``if (( [!] $? (>=|<) N ))``
* ``if [not] [/i] X==Y (...)``             -> ``if [[ [!] SIDE == "SIDE" ]]``
* ``if [not] [/i] LHS rel RHS (...)``      -> ``if (( [!] (LHS op RHS) ))``
  with ``rel`` in ``equ neq lss leq gtr geq`` (spaced, the only form cmd
  accepts for the word operators; glued ``5geq3`` is invalid cmd — probed).

``/i`` folds case (``${X,,}`` for expansions, lowercased literals).  Every
conservative bail leaves the whole conditional byte-for-byte: non-bare
``defined``/non-integer ``errorlevel`` operands, quote-asymmetric or
forbidden-character compare sides, glued-``%VAR%`` partials, delayed
expansion, uppercase keywords / relational operators, ``/i`` before
exist/defined/errorlevel, and every branch-level bail the landed matcher
already applies.

Design: ``.kimix_cache/cmd_control_flow_design.md`` §1 (rows 2-4), §3.1.
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
BashFix = _shell_compat.BashFix


def _fix(command: str) -> BashFix:
    return _shell_compat.fix_bash_command(command)


def _fix_app_layer(command: str) -> BashFix:
    with patch("kimi_cli.tools.file.bash.bash_fix.sys.platform", "win32"):
        return bash_fix.fix_bash_command(command)


def _bash_or_skip() -> str:
    from kimi_cli.tools.file.bash.bash_tool import find_bash

    bash = find_bash()
    if bash is None:
        pytest.skip("no bash executable on this host")
    return str(bash)


# ── positive: exact structural outputs ───────────────────────────────────────
TRANSLATE_CASES = [
    # ── defined ──────────────────────────────────────────────────────────
    pytest.param(
        "if defined V (echo set)",
        "if [[ -n ${V+x} ]]; then echo set; fi",
        ("if defined V",),
        id="defined-no-else",
    ),
    pytest.param(
        "if defined V (echo set) else (echo unset)",
        "if [[ -n ${V+x} ]]; then echo set; else echo unset; fi",
        ("if defined V",),
        id="defined-else",
    ),
    pytest.param(
        "if not defined PYTHONHOME (echo unset)",
        "if [[ ! -n ${PYTHONHOME+x} ]]; then echo unset; fi",
        ("if not defined PYTHONHOME",),
        id="defined-negated",
    ),
    pytest.param(
        "if defined A (x) else if defined B (y)",
        "if [[ -n ${A+x} ]]; then x; elif [[ -n ${B+x} ]]; then y; fi",
        ("if defined A",),
        id="defined-elif-chain",
    ),
    # ── errorlevel ───────────────────────────────────────────────────────
    pytest.param(
        "if errorlevel 1 (echo failed) else (echo ok)",
        "if (( $? >= 1 )); then echo failed; else echo ok; fi",
        ("if errorlevel 1",),
        id="errorlevel",
    ),
    pytest.param(
        "if errorlevel 1 (echo failed)",
        "if (( $? >= 1 )); then echo failed; fi",
        ("if errorlevel 1",),
        id="errorlevel-no-else",
    ),
    pytest.param(
        "if not errorlevel 2 (echo fine) else (echo bad)",
        "if (( $? < 2 )); then echo fine; else echo bad; fi",
        ("if not errorlevel 2",),
        id="errorlevel-negated",
    ),
    pytest.param(
        "false && if errorlevel 1 (echo failed) else (echo ok)",
        "false && if (( $? >= 1 )); then echo failed; else echo ok; fi",
        ("if errorlevel 1",),
        id="errorlevel-after-andthen",
    ),
    # ── string compare (glued ==) ────────────────────────────────────────
    pytest.param(
        'if "%V%"=="x" (echo a) else (echo b)',
        'if [[ ${V} == "x" ]]; then echo a; else echo b; fi',
        ('if "%V%"=="x"',),
        id="compare-quoted-expansion",
    ),
    pytest.param(
        'if "%BUILD%"=="release" (echo rel) else (echo dbg)',
        'if [[ ${BUILD} == "release" ]]; then echo rel; else echo dbg; fi',
        ('if "%BUILD%"=="release"',),
        id="compare-build",
    ),
    pytest.param(
        "if X==Y (a)",
        'if [[ "X" == "Y" ]]; then a; fi',
        ("if X==Y",),
        id="compare-unquoted-literals",
    ),
    pytest.param(
        "if %errorlevel%==0 (echo ok)",
        'if [[ $? == "0" ]]; then echo ok; fi',
        ("if %errorlevel%==0",),
        id="compare-errorlevel-glued",
    ),
    pytest.param(
        'if not "%V%"=="x" (a)',
        'if [[ ! ${V} == "x" ]]; then a; fi',
        ('if not "%V%"=="x"',),
        id="compare-negated",
    ),
    # ── /i case folding ──────────────────────────────────────────────────
    pytest.param(
        'if /i "%V%"=="xy" (echo eq) else (echo ne)',
        'if [[ ${V,,} == "xy" ]]; then echo eq; else echo ne; fi',
        ('if /i "%V%"=="xy"',),
        id="compare-slash-i-fold",
    ),
    pytest.param(
        'if /i not "a"=="b" (a)',
        'if [[ ! "a" == "b" ]]; then a; fi',
        ('if /i not "a"=="b"',),
        id="compare-slash-i-not-literals",
    ),
    # ── arithmetic (spaced word operators) ───────────────────────────────
    pytest.param(
        "if 5 geq 3 (echo big) else (echo small)",
        "if (( 5 >= 3 )); then echo big; else echo small; fi",
        ("if 5 geq 3",),
        id="arith-geq",
    ),
    pytest.param(
        "if %errorlevel% neq 0 (echo bad)",
        "if (( $? != 0 )); then echo bad; fi",
        ("if %errorlevel% neq 0",),
        id="arith-errorlevel-neq",
    ),
    pytest.param(
        "if 1 equ 1 (echo same)",
        "if (( 1 == 1 )); then echo same; fi",
        ("if 1 equ 1",),
        id="arith-equ",
    ),
    pytest.param(
        "if 2 lss 3 (a) else (b)",
        "if (( 2 < 3 )); then a; else b; fi",
        ("if 2 lss 3",),
        id="arith-lss",
    ),
    pytest.param(
        "if 2 leq 3 (a)",
        "if (( 2 <= 3 )); then a; fi",
        ("if 2 leq 3",),
        id="arith-leq",
    ),
    pytest.param(
        "if 4 gtr 3 (a)",
        "if (( 4 > 3 )); then a; fi",
        ("if 4 gtr 3",),
        id="arith-gtr",
    ),
    pytest.param(
        "if not 5 lss 3 (a)",
        "if (( ! (5 < 3 ) )); then a; fi",
        ("if not 5 lss 3",),
        id="arith-negated",
    ),
    pytest.param(
        "if %V% geq 3 (a)",
        "if (( ${V} >= 3 )); then a; fi",
        ("if %V% geq 3",),
        id="arith-expansion-side",
    ),
    # ── mixed-kind else-if chain (design §3.1 worked example) ─────────────
    pytest.param(
        "if defined OUT (mkdir OUT) else if errorlevel 1 (echo failed)",
        "if [[ -n ${OUT+x} ]]; then mkdir OUT; elif (( $? >= 1 )); then echo failed; fi",
        ("if defined OUT",),
        id="mixed-chain-defined-then-elif-errorlevel",
    ),
    pytest.param(
        "if exist A (x) else if defined B (y) else if errorlevel 1 (z) else (w)",
        "if [[ -e A ]]; then x; elif [[ -n ${B+x} ]]; then y; "
        "elif (( $? >= 1 )); then z; else w; fi",
        ("if exist A",),
        id="mixed-chain-exist-defined-errorlevel",
    ),
]


@pytest.mark.parametrize(("source", "expected", "notes"), TRANSLATE_CASES)
def test_cmd_if_variants_translated(
    source: str, expected: str, notes: tuple
) -> None:
    result = _fix(source)
    assert result.command == expected
    assert result.cmd_conditional_fixes == notes
    assert result.changed


def test_variant_operand_and_branch_rewrites_compose() -> None:
    """Inner ``2>nul`` and path rewrites keep firing inside new-kind branches."""
    result = _fix(r"if defined V (dir D:\a 2>nul) else (if exist C:\p (echo y))")
    out = result.command
    assert out.endswith(
        "if [[ -n ${V+x} ]]; then dir D:/a 2>/dev/null; "
        'else if [[ -e C:/p ]]; then echo y; fi; fi'
    )
    assert result.nul_fixes == ("nul",)
    assert result.path_changes == (r"D:\a", r"C:\p")
    assert result.cmd_conditional_fixes == ("if defined V", r"if exist C:\p")


def test_compare_body_glob_composes() -> None:
    """A defined-kind body glob still runs its command context normally."""
    result = _fix("if not defined PYTHONHOME (cat *.tmp)")
    assert result.command == (
        "if [[ ! -n ${PYTHONHOME+x} ]]; then cat *.tmp; fi"
    )


def test_app_layer_command_identical() -> None:
    for case in TRANSLATE_CASES:
        src = case.values[0]
        assert _fix_app_layer(src).command == _fix(src).command


# ── negative: shapes that must stay byte-for-byte ───────────────────────────
UNTOUCHED_CASES = [
    # defined / errorlevel operand guards
    "if defined %V% (x)",
    "if defined 1 (x)",
    "if defined a-b (x)",
    "if errorlevel abc (x)",
    "if errorlevel 1.5 (x)",
    # compare side rules (design §3.1 bails)
    'if a=="a" (x)',          # asymmetric quotes: cmd is FALSE, naive bash TRUE
    'if "a"==b (x)',          # the mirror asymmetry
    'if "a" == "b" (x)',      # spaced ==: token split breaks the one-word rule
    'if !V!==1 (x)',          # cmd delayed expansion
    'if "%V%"=="a*b" (x)',    # glob char in a compare side
    'if "%V%"=="a$b" (x)',    # $ expansion in a side
    'if `a`=="b" (x)',        # backtick in a side
    'if %V%=="a b" (x)',      # unbalanced/garbage word
    # arithmetic side rules
    "if 5 GEQ 3 (x)",         # uppercase relational word bails v1
    "if not X geq Y (x)",     # non-numeric/non-%V% arithmetic sides
    "if 5 geq (x)",           # missing right operand
    "if X==Y geq 3 (x)",      # == wins as compare, but sides unquoted... (bail)
    # /i before the wrong kinds is invalid cmd
    "if /i exist x (a)",
    "if /i defined V (a)",
    "if /i errorlevel 1 (a)",
    # case-sensitive keywords (same conservatism as the landed matcher)
    "IF DEFINED X (x)",
    "if EXIST x (x)",
    "if Not defined x (x)",
    # branch-level bails shared with the landed matcher
    "if defined V (a)\nelse (b)",     # newline before else
    "if defined V\n(a)",              # newline before paren
    "if defined V ()",                # empty body
    "if defined V (a)#c",             # comment body
    "if defined V (a)rev",            # word glued to )
    # real bash that merely shares a word with the cmd shapes
    "if defined; then :; fi",         # a command literally named ``defined``
    "if [ -v VAR ]; then :; fi",      # real bash ``-v`` test
    "if errorlevel; then :; fi",
    'echo "if defined V (x)"',        # quoted data, never code
    "x=if; echo $x",
]


@pytest.mark.parametrize("command", UNTOUCHED_CASES)
def test_untouched_when_not_the_cmd_shape(command: str) -> None:
    result = _fix(command)
    assert result.command == command, "structure must stay byte-for-byte"
    assert result.cmd_conditional_fixes == ()


# ── BashFix report plumbing ──────────────────────────────────────────────────
def test_warning_names_control_flow_constructs() -> None:
    assert "cmd.exe control-flow construct" in _fix("if defined V (a)").warning
    assert "`if defined V`" in _fix("if defined V (a)").warning


def test_positional_construction_order_unchanged() -> None:
    """No new BashFix field was added: the landed positional build still holds."""
    result = _fix("if errorlevel 1 (a)")
    rebuilt = BashFix(
        result.command,
        result.replacements,
        result.path_changes,
        result.shell_wrappers,
        result.nul_fixes,
        result.unsupported,
        result.cmd_conditional_fixes,
    )
    assert rebuilt == result


# ── real-bash validation ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "source",
    [c.values[0] for c in TRANSLATE_CASES]
    + [r"if defined V (dir D:\a 2>nul) else (echo no)"],
)
def test_translations_parse_with_bash(source: str) -> None:
    """Every translated output must parse: the source was a guaranteed rc=2."""
    fixed = _fix(source).command
    proc = subprocess.run(
        [_bash_or_skip(), "-n", "-c", fixed], capture_output=True, text=True
    )
    assert proc.returncode == 0, (source, fixed, proc.stderr)


def _run(script: str, env: dict[str, str] | None = None) -> str:
    import os

    full_env = dict(os.environ)
    full_env.update(env or {})
    proc = subprocess.run(
        [_bash_or_skip(), "--noprofile", "--norc", "-c", script],
        capture_output=True,
        text=True,
        env=full_env,
        timeout=60,
    )
    return proc.stdout.strip()


def test_defined_semantics_set_unset(tmp_path: Path) -> None:
    """``${V+x}`` reproduces cmd ``if defined`` (set-but-empty counts as set)."""
    assert _run("unset ZZ; " + _fix("if defined ZZ (echo s) else (echo u)").command) == "u"
    assert _run("ZZ=1; " + _fix("if defined ZZ (echo s) else (echo u)").command) == "s"
    assert _run('ZZ=""; ' + _fix("if defined ZZ (echo s) else (echo u)").command) == "s"
    # negated form inverts
    assert _run("unset ZZ; " + _fix("if not defined ZZ (echo y) else (echo n)").command) == "y"


def test_errorlevel_semantics(tmp_path: Path) -> None:
    """``if errorlevel N`` ⇔ previous rc >= N (design §cmd ground truth)."""
    assert _run("false; " + _fix("if errorlevel 1 (echo ge) else (echo lt)").command) == "ge"
    assert _run("true; " + _fix("if errorlevel 1 (echo ge) else (echo lt)").command) == "lt"
    assert _run(
        "(exit 7); " + _fix("if errorlevel 5 (echo ge) else (echo lt)").command
    ) == "ge"
    # ``if not errorlevel N`` ⇔ rc < N
    assert _run("true; " + _fix("if not errorlevel 5 (echo lt) else (echo ge)").command) == "lt"


def test_compare_semantics_with_real_env(tmp_path: Path) -> None:
    assert _run(
        'FOO=bar; ' + _fix('if "%FOO%"=="bar" (echo y) else (echo n)').command
    ) == "y"
    assert _run(
        'FOO=bar; ' + _fix('if "%FOO%"=="baz" (echo y) else (echo n)').command
    ) == "n"


def test_fold_i_semantics(tmp_path: Path) -> None:
    """``/i`` makes the comparison case-insensitive via ``${V,,}``."""
    assert _run(
        'V=XY; ' + _fix('if /i "%V%"=="xy" (echo eq) else (echo ne)').command
    ) == "eq"
    # default (no /i) is case-sensitive, matching cmd
    assert _run(
        'V=XY; ' + _fix('if "%V%"=="xy" (echo eq) else (echo ne)').command
    ) == "ne"


def test_arithmetic_semantics(tmp_path: Path) -> None:
    assert _run(_fix("if 5 geq 3 (echo big) else (echo small)").command) == "big"
    assert _run("sh -c 'exit 5'; " + _fix("if %errorlevel% neq 0 (echo bad) else (echo good)").command) == "bad"
    assert _run("true; " + _fix("if %errorlevel% neq 0 (echo bad) else (echo good)").command) == "good"


@pytest.mark.skipif(sys.platform != "win32", reason="cmd control flow is Windows-targeted")
def test_branch_group_keeps_interpreter_context(tmp_path: Path) -> None:
    """A defined-branch ``cd`` must persist (parens deleted, no subshell)."""
    (tmp_path / "sub").mkdir()
    fixed = _fix("if defined ZZ (cd sub) && pwd").command
    assert fixed == "if [[ -n ${ZZ+x} ]]; then cd sub; fi && pwd"
    script = tmp_path / "probe.sh"
    script.write_text(fixed + "\n", encoding="utf-8", newline="\n")
    proc = subprocess.run(
        [_bash_or_skip(), "--noprofile", "--norc", str(script)],
        capture_output=True, text=True, cwd=str(tmp_path),
        env={"ZZ": "1", "PATH": str(Path(_bash_or_skip()).parent)},
        timeout=60,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout.strip().replace("\\", "/").lower().endswith("/sub")
