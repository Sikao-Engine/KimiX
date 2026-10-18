"""Native bash-fix kernel tests: ``runtime_py.bash_fix`` vs pure Python.

The Windows Git Bash scanner (``bin/kimix_native/_shell_compat.py``) has two
implementations of the same algorithm:

* the pure-Python reference in ``kimix_native.shell_compat`` (the historical
  body of ``kimi_cli.tools.file.bash.bash_fix``), and
* the compiled ``runtime_py.bash_fix`` kernel — the C++ port in the kimix native
  repo (``src/llm/bash_parser``), locked byte-for-byte against the reference by
  its own differential golden battery.

These tests lock the Python-side contract of the native path:

1. ``_native_bash_fix()`` resolves to ``runtime_py.bash_fix`` when the staged
   extension is current (version gate + ``KIMIX_NATIVE`` / ``KIMIX_NATIVE_BASH_FIX``
   toggles) and returns ``None`` otherwise — the facade then falls back to the
   reference;
2. ``fix_bash_command`` / ``_process_unquoted`` / ``bash_compatibility_prelude``
   are BYTE-IDENTICAL between the two implementations over a corpus that spans
   every rule family (fallbacks, wrappers, Windows/Git-Bash paths, ``nul``
   redirects, cmd.exe ``if``/``for`` translation, heredocs, unsupported commands,
   malformed/unbalanced input, embedded NUL and non-ASCII);
3. the native path still returns the reference ``BashFix`` frozen dataclass with
   the full seven-field surface (``cmd_conditional_fixes`` included);
4. the reference ``/tmp`` temp-dir spelling (including a monkeypatched
   ``_windows_temp_dir``, and therefore also on POSIX hosts) reaches the native
   kernel.

Every comparison takes the native result from ``runtime_py.bash_fix`` directly and
the oracle from the facade's ``_py_*`` captures, so the test is meaningful in both
gates.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BIN = _REPO / "bin"
_NATIVE_FILE = "runtime_py.pyd" if sys.platform == "win32" else "runtime_py.so"

pytestmark = pytest.mark.skipif(
    not (_BIN / _NATIVE_FILE).is_file(),
    reason="native runtime not staged",
)

sys.path.insert(0, str(_BIN))

from kimi_cli.native_loader import get_compat as _get_compat  # noqa: E402

_shell = _get_compat("_shell_compat")


# ---------------------------------------------------------------------------
# corpus: one or more rows per rule family of the reference scanner
# ---------------------------------------------------------------------------
BASH_FIX_CORPUS = [
    # empty / no-op
    "",
    "   ",
    "echo plain",
    "ls -la",
    # fallback commands (guarded definitions + export -f prefix)
    "rev",
    "printf abc | rev -0",
    "open README.md",
    "xcrun simctl list",
    "pbcopy < in.txt",
    "wget https://example.com/f.zip -O out.zip",
    "tree -L 2 .",
    "gtimeout 5 make",
    "watch -n 1 date",
    # wrapper operands re-scanned as command contexts
    "env FOO=1 rev",
    "nohup rev &",
    "timeout 10 rev file.txt",
    "xargs rev",
    "stdbuf -o0 rev",
    "nice -n 5 rev",
    # redundant shell unwrapping / -c inline scripts
    "bash rev",
    "sh -c 'rev; cat /tmp/x'",
    "bash cd repo && make",
    "bash -c 'echo /tmp/x > nul'",
    # Windows path rewrites + quoting
    r"cat C:\repo\src\main.cpp",
    r"cd D:\x && dir .\build",
    r'type "C:\Program Files\a b.txt"',
    r"ls \\server\share\dir",
    r"echo ~\Desktop",
    "cd /d D:\\x",
    "cd /d/foo",
    # Git Bash virtual absolute paths
    "echo /tmp/x",
    "cat /tmp/a\\ b",
    "echo /tmp/${x}",
    "echo /tmp/x_$i",
    "echo /c/dev/x",
    "echo /d/dev/$f",
    "echo /tmp/x$(cat /tmp/y)",
    # null-device rewrites
    "echo hi > nul",
    "echo hi 2> nul",
    r"echo hi > \nul",
    "echo hi > $'nul'",
    "echo hi > nul''",
    "echo hi > 'nul'",
    "cat x 2>&1 >nul",
    # cmd.exe control flow translation
    "@echo off & dir",
    "if exist foo.txt (echo yes) else (echo no)",
    "if not defined V echo missing",
    "if errorlevel 1 (echo bad) else (echo ok)",
    'if "%BUILD%"=="release" (echo rel) else (echo dbg)',
    "if 5 equ 5 echo equal",
    "for %f in (a,b,c) do echo %f",
    "for %%f in (*.txt) do del %%f",
    "goto :eof",
    "call build.bat",
    # heredocs / here-strings / trailing operator repair
    "cat <<E\nhi\nE\n&& echo x\n",
    "cat <<-'E'\n\tbody\n\tE\n",
    "cat <<E\nbody with $x\nE",
    # unsupported commands (refusal channel only)
    "journalctl -u svc",
    "systemctl status",
    "apt-get install foo",
    # quoting / escaping / expansions
    "echo 'a b' \"c d\"",
    'echo "$(rev /tmp/x)"',
    "echo `rev`",
    "echo $((1 + 2))",
    "echo a\\ b",
    # malformed / unbalanced input (conservative pass-through)
    "echo 'unterminated",
    'echo "unterminated',
    "echo $(unclosed",
    "if exist",
    "for %f in",
    "((" * 40,
    "echo " + "$(" * 60 + "pwd" + ")" * 60,
    # byte edge cases
    "\x00",
    "echo a\x00b",
    "echo 中文 /tmp/中文",
    "echo 🎉 > nul",
    "x" * 4000,
]


def _native():
    """The compiled submodule; skip when the loaded runtime is stale."""
    module = _shell._native_bash_fix()
    if module is None:
        pytest.skip("native bash_fix kernel inactive (KIMIX_NATIVE/_BASH_FIX gate)")
    return module


def _native_fields(native, command: str) -> tuple:
    """The reference dataclass fields, rebuilt from the native tuple."""
    (
        fixed,
        replacements,
        path_changes,
        shell_wrappers,
        nul_fixes,
        unsupported,
        cmd_conditional_fixes,
    ) = native.fix_bash_command(command, _shell._native_temp_dir())
    result = _shell.BashFix(
        command=fixed,
        replacements=tuple(replacements),
        path_changes=tuple(path_changes),
        shell_wrappers=tuple(shell_wrappers),
        nul_fixes=tuple(nul_fixes),
        unsupported=tuple(unsupported),
        cmd_conditional_fixes=tuple(cmd_conditional_fixes),
    )
    return (
        result.command,
        result.replacements,
        result.path_changes,
        result.shell_wrappers,
        result.nul_fixes,
        result.unsupported,
        result.cmd_conditional_fixes,
        result.changed,
        result.warning,
    )


def _reference_fields(command: str) -> tuple:
    """The same tuple from the pure-Python reference kernel."""
    result = _shell._py_fix_bash_command(command)
    return (
        result.command,
        result.replacements,
        result.path_changes,
        result.shell_wrappers,
        result.nul_fixes,
        result.unsupported,
        result.cmd_conditional_fixes,
        result.changed,
        result.warning,
    )


# ---------------------------------------------------------------------------
# 1. the kernel is actually reachable
# ---------------------------------------------------------------------------
def test_native_submodule_exposes_the_fix_api() -> None:
    native = _native()
    for name in ("fix_bash_command", "process_unquoted", "bash_compatibility_prelude"):
        assert callable(getattr(native, name)), name


def test_fix_bash_command_is_the_native_kernel() -> None:
    """The facade entry point must match the native tuple exactly."""
    native = _native()
    command = r"rev C:\repo\x > nul && echo /tmp/y"
    fields = _native_fields(native, command)
    assert _shell.fix_bash_command(command) == _shell.BashFix(
        command=fields[0],
        replacements=fields[1],
        path_changes=fields[2],
        shell_wrappers=fields[3],
        nul_fixes=fields[4],
        unsupported=fields[5],
        cmd_conditional_fixes=fields[6],
    )


# ---------------------------------------------------------------------------
# 2. byte-for-byte parity with the reference kernel
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("command", BASH_FIX_CORPUS)
def test_fix_bash_command_matches_reference(command: str) -> None:
    native = _native()
    assert _native_fields(native, command) == _reference_fields(command)


@pytest.mark.parametrize("command", BASH_FIX_CORPUS)
def test_process_unquoted_matches_reference(command: str) -> None:
    native = _native()
    assert native.process_unquoted(command) == _shell._py_process_unquoted(command)


def test_prelude_matches_reference() -> None:
    native = _native()
    expected = _shell._py_bash_compatibility_prelude()
    if sys.platform != "win32":
        # the facade keeps the reference platform gate
        assert _shell.bash_compatibility_prelude() == ""
        return
    assert native.bash_compatibility_prelude() == expected
    assert _shell.bash_compatibility_prelude() == expected


# ---------------------------------------------------------------------------
# 3. result/dataclass contract
# ---------------------------------------------------------------------------
def test_native_result_rebuilds_the_frozen_dataclass() -> None:
    native = _native()
    result = _shell.fix_bash_command("rev")
    assert isinstance(result, _shell.BashFix)
    assert result.changed is True
    assert "rev" in result.replacements
    assert result.warning.startswith("Added Windows Git Bash fallback(s)")
    # the caller-supplied dataclass is what the consumer sees, not the tuple
    assert type(result) is _shell.BashFix  # noqa: E721 - identity of the class
    # unchanged input keeps the original bytes and empty metadata
    unchanged = _shell.fix_bash_command("echo plain")
    assert unchanged == _shell.BashFix("echo plain")
    assert unchanged.changed is False and unchanged.warning == ""
    # cmd.exe control flow lands in its own list (not replacements)
    translated = _shell.fix_bash_command("if exist foo.txt (echo yes)")
    assert translated.cmd_conditional_fixes
    assert not translated.replacements


def test_native_empty_command_is_a_pass_through() -> None:
    _native()
    assert _shell.fix_bash_command("") == _shell.BashFix("")


# ---------------------------------------------------------------------------
# 4. temp-dir plumbing (the reference gettempdir()/monkeypatch contract)
# ---------------------------------------------------------------------------
def test_native_temp_dir_is_the_reference_spelling() -> None:
    _native()
    import tempfile

    assert _shell._native_temp_dir() == tempfile.gettempdir().replace("\\", "/")


def test_monkeypatched_temp_dir_reaches_the_native_kernel(monkeypatch) -> None:
    _native()
    monkeypatch.setattr(
        _shell, "_windows_temp_dir", lambda: "C:/Users/John Doe/AppData/Local/Temp"
    )
    result = _shell.fix_bash_command("echo /tmp/x")
    assert result.command == 'echo "C:/Users/John Doe/AppData/Local/Temp/x"'
    assert result.path_changes == ("/tmp/x",)
    # and the reference agrees, i.e. the native path is not an exception
    assert result == _shell._py_fix_bash_command("echo /tmp/x")


# ---------------------------------------------------------------------------
# 5. gates: pure-Python fallback still works, kernel toggle is honored
# ---------------------------------------------------------------------------
def _probe(env_extra: dict[str, str]) -> str:
    code = textwrap.dedent(
        """
        import sys
        sys.path.insert(0, {bin_dir!r})
        from kimix_native import _shell_compat as sc
        native = sc._native_bash_fix()
        print("NATIVE=%s" % (native is not None))
        print("FIX=%r" % (sc.fix_bash_command(r"rev C:\\repo\\x > nul").command,))
        print("UNQ=%r" % (sc._process_unquoted(r"a\\b"),))
        """
    ).format(bin_dir=str(_BIN))
    env = {k: v for k, v in os.environ.items() if not k.startswith("KIMIX_NATIVE")}
    env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(_REPO),
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.mark.parametrize("env_extra", [{"KIMIX_NATIVE": "0"}, {"KIMIX_NATIVE_BASH_FIX": "0"}])
def test_gate_falls_back_to_pure_python(env_extra: dict[str, str]) -> None:
    out = _probe(env_extra)
    assert "NATIVE=False" in out
    reference_fix = _shell._py_fix_bash_command(r"rev C:\repo\x > nul")
    assert f"FIX={reference_fix.command!r}" in out
    assert f"UNQ={_shell._py_process_unquoted('a\\b')!r}" in out


def test_gate_is_on_by_default() -> None:
    out = _probe({})
    assert "NATIVE=True" in out
