"""Regression tests: the Git Bash ``nul``-file leak.

On Windows Git Bash, a redirection whose *effective* target is the bare word
``nul`` (``> nul``, ``2>> NUL``, ``&> nul``, backslash-escaped ``> \\nul``,
ANSI-C ``> $'nul'``) silently creates/truncates an ordinary file named ``nul``
in the working directory instead of discarding output.  The Bash tool's
compatibility scanner must rewrite every such target to ``/dev/null`` before
the command runs — including targets that only materialise at *runtime*, when
bash re-parses a string handed to ``eval`` (or a same-shell wrapper such as
``watch`` that re-``eval``s its operands).

The tests here exercise the canonical pure-Python scanner in
``bin/kimix_native/_shell_compat.py`` (reached through the app-layer
``bash_fix.fix_bash_command`` and the tool's ``_prepare_command`` pipeline).
Execution tests additionally prove no ``nul`` file appears on disk.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import patch

import pytest
import regex as re

from kimi_cli.tools.file.bash import bash_fix
from kimi_cli.tools.file.bash.bash_fix import BashFix
from kimi_cli.tools.file.bash.shell_common import inspect_bash_command

# A bare ``nul`` word anywhere in the fixed command means Git Bash can still
# create the file: at top level, or when a quoted operand is re-parsed at
# runtime by ``eval``/``watch``/``bash -c``.  ``\bnul\b`` never matches
# ``null``/``/dev/null``/``nul.txt`` (word chars follow) nor ``annul``.
_NUL_WORD_RE = re.compile(r"(?i)(?<![A-Za-z0-9_.])nul(?![A-Za-z0-9_.])")


def _fix_for_windows(command: str) -> BashFix:
    """Run the fixer with the Windows platform gate forced on."""
    with patch("kimi_cli.tools.file.bash.bash_fix.sys.platform", "win32"):
        return bash_fix.fix_bash_command(command)


def _assert_no_nul_leak(command: str) -> BashFix:
    """Fix *command* and assert no bare ``nul`` word survives anywhere."""
    result = _fix_for_windows(command)
    assert not _NUL_WORD_RE.search(result.command), (
        f"nul leak: {command!r} fixed to {result.command!r}"
    )
    return result


# ── Top-level redirections (the classic leak) ──────────────────────────────
@pytest.mark.parametrize(
    "command",
    [
        "echo hi > nul",
        "echo hi >nul",
        "echo hi >> nul",
        "echo hi &> nul",
        "echo hi >& nul",
        "echo hi >>& nul",
        "echo hi 2> nul",
        "echo hi 2>> NUL",
        "echo hi 2>&nul",
        "echo hi 1>nul",
        "echo hi >| nul",
        "echo hi &>> nul",
        "echo hi > nul 2>&1",
        "foo > nul; bar > NUL",
        "foo >nul 2>nul",
        "if x; then echo a > nul; fi",
        "for f in *; do echo $f > nul; done",
        "while true; do echo x > nul; done",
        "{ echo a; } > nul",
        "(echo a) > nul",
        "foo() { echo a > nul; }",
        "case x in a) echo y > nul ;; esac",
        "exec > nul",
        "exec 2>> nul",
        "foo <> nul",
        "foo <>NUL",
        "foo | bar > nul",
        "foo > \\\nnul",
        "foo \\\n> nul",
        "> nul echo hi",
        "cat <<EOF > nul\nhello\nEOF",
        "diff <(foo > nul) bar",
        "x=$(echo hi > nul)",
        "echo $(foo > nul)",
        "timeout 10 foo > nul",
        "env X=1 foo > nul",
        "nohup foo > nul &",
        "echo a | xargs foo > nul",
    ],
)
def test_toplevel_nul_redirect_is_rewritten(command: str) -> None:
    _assert_no_nul_leak(command)


# ── Backslash / ANSI-C escaped targets (bash strips the escape → bare nul) ──
@pytest.mark.parametrize(
    "command",
    [
        r"echo hi > \nul",
        r"echo hi > n\ul",
        r"echo hi > nu\l",
        r"echo hi > \N\U\L",
        r"echo hi > \NUL",
        "echo hi > $'nul'",
        'echo hi > $"nul"',
        "echo hi > ''nul",
        "echo hi > nul''",
        'echo hi > n""ul',
    ],
)
def test_escaped_nul_redirect_is_rewritten(command: str) -> None:
    _assert_no_nul_leak(command)


# ── Runtime-reparsed strings: eval / watch (the leak that survives quoting) ──
@pytest.mark.parametrize(
    "command",
    [
        "eval 'foo > nul'",
        'eval "foo > nul"',
        "eval 'foo > nul && bar >> NUL'",
        "eval \"eval 'foo > nul'\"",
        'sh -c \'eval "foo > nul"\'',
        "watch -n1 'foo > nul'",
        "watch 'echo a > nul; echo b >> nul'",
        "command eval 'foo > nul'",
        "builtin eval 'foo > nul'",
        "eval foo '> nul'",
        "find . -exec sh -c 'foo > nul' \\;",
        "trap 'foo > nul' EXIT",
        "trap 'foo > nul' DEBUG ERR",
        "(eval 'foo > nul')",
        "{ eval 'x > nul'; }",
        "echo $(eval 'x > nul')",
        "eval 'foo > nul' 'bar > nul'",
    ],
)
def test_eval_nul_redirect_is_rewritten(command: str) -> None:
    _assert_no_nul_leak(command)


# ── bash -c / sh -c inline scripts ─────────────────────────────────────────
@pytest.mark.parametrize(
    "command",
    [
        "bash -c 'foo > nul'",
        'bash -c "foo > nul"',
        "sh -c 'foo > nul'",
        "timeout 5 bash -c 'foo > nul'",
        "nohup sh -c 'foo >> nul' &",
        "bash -c 'foo > nul && bar > nul'",
        r"bash -c 'cd C:\x && foo > nul'",
        'sh -c "bash -c \'foo > nul\'"',
    ],
)
def test_shell_inline_script_nul_is_rewritten(command: str) -> None:
    _assert_no_nul_leak(command)


# ── Non-leaks: these must NOT be rewritten (intentional filenames / data) ──
@pytest.mark.parametrize(
    "command",
    [
        "echo hi > 'nul'",  # quoted target: intentional filename (policy)
        'echo hi > "nul"',
        "echo nul",  # data word, no redirection
        "cat nul",
        "echo hi > nul.txt",
        "echo hi > null",
        "echo hi < nul",  # input redirection never creates a file
        "echo hi > /dev/null",
        "echo hi > /nul",
        "echo hi >&2 > nul.txt",
        "grep nul file.txt",
        "echo a > b.nul",
        "echo 'nul > nul'",  # pure data inside quotes, never reparsed
    ],
)
def test_intentional_and_non_redirect_uses_preserved(command: str) -> None:
    result = _fix_for_windows(command)
    assert result.nul_fixes == (), command
    # The scanner must not have introduced a /dev/null rewrite for these.
    assert "/dev/null" not in result.command or "/dev/null" in command


# ── The full tool pipeline (backslash-normalize + fixer) never leaks nul ───
@pytest.mark.parametrize(
    "command",
    [
        r"cd C:\dev\x && foo > nul",
        r"grep -r x src\tools > nul 2>&1",
        "eval 'foo > nul'",
        "echo hi > nul",
        r"foo > \nul",
    ],
)
def test_full_pipeline_never_leaks_nul(command: str) -> None:
    if sys.platform != "win32":
        pytest.skip("shell_common.inspect_bash_command is Windows-gated")
    fixed = inspect_bash_command(command).command
    assert not _NUL_WORD_RE.search(fixed), (command, fixed)


# ── Execution ground truth: no ``nul`` file appears on disk ────────────────
def _bash_available() -> bool:
    from kimi_cli.tools.file.bash.bash_tool import find_bash

    return find_bash() is not None


@pytest.mark.skipif(sys.platform != "win32", reason="Git Bash nul leak is Windows-only")
@pytest.mark.skipif(not _bash_available(), reason="no bash executable on this host")
@pytest.mark.parametrize(
    "command",
    [
        "echo hi > nul",
        r"echo hi > \nul",
        "echo hi > $'nul'",
        "eval 'echo hi > nul'",
        'eval "echo hi >> nul"',
        "bash -c 'echo hi > nul'",
        "exec 3> nul; exec 3>&-",
        "{ echo a; } > nul",
        "if true; then echo x > nul; fi",
        "echo hi 2> nul 1>&2",
    ],
)
def test_no_nul_file_created_on_disk(tmp_path, command: str) -> None:
    """Run the *fixed* command through real Git Bash; no ``nul`` file may appear."""
    from kimi_cli.tools.file.bash.bash_tool import find_bash
    from kimi_cli.tools.file.bash.shell_common import bash_argv

    fixed = inspect_bash_command(command).command
    argv, env = bash_argv(fixed, login=False)
    proc = asyncio.run(
        _run(argv, env, cwd=tmp_path),
    )
    assert proc.returncode == 0, (command, fixed, proc.stdout, proc.stderr)
    leftovers = [p.name.lower() for p in tmp_path.iterdir()]
    assert "nul" not in leftovers, (
        f"empty nul file created for {command!r} (fixed: {fixed!r}); "
        f"dir now holds {leftovers}"
    )


async def _run(argv, env, cwd):
    import asyncio as _a

    proc = await _a.create_subprocess_exec(
        *argv,
        env=env,
        cwd=str(cwd),
        stdout=_a.subprocess.PIPE,
        stderr=_a.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return type("R", (), {"returncode": proc.returncode, "stdout": out, "stderr": err})
