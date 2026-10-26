r"""Regression tests: cmd.exe ``if [not] exist … ( … ) else ( … )`` conditionals.

Agents on Windows routinely paste cmd.exe batch conditionals into the Bash
tool::

    if exist LuisaCompute (dir LuisaCompute | more +5) else (echo no LuisaCompute dir)

Git Bash rejects the construct outright (``syntax error near unexpected
token '('``) because a command word at pipeline head is never followed by a
bare ``(``.  The compatibility scanner in ``bin/kimix_native/_shell_compat.py``
recognises the *strict* cmd shape — literal ``[not] exist``, one decodable
path operand, parenthesised branches, horizontal whitespace only, single
line — and translates it to ``if [[ [!] -e P ]]; then A; else B; fi``.
Parenthesised cmd groups share the interpreter context (a ``cd`` inside a
branch persists), so the parens are deleted, never turned into bash
subshells.  Branch bodies are scanned normally, so inner path / ``2>nul`` /
fallback rewrites keep composing with the translation.

Everything else is left byte-for-byte (conservative by design): non-parenthesised
branches, multi-line conditionals, quoted operands carrying backslashes,
empty or comment-bearing branch bodies, wildcard operands, and real bash
``if``/``if exist`` commands.  (The generalized ``if defined``/``if errorlevel``/
string- and arithmetic-compare shapes are covered in
``tests/unit/tools/test_bash_cmd_if_variants.py``.)

The structured report field ``BashFix.cmd_conditional_fixes`` and the
``warning`` prose live on the canonical pure-Python scanner (the app-layer
``bash_fix.fix_bash_command`` re-export keeps the *command* identical; the
kimi-cli wrapper reconstructs the result with its own keyword fields and is
out of scope for this epoch).
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from kimi_cli.tools.file.bash import bash_fix
from kimi_cli.native_loader import get_compat as _get_compat

# The canonical pure-Python scanner (the module ``bash_fix`` re-exports);
# reached directly so the structured ``cmd_conditional_fixes`` field is
# observable — the app-layer wrapper predates it and drops it (documented).
_shell_compat = _get_compat("_shell_compat")
BashFix = _shell_compat.BashFix

# The reproduced failing command from the bug report.
REPRODUCED = (
    r"if exist LuisaCompute (dir LuisaCompute | more +5) "
    r"else (echo no LuisaCompute dir) && echo "
    + '"---"'
    + " && "
    + r"if exist D:\LuisaCompute "
    + r"(dir D:\LuisaCompute\include\luisa\backends\ext 2>nul | findstr vk) "
    + r"else (echo no D:\LuisaCompute)"
)


def _fix(command: str) -> BashFix:
    """Run the canonical scanner (platform-gate free, full report fields)."""
    return _shell_compat.fix_bash_command(command)


def _fix_app_layer(command: str) -> BashFix:
    """Run the app-layer entry with the Windows platform gate forced on.

    Mirrors ``tests/unit/tools/test_bash_nul_leak.py``; used to prove the
    rewritten *command* survives the app-layer wrapper unchanged.
    """
    with patch("kimi_cli.tools.file.bash.bash_fix.sys.platform", "win32"):
        return bash_fix.fix_bash_command(command)


def _bash_or_skip() -> str:
    # ``find_bash`` is the Bash tool's own Git-Bash-first resolver; a bare
    # ``shutil.which("bash")`` can hit the WindowsApps WSL execution alias,
    # which cannot see Windows temp paths (rc 127 noise).
    from kimi_cli.tools.file.bash.bash_tool import find_bash

    bash = find_bash()
    if bash is None:
        pytest.skip("no bash executable on this host")
    return str(bash)


# ── positive: exact structural outputs ───────────────────────────────────────
TRANSLATE_CASES = [
    pytest.param(
        "if exist x (echo a) else (echo b)",
        "if [[ -e x ]]; then echo a; else echo b; fi",
        ("if exist x",),
        id="if-exist-else",
    ),
    pytest.param(
        "if exist x (echo a)",
        "if [[ -e x ]]; then echo a; fi",
        ("if exist x",),
        id="if-exist-no-else",
    ),
    pytest.param(
        "if not exist x (echo a) else (echo b)",
        "if [[ ! -e x ]]; then echo a; else echo b; fi",
        ("if not exist x",),
        id="if-not-exist",
    ),
    pytest.param(
        "if exist A (echo 1) else if exist B (echo 2) else (echo 3)",
        "if [[ -e A ]]; then echo 1; elif [[ -e B ]]; then echo 2; else echo 3; fi",
        ("if exist A",),
        id="elif-chain-final-else",
    ),
    pytest.param(
        "if exist A (echo 1) else if exist B (echo 2)",
        "if [[ -e A ]]; then echo 1; elif [[ -e B ]]; then echo 2; fi",
        ("if exist A",),
        id="elif-chain-no-final-else",
    ),
    pytest.param(
        "if exist A (echo 1) else if not exist B (echo 2)",
        "if [[ -e A ]]; then echo 1; elif [[ ! -e B ]]; then echo 2; fi",
        ("if exist A",),
        id="elif-chain-negated",
    ),
    pytest.param(
        "if exist x (a) && echo t",
        "if [[ -e x ]]; then a; fi && echo t",
        ("if exist x",),
        id="tail-andthen",
    ),
    pytest.param(
        "if exist x (a); echo t",
        "if [[ -e x ]]; then a; fi; echo t",
        ("if exist x",),
        id="tail-semicolon",
    ),
    pytest.param(
        "if exist x (a)|grep b",
        "if [[ -e x ]]; then a; fi|grep b",
        ("if exist x",),
        id="tail-pipe",
    ),
    pytest.param(
        "time if exist x (a)",
        "time if [[ -e x ]]; then a; fi",
        ("if exist x",),
        id="after-time-keyword",
    ),
    pytest.param(
        "! if exist x (a)",
        "! if [[ -e x ]]; then a; fi",
        ("if exist x",),
        id="after-negate-reserved-word",
    ),
    pytest.param(
        "echo hi; if exist x (a) else (b)",
        "echo hi; if [[ -e x ]]; then a; else b; fi",
        ("if exist x",),
        id="after-semicolon-command",
    ),
    pytest.param(
        "if exist A (if exist B (echo x) else (echo y))",
        "if [[ -e A ]]; then if [[ -e B ]]; then echo x; else echo y; fi; fi",
        ("if exist A", "if exist B"),
        id="nested-cmd-ifs",
    ),
    pytest.param(
        r"if exist P (dir P\sub) else (echo no P)",
        # P\sub is a single-backslash relative word: the pre-existing path
        # machinery leaves such ambiguous words byte-for-byte (same doctrine
        # as the argument position), while the structure still translates.
        r"if [[ -e P ]]; then dir P\sub; else echo no P; fi",
        ("if exist P",),
        id="operand-and-body-paths",
    ),
    pytest.param(
        'if exist "My Data" (echo ok)',
        'if [[ -e "My Data" ]]; then echo ok; fi',
        ('if exist "My Data"',),
        id="quoted-operand-without-backslash",
    ),
    pytest.param(
        "x=$(if exist P (a) else (b))",
        "x=$(if [[ -e P ]]; then a; else b; fi)",
        ("if exist P",),
        id="inside-command-substitution",
    ),
    pytest.param(
        r"if exist x (cd C:\y)",
        "if [[ -e x ]]; then cd C:/y; fi",
        ("if exist x",),
        id="no-subshell-cd-body-path",
    ),
    pytest.param(
        "if exist x (a) else (b)\n",
        "if [[ -e x ]]; then a; else b; fi\n",
        ("if exist x",),
        id="trailing-newline",
    ),
    pytest.param(
        "if  exist  x  ( a )  ",
        "if [[ -e x ]]; then  a ; fi  ",
        # the note records the original keyword region verbatim (double
        # spaces included); the emitted header is normalized.
        ("if  exist  x",),
        id="extra-spacing-normalized",
    ),
]


@pytest.mark.parametrize(("source", "expected", "notes"), TRANSLATE_CASES)
def test_cmd_if_shapes_translated(source: str, expected: str, notes: tuple) -> None:
    result = _fix(source)
    assert result.command == expected
    assert result.cmd_conditional_fixes == notes
    assert result.changed


def test_if_exist_else_translated_report() -> None:
    """Design §6: keyword token recorded and ``changed`` true."""
    result = _fix("if exist x (echo a) else (echo b)")
    assert result.command == "if [[ -e x ]]; then echo a; else echo b; fi"
    assert result.cmd_conditional_fixes == ("if exist x",)
    assert result.changed
    assert "cmd.exe control-flow construct" in result.warning


def test_if_exist_no_else() -> None:
    result = _fix("if exist LuisaCompute (dir LuisaCompute)")
    assert result.command == "if [[ -e LuisaCompute ]]; then dir LuisaCompute; fi"


def test_if_not_exist() -> None:
    result = _fix(r"if not exist x (echo missing)")
    assert result.command == "if [[ ! -e x ]]; then echo missing; fi"
    assert result.cmd_conditional_fixes == ("if not exist x",)


def test_if_chain_elif() -> None:
    result = _fix("if exist a (x) else if exist b (y) else (z)")
    out = result.command
    assert out == "if [[ -e a ]]; then x; elif [[ -e b ]]; then y; else z; fi"
    # single trailing fi, exactly one elif
    assert out.count("; fi") == 1
    assert out.count("elif") == 1
    # one report entry for the whole conditional
    assert result.cmd_conditional_fixes == ("if exist a",)


def test_operand_path_and_nul_compose() -> None:
    """The reproduced failing command: structure + inner rewrites compose."""
    result = _fix(REPRODUCED)
    out = result.command
    # fallback definitions are prefixed by fix(); the rewritten command is
    # the tail after them.
    assert "findstr()" in out  # findstr fallback definition prefix
    assert out.endswith(
        'if [[ -e LuisaCompute ]]; then dir LuisaCompute | more +5; '
        'else echo no LuisaCompute dir; fi && echo "---" && '
        'if [[ -e D:/LuisaCompute ]]; then '
        'dir D:/LuisaCompute/include/luisa/backends/ext 2>/dev/null '
        '| findstr vk; else echo no D:/LuisaCompute; fi'
    )
    assert result.nul_fixes == ("nul",)
    assert result.path_changes == (
        r"D:\LuisaCompute",
        r"D:\LuisaCompute\include\luisa\backends\ext",
        r"D:\LuisaCompute",
    )
    assert result.cmd_conditional_fixes == (
        "if exist LuisaCompute",
        r"if exist D:\LuisaCompute",
    )
    assert result.replacements == ("findstr",)
    assert result.changed


def test_app_layer_command_identical() -> None:
    """The app-layer wrapper (Windows gate forced on) emits the same command."""
    assert _fix_app_layer(REPRODUCED).command == _fix(REPRODUCED).command


def test_inside_command_substitution() -> None:
    result = _fix("x=$(if exist P (a) else (b))")
    assert result.command == "x=$(if [[ -e P ]]; then a; else b; fi)"


def test_inside_eval_inline_script() -> None:
    """``eval`` operands are runtime-reparsed scripts: inner fix + merged note."""
    result = _fix('eval "if exist x (a) else (b)"')
    assert result.command == "eval 'if [[ -e x ]]; then a; else b; fi'"
    assert result.cmd_conditional_fixes == ("if exist x",)


def test_two_conditionals_one_line_report_each() -> None:
    result = _fix("if exist a (x) && if not exist b (y)")
    assert result.command == "if [[ -e a ]]; then x; fi && if [[ ! -e b ]]; then y; fi"
    assert result.cmd_conditional_fixes == ("if exist a", "if not exist b")


# ── negative: shapes that must stay byte-for-byte ───────────────────────────
UNTOUCHED_CASES = [
    # real bash that merely shares words with the cmd shape
    "if exist; then echo t; fi",
    "if exist foo; then bar; fi",
    "if [ -e x ]; then echo t; fi",
    "if [[ -e x ]]; then echo t; fi",
    "if test -e x; then echo t; fi",
    # quoted data is never code for the scanner
    'echo "if exist x (y)"',
    "echo 'if exist x (y)'",
    # cmd variants that REMAIN out of scope (non-parenthesised, multi-line,
    # operand guards -- the generalized defined/errorlevel/compare shapes now
    # translate; see tests/unit/tools/test_bash_cmd_if_variants.py)
    # non-parenthesised branches: extent ambiguous, bail
    "if exist x echo yes",
    "if exist x (a) else b",
    # multi-line conditionals: out of scope
    "if exist x\n(a)",
    "if exist x\n(a)\nelse\n(b)",
    "if exist x (a)\nelse (b)",
    "if exist x (a) else\n(b)",
    # operand guards
    "if exist (a)",                      # no operand
    "if exist $(f) (a)",                 # expansion operand
    "if exists x (a)",                   # not the literal exist token
    "if EXIST x (a)",                    # exact-case words only
    "if not x exist (a)",
    # close-paren tail that would corrupt the emitted `; fi`
    "if exist x (a)rev",
    'if exist x (a)"q"',
    # empty branch bodies (bash rejects `then ;` / `else ;`)
    "if exist x ()",
    "if exist x (  ) else (b)",
    "if exist x (a) else ()",
    # comment-bearing bodies would swallow the inserted terminator
    "if exist x (echo #c)",
    "if exist x (a)#c",
    "if exist x #c",
    # wildcard operands cannot keep their cmd meaning under `[[ -e ]]`
    r"if exist *.txt (echo a)",
    # quoted operands carrying backslashes: the scanner never rewrites quoted
    # words, and a literal-backslash test would silently take the wrong
    # branch (design §7 revisited in Epoch 2: bail, keep the loud error)
    r'if exist "D:\My Dir" (echo a) else (echo b)',
    r"if not exist 'C:\Program Files' (echo a)",
    # balanced-paren machinery limits
    "if exist x (",
    "if exist x (a",
    "if exist x (a) else (",
    # dangling else
    "if exist x (a) else",
]


@pytest.mark.parametrize("command", UNTOUCHED_CASES)
def test_untouched_when_not_the_cmd_shape(command: str) -> None:
    result = _fix(command)
    assert result.command == command, "structure must stay byte-for-byte"
    assert result.cmd_conditional_fixes == ()
    if "[[" not in command:
        assert "[[" not in result.command


def test_two_words_before_paren_not_translated() -> None:
    r"""``if exist x D:\y (a)`` has no single operand: only the old rewrite."""
    result = _fix(r"if exist x D:\y (a)")
    assert result.command == r"if exist x D:/y (a)"
    assert result.cmd_conditional_fixes == ()
    assert "[[" not in result.command


def test_wildcard_operand_bails_but_keeps_existing_path_rewrite() -> None:
    r"""`D:\a\*` is not translated; only the pre-existing argument path applies."""
    result = _fix(r"if exist D:\a\* (echo a)")
    assert result.command == r"if exist D:/a/* (echo a)"
    assert result.cmd_conditional_fixes == ()
    assert "[[" not in result.command
    assert result.path_changes == (r"D:\a\*",)


def test_real_bash_if_exist_command_untouched() -> None:
    """A real bash command whose name is ``exist`` keeps its semantics."""
    result = _fix("if exist somefile; then echo y; fi")
    assert result.command == "if exist somefile; then echo y; fi"
    assert not result.changed


# ── BashFix report plumbing ──────────────────────────────────────────────────
def test_field_defaults_keep_existing_constructions_valid() -> None:
    assert BashFix("echo ok") == BashFix("echo ok")
    assert BashFix("echo ok").cmd_conditional_fixes == ()
    assert not BashFix("echo ok").changed
    assert BashFix("echo ok").warning == ""


def test_warning_names_the_translation() -> None:
    result = _fix("if exist x (a) else (b)")
    assert "cmd.exe control-flow construct" in result.warning
    assert "`if exist x`" in result.warning


def test_positional_construction_order() -> None:
    """fix() builds BashFix positionally; the new field must be last."""
    result = _fix("if exist x (a)")
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
    "command",
    [
        REPRODUCED,
        "if exist x (echo a) else (echo b)",
        "if exist x (a) && if not exist b (y)",
        "if exist A (if exist B (x) else (y)) else (z)",
        'if exist "My Data" (echo a) else (echo b)',
        "x=$(if exist P (a) else (b))",
        "time if exist x (a)",
        'eval "if exist x (a) else (b)"',
    ],
)
def test_translations_parse_with_bash(command: str) -> None:
    """Every translated output must parse: the bug was a guaranteed rc=2."""
    fixed = _fix(command).command
    proc = subprocess.run(
        [_bash_or_skip(), "-n", "-c", fixed], capture_output=True, text=True
    )
    assert proc.returncode == 0, (command, fixed, proc.stderr)


    @pytest.mark.skipif(sys.platform != "win32", reason="Git Bash nul leak is Windows-only")
    def test_reproduced_command_runs_and_leaks_no_nul(tmp_path: Path) -> None:
        """Run the fixed reproduction under real Git Bash: rc=0, no ``nul`` file.

        The relative operand is absent (isolated cwd), so its else-branch must
        execute.  The absolute operand ``D:\\LuisaCompute`` comes verbatim from
        the bug report and is host state: when the host has that directory the
        translated then-branch runs instead (``dir … | findstr vk``, whose exit
        status depends on the machine's listing), so the verbatim run only pins
        the clean-run invariants.  A hermetic twin — the same command with the
        absolute operand renamed to a name that cannot exist — pins the exact
        ``rc == 0`` + else-branch behavior on every host.
        """
        bash = _bash_or_skip()

        def _run(command: str, name: str) -> subprocess.CompletedProcess[str]:
            script = tmp_path / name
            script.write_text(command + "\n", encoding="utf-8", newline="\n")
            proc = subprocess.run(
                [bash, "--noprofile", "--norc", str(script)],
                capture_output=True,
                text=True,
                cwd=str(tmp_path),
                timeout=60,
            )
            # rc == 2 is the historical bash syntax-error exit code.
            assert proc.returncode != 2, (proc.returncode, proc.stdout, proc.stderr)
            assert "syntax error" not in proc.stderr
            return proc

        absent_operand = rf"D:\LuisaCompute_absent_{uuid.uuid4().hex[:8]}"
        assert not os.path.exists(absent_operand)
        twin = _run(
            _fix(REPRODUCED.replace(r"D:\LuisaCompute", absent_operand)).command,
            "cmd_if_probe_absent.sh",
        )
        assert twin.returncode == 0, (twin.returncode, twin.stdout, twin.stderr)
        assert "no LuisaCompute dir" in twin.stdout
        assert "---" in twin.stdout
        assert "no D:/LuisaCompute" in twin.stdout

        fixed = _fix(REPRODUCED).command
        proc = _run(fixed, "cmd_if_probe.sh")
        absolute_operand_exists = os.path.exists(r"D:\LuisaCompute")
        if absolute_operand_exists:
            assert proc.returncode in (0, 1), (proc.returncode, proc.stdout, proc.stderr)
        else:
            assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
            assert "no D:/LuisaCompute" in proc.stdout
        assert "no LuisaCompute dir" in proc.stdout
        assert "---" in proc.stdout
        leftovers = [p.name.lower() for p in tmp_path.iterdir()]
        assert "nul" not in leftovers, (
            f"empty nul file created running {fixed!r}; dir holds {leftovers}"
        )


def test_branch_group_keeps_interpreter_context(tmp_path: Path) -> None:
    """Parens must NOT become subshells: ``cd`` inside a branch persists.

    cmd.exe groups share the interpreter context; the translation uses
    ``then``/``else`` grouping so the ``cd`` still applies to the script.
    A subshell (``( cd sub )``) would print the parent directory instead.
    """
    bash = _bash_or_skip()
    (tmp_path / "sub").mkdir()
    fixed = _fix(r"if exist sub (cd sub) && pwd").command
    assert fixed == "if [[ -e sub ]]; then cd sub; fi && pwd"
    script = tmp_path / "cd_persist_probe.sh"
    script.write_text(fixed + "\n", encoding="utf-8", newline="\n")
    proc = subprocess.run(
        [bash, "--noprofile", "--norc", str(script)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=60,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    printed = proc.stdout.strip().replace("\\", "/")
    assert printed.lower().endswith("/sub"), (
        f"cd did not persist (subshell regression?): {proc.stdout!r}"
    )
