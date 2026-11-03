"""Startup lazy-import phase (Python 3.15) — deterministic subprocess tests.

These tests pin the startup-performance contract without any wall-time
assertions (which are fragile under load):

* ``import kimix`` / ``import kimix.ui`` / ``import kimix.cli_impl.main`` must
  not execute the SDK-heavy modules (``kimix.utils`` / ``kimix.base`` /
  ``kimix.cot`` / ``kimi_agent_sdk`` / ``kosong`` / ``pydantic`` …).
* ``kimix --help`` must print the historical help text (golden) and exit 0
  without loading the SDK stack — enforced by inspecting ``sys.modules`` in
  the subprocess afterwards, not by measuring elapsed time.
* ``kimix --connect <pid>`` (no server present) must likewise stay light.
* PEP 810 module-level lazy bindings in ``kimix.cli_impl`` reify on first
  use and preserve object identity with the canonical source modules.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Modules that must never be executed by the bare-import / --help / --connect
# paths.  They form the SDK stack (kosong / kimi_agent_sdk / pydantic) plus
# the kimix agent layers that pull it in.
_HEAVY_MODULES = (
    "kimix.utils",
    "kimix.utils.config",
    "kimix.base",
    "kimix.cot",
    "kimix.cli_impl.commands",
    "kimi_agent_sdk",
    "kosong",
    "pydantic",
    "kimi_cli.wire",
    "kimi_cli.wire.types",
)

# Golden help for ``kimix --help`` (prog forced to ``kimix``), stored as the
# whitespace-separated word sequence.  argparse re-wraps help text based on
# the detected terminal width, which varies between CI/dev environments, so
# comparing words (order-sensitive) instead of lines keeps the golden test
# deterministic while still catching missing/extra/reordered options.
_GOLDEN_HELP_WORDS = [
    "usage:",
    "kimix",
    "[-h]",
    "[--connect",
    "[PID]]",
    "[-c]",
    "[-no_color]",
    "[-no_think]",
    "[-no_yolo]",
    "[--manually-cot]",
    "[-s",
    "[SKILL_DIR",
    "...]]",
    "{serve,gui,ssecli,mcp}",
    "...",
    "Kimi",
    "Agent",
    "CLI",
    "positional",
    "arguments:",
    "{serve,gui,ssecli,mcp}",
    "options:",
    "-h,",
    "--help",
    "show",
    "this",
    "help",
    "message",
    "and",
    "exit",
    "--connect",
    "[PID]",
    "Send",
    "stdin",
    "to",
    "another",
    "Kimix",
    "CLI",
    "(optionally",
    "select",
    "its",
    "process",
    "ID)",
    "-c,",
    "--clean",
    "Delete",
    "cache",
    "file",
    "after",
    "quit",
    "-no_color,",
    "--no_color",
    "Disable",
    "colorful",
    "print",
    "-no_think,",
    "--no_think",
    "Disable",
    "thinking",
    "mode",
    "-no_yolo,",
    "--no_yolo",
    "Disable",
    "YOLO",
    "mode",
    "--manually-cot",
    "Enable",
    "manually",
    "CoT",
    "mode",
    "-s,",
    "--skill-dir",
    "[SKILL_DIR",
    "...]",
    "Specify",
    "custom",
    "skill",
    "directory(s)",
]


def _run_snippet(code: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run *code* in a fresh interpreter; return the completed process."""
    env = os.environ.copy()
    # Suppress the optional native-acceleration status line so help output is
    # deterministic regardless of whether the native binary is staged.
    env["KIMIX_NATIVE"] = "0"
    # Make argparse wrapping deterministic even if the outer environment
    # exports a COLUMNS value.
    env["COLUMNS"] = "80"
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        timeout=120,
    )


def _assert_ok(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, (
        f"snippet failed with {result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def test_import_kimix_defers_heavy_modules() -> None:
    code = (
        "import sys\n"
        "import kimix\n"
        "heavy = [m for m in " + repr(_HEAVY_MODULES) + " if m in sys.modules]\n"
        "print('HEAVY=' + ','.join(heavy))\n"
    )
    result = _run_snippet(code)
    _assert_ok(result)
    assert "HEAVY=" in result.stdout
    assert result.stdout.strip().splitlines()[-1] == "HEAVY="


def test_import_kimix_ui_defers_printing_and_stream() -> None:
    code = (
        "import sys\n"
        "import kimix.ui\n"
        "loaded = [m for m in ('kimix.ui.printing', 'kimix.ui.stream', 'kimix.base', 'kimix.utils') if m in sys.modules]\n"
        "print('LOADED=' + ','.join(loaded))\n"
    )
    result = _run_snippet(code)
    _assert_ok(result)
    assert result.stdout.strip().splitlines()[-1] == "LOADED="


def test_import_cli_main_defers_runner_and_utils() -> None:
    code = (
        "import sys\n"
        "import kimix.cli_impl.main\n"
        "loaded = [m for m in ('kimix.cli_impl.core', 'kimix.cli_impl.args', 'kimix.utils', 'kimix.base') if m in sys.modules]\n"
        "print('LOADED=' + ','.join(loaded))\n"
    )
    result = _run_snippet(code)
    _assert_ok(result)
    assert result.stdout.strip().splitlines()[-1] == "LOADED="


def test_help_is_golden_and_stays_light() -> None:
    code = (
        "import sys\n"
        "sys.argv = ['kimix', '--help']\n"
        "from kimix.cli_impl.main import cli\n"
        "try:\n"
        "    cli()\n"
        "except SystemExit as exc:\n"
        "    print('EXIT=%s' % exc.code)\n"
        "heavy = [m for m in " + repr(_HEAVY_MODULES) + " if m in sys.modules]\n"
        "print('HEAVY=' + ','.join(heavy))\n"
    )
    result = _run_snippet(code)
    _assert_ok(result)
    lines = result.stdout.splitlines()
    assert "EXIT=0" in lines, result.stdout
    assert lines[-1] == "HEAVY=", result.stdout
    help_text = "\n".join(lines[: lines.index("EXIT=0")])
    assert help_text.split() == _GOLDEN_HELP_WORDS, help_text
    assert result.stderr == "", result.stderr


def test_connect_path_stays_light(tmp_path: Path) -> None:
    code = (
        "import sys\n"
        "sys.argv = ['kimix', '--connect', '999999']\n"
        "from kimix.cli_impl.main import cli\n"
        "cli()\n"
        "heavy = [m for m in " + repr(_HEAVY_MODULES) + " if m in sys.modules]\n"
        "print('HEAVY=' + ','.join(heavy))\n"
    )
    result = _run_snippet(code, cwd=tmp_path)
    _assert_ok(result)
    lines = result.stdout.splitlines()
    assert lines[-1] == "HEAVY=", result.stdout
    assert any("Could not connect to PID 999999" in line for line in lines), result.stdout


def test_core_lazy_bindings_reify_with_identity() -> None:
    """PEP 810 lazy bindings resolve on first use to the canonical objects."""
    code = (
        "import sys\n"
        "from kimix.cli_impl import core\n"
        # The SDK stack must still be absent: importing core only needs the\n"
        # cheap stdlib/argparse/printing layers (constants pulls printing).\n"
        "heavy = [m for m in ('kimix.base', 'kimix.utils', 'kimix.cot', 'kimi_agent_sdk') if m in sys.modules]\n"
        "assert not heavy, 'SDK stack loaded at core import: %s' % heavy\n"
        "print_fn = core.print_info  # attribute access triggers reification\n"
        "import kimix.ui.printing\n"
        "assert print_fn is kimix.ui.printing.print_info\n"
        "assert core.print_error is kimix.ui.printing.print_error\n"
        "assert core.prompt is kimix.utils.prompt\n"
        "assert core.sync_all is kimix.base.sync_all\n"
        "print('IDENTITY_OK')\n"
    )
    result = _run_snippet(code)
    _assert_ok(result)
    assert "IDENTITY_OK" in result.stdout


def test_help_native_line_still_printed_before_parse() -> None:
    """``_check_native`` runs before argument parsing (output order kept)."""
    code = (
        "import sys\n"
        "sys.argv = ['kimix', '--help']\n"
        "from kimix.cli_impl.main import cli\n"
        "cli()\n"
    )
    env = os.environ.copy()
    env.pop("KIMIX_NATIVE", None)
    env["COLUMNS"] = "80"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    _assert_ok(result)
    lines = result.stdout.splitlines()
    # The native status line (debug or warning) must precede the usage line,
    # matching the historical ordering on environments with native staged.
    assert lines[0] != "usage: kimix [-h] [--connect [PID]] [-c] [-no_color] [-no_think] [-no_yolo]"
    assert any(
        "Native acceleration" in line for line in lines
    ), result.stdout
    assert any(line.startswith("usage: kimix ") for line in lines), result.stdout


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_flag_variants(flag: str) -> None:
    code = (
        "import sys\n"
        "sys.argv = ['kimix', %r]\n"
        "from kimix.cli_impl.main import cli\n"
        "cli()\n"
    ) % flag
    result = _run_snippet(code)
    _assert_ok(result)
    assert result.stdout.split() == _GOLDEN_HELP_WORDS, result.stdout
