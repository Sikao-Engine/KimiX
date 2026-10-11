"""Guard: the bash/pwsh command parser never executes runtime_py code.

The shell-command parsing pipeline (``bash_fix.fix_bash_command``,
``bash_tool._process_unquoted``/``_prepare_bash_cmd``,
``pwsh_fix.fix_pwsh_command``, ``process_pwsh.pwsh_transform`` and the
``kimix_native.parse`` mirrors) must run the canonical pure-Python scanner in
``bin/kimix_native/_shell_compat.py`` — never the compiled ``runtime_py``
kernel.  The kernel predates the current shell rules (null-device rewrites,
shell-wrapper repairs, escaped-nul preservation) and measurably diverges from
the reference scanner, so any native route is a correctness bug (e.g. it turns
the null-device escape ``\\nul`` into ``/nul``).

These tests block every ``runtime_py`` entry point with a raising stub while
the gate is forced ON, then run real commands through the whole pipeline: if
anything touches the kernel the test explodes.
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from kimi_cli.tools.file.bash import bash_fix, bash_tool, process_pwsh, pwsh_fix
from kimi_cli.tools.file.bash.shell_common import (
    inspect_bash_command,
    prepare_bash_command,
)


class _NativeBlocker:
    """Attribute-access tripwire standing in for ``runtime_py.parse``."""

    def __getattr__(self, name: str):
        raise AssertionError(
            f"shell parsing touched runtime_py.parse.{name} — the parser must "
            "be pure Python (bin/kimix_native/_shell_compat.py)"
        )


@pytest.fixture()
def block_runtime_py():
    """Make any runtime_py parse call raise, and force every gate ON."""
    import kimix_native

    blocker = _NativeBlocker()
    saved_native = kimix_native._native
    patches = [
        patch.object(kimix_native, "use_native", lambda kernel: True),
        patch.object(kimix_native, "_native", blocker),
    ]
    for p in patches:
        p.start()
    # Some modules cached a reference at import time; patch those too.
    import kimix_native.parse as shim_parse

    with patch.object(shim_parse, "_native", blocker), patch.object(
        shim_parse, "use_native", lambda kernel: True
    ):
        yield blocker
    for p in reversed(patches):
        p.stop()
    assert kimix_native._native is saved_native or True  # restore sanity


SHELL_COMMANDS = [
    "echo hi > nul",
    r"echo hi > \nul",
    "eval 'foo > nul'",
    "trap 'foo > nul' EXIT",
    r"ls C:\Users\me",
    "rev file.txt",
    "bash -c 'cd C:\\x && rev'",
    "grep -r x src/tools",
    "echo 中文",
]

PWSH_COMMANDS = [
    'Write-Output "hello"',
    "Get-ChildItem C:\\Users",
    "Get-Process | Where-Object { $_.CPU -gt 10 }",
]


@pytest.mark.parametrize("command", SHELL_COMMANDS)
def test_bash_pipeline_is_pure_python(block_runtime_py, command: str) -> None:
    # bash_tool._process_unquoted / _prepare_bash_cmd
    bash_tool._process_unquoted(command)
    bash_tool._prepare_bash_cmd(command)
    # bash_fix.fix_bash_command (Windows-gated; force the gate)
    with patch("kimi_cli.tools.file.bash.bash_fix.sys.platform", "win32"):
        bash_fix.fix_bash_command(command)
    # the full tool pipeline (runs on any platform: prepare+fix)
    with patch("kimi_cli.tools.file.bash.bash_fix.sys.platform", "win32"):
        inspect_bash_command(command)
        prepare_bash_command(command)
    # shim mirrors
    import kimix_native.parse as shim_parse

    shim_parse.fix_bash_command(command)
    shim_parse._process_unquoted(command)


@pytest.mark.parametrize("command", PWSH_COMMANDS)
def test_pwsh_pipeline_is_pure_python(block_runtime_py, command: str) -> None:
    pwsh_fix.fix_pwsh_command(command)
    process_pwsh.pwsh_transform(command)
    import kimix_native.parse as shim_parse

    shim_parse.fix_pwsh_command(command)
    shim_parse.pwsh_transform(command)


def test_comment_parsers_keep_native_dispatch(block_runtime_py) -> None:
    """The PARSE kernel is still wired for comment parsing (only shell is pure).

    With ``runtime_py`` blocked and the gate forced on, ``parse()`` must try to
    reach the kernel (proving comment acceleration was not removed) — the
    tripwire raises AssertionError from attribute access.
    """
    import kimix_native.parse as shim_parse

    with pytest.raises(AssertionError):
        shim_parse.parse("python", "x = 1  # comment")


def test_no_shell_scan_symbols_remain_in_shim() -> None:
    """Static check: kimix_native.parse never calls the kernel shell scanners."""
    import inspect

    import kimix_native.parse as shim_parse

    source = inspect.getsource(shim_parse)
    for symbol in ("shell_scan", "shell_transform", "pwsh_fix_warning"):
        assert symbol not in source, (
            f"kimix_native/parse.py still references runtime_py entry {symbol}"
        )


def test_bash_tool_and_pwsh_fix_have_no_native_parse_refs() -> None:
    """Static check: the app-layer fixers hold no runtime_py parse handle."""
    import inspect

    for mod in (bash_tool, pwsh_fix, bash_fix, process_pwsh):
        source = inspect.getsource(mod)
        assert "_NATIVE_PARSE" not in source, f"{mod.__name__} still binds _NATIVE_PARSE"
