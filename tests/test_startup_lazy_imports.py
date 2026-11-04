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


def test_help_skips_the_native_probe() -> None:
    """``--help`` must exit inside parsing, before the native probe runs.

    ``_run_cli`` calls ``set_arg()`` first and ``_check_native()`` second, so the
    native status line is deliberately absent from the ``--help`` output: the
    probe reifies ``kimi_cli.native_loader`` (``runtime_py``) and
    ``kimix.ui.printing`` (~20 ms combined) purely to log a diagnostic that a
    help request can never show.
    """
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
    assert any(line.startswith("usage: kimix ") for line in lines), result.stdout
    # No native diagnostic: the probe never ran.
    assert not any("Native acceleration" in line for line in lines), result.stdout
    # And the loader itself was never imported.
    probe = (
        "import sys\n"
        "sys.argv = ['kimix', '--help']\n"
        "from kimix.cli_impl.main import cli\n"
        "try:\n"
        "    cli()\n"
        "except SystemExit:\n"
        "    pass\n"
        "print('NATIVE_LOADER' if 'kimi_cli.native_loader' in sys.modules else 'NO_NATIVE_LOADER')\n"
        "print('PRINTING' if 'kimix.ui.printing' in sys.modules else 'NO_PRINTING')\n"
    )
    probe_result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    _assert_ok(probe_result)
    assert "NO_NATIVE_LOADER" in probe_result.stdout, probe_result.stdout
    assert "NO_PRINTING" in probe_result.stdout, probe_result.stdout


def test_native_probe_still_reports_on_a_real_run() -> None:
    """The probe keeps its diagnostic role — it now runs after parsing."""
    code = (
        "from kimix.cli_impl import core\n"
        "core._check_native()\n"
        "print('CHECK_NATIVE_OK')\n"
    )
    env = os.environ.copy()
    env.pop("KIMIX_NATIVE", None)
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    _assert_ok(result)
    assert "CHECK_NATIVE_OK" in result.stdout
    assert "Native acceleration" in result.stdout, result.stdout


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


# ---------------------------------------------------------------------------
# Deferred native handles in ``kimix.ui.printing`` (PEP 562 + lazy resolution)
# ---------------------------------------------------------------------------


def test_printing_import_does_not_touch_the_native_loader() -> None:
    """Importing ``kimix.ui.printing`` must not resolve the native loader.

    ``printing`` used to import ``kimi_cli.native_loader`` at module scope,
    which charged ``runtime_py`` + the ``kimix_native`` shim to every CLI
    invocation (``main`` -> ``core`` -> ``args`` -> ``constants`` -> here).
    """
    code = (
        "import sys\n"
        "import kimix.ui.printing as p\n"
        "print('NATIVE_LOADER' if 'kimi_cli.native_loader' in sys.modules"
        " else 'NO_NATIVE_LOADER')\n"
        "print('LAZY' if '_print_func' not in vars(p) else 'EAGER_PRINT_FUNC')\n"
        "print('LAZY' if '_NATIVE_STREAM' not in vars(p) else 'EAGER_STREAM')\n"
    )
    result = _run_snippet(code)
    _assert_ok(result)
    assert "NO_NATIVE_LOADER" in result.stdout, result.stdout
    assert "EAGER" not in result.stdout, result.stdout


def test_printing_lazy_attributes_resolve_on_access() -> None:
    """``__getattr__`` reifies the historical names with the same objects.

    The native-print tests read ``p._NATIVE_PRINT`` / ``p._print_func`` as
    module attributes; that contract survives the deferral.
    """
    code = (
        "import kimix.ui.printing as p\n"
        "import kimi_cli.native_loader as nl\n"
        "assert p._native_get_module is nl.get_module\n"
        "assert p._native_get_compat is nl.get_compat\n"
        "assert p._native_use_native is nl.use_native\n"
        "assert p._NATIVE_STREAM is nl.get_module('stream')\n"
        "assert p._print_func is not None\n"
        "assert p._print_func.__name__ in {'print', '_native_print_func'}\n"
        "# Resolution is cached: the module dict is populated after access.\n"
        "assert '_print_func' in vars(p)\n"
        "assert '_NATIVE_PRINT' in vars(p)\n"
        "print('RESOLVE_OK')\n"
    )
    result = _run_snippet(code)
    _assert_ok(result)
    assert "RESOLVE_OK" in result.stdout, result.stdout


def test_printing_unknown_attribute_still_raises() -> None:
    code = (
        "import kimix.ui.printing as p\n"
        "try:\n"
        "    p.definitely_not_defined\n"
        "except AttributeError as exc:\n"
        "    print('ATTR_ERROR', exc)\n"
    )
    result = _run_snippet(code)
    _assert_ok(result)
    assert "ATTR_ERROR" in result.stdout, result.stdout


@pytest.mark.parametrize(
    "native_env",
    [{"KIMIX_NATIVE": "0"}, {"KIMIX_NATIVE": "auto"}],
)
def test_print_always_has_a_bound_sink(native_env: dict[str, str]) -> None:
    """The sink is bound even when the native PRINT kernel is unavailable.

    Regression guard: ``_ensure_native`` must fall back to the builtin print
    instead of leaving ``_print_func`` unbound (``NameError`` on the first
    write, e.g. when reporting a failed ``--connect``).
    """
    code = (
        "import kimix.ui.printing as p\n"
        "assert callable(p._print_func)\n"
        "p.print('bound-sink')\n"
        "p.print_error('bound-error')\n"
        "p._stream.print_word('bound-stream', True)\n"
        "print('SINK_OK')\n"
    )
    env = os.environ.copy()
    env.update(native_env)
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    _assert_ok(result)
    assert "bound-sink" in result.stdout, result.stdout
    assert "bound-error" in result.stdout, result.stdout
    assert "bound-stream" in result.stdout, result.stdout


def test_print_stream_defers_its_sink_but_keeps_an_explicit_one() -> None:
    code = (
        "import io\n"
        "import kimix.ui.printing as p\n"
        "buf = io.StringIO()\n"
        "explicit = p.PrintStream(print_func=lambda *a, **k: buf.write(str(a[0])))\n"
        "explicit.print_word('explicit', True)\n"
        "assert buf.getvalue() == 'explicit', buf.getvalue()\n"
        "# The module-level stream has no explicit sink: it resolves on first use.\n"
        "assert p._stream._print_func is None\n"
        "p._stream.print_word('lazy', True)\n"
        "assert p._stream._print_func is not None\n"
        "print('STREAM_OK')\n"
    )
    result = _run_snippet(code)
    _assert_ok(result)
    assert "STREAM_OK" in result.stdout, result.stdout
    assert "lazy" in result.stdout, result.stdout


# ---------------------------------------------------------------------------
# Deferred ``cli_impl.constants.HELP_STR`` (PEP 562)
# ---------------------------------------------------------------------------


def test_help_str_is_deferred_and_identical() -> None:
    """``HELP_STR`` renders on first access, exactly once, with the same text."""
    code = (
        "import kimix.cli_impl.constants as c\n"
        "assert 'HELP_STR' not in vars(c), 'HELP_STR must not be built at import'\n"
        "text = c.HELP_STR\n"
        "assert c.HELP_STR is text, 'HELP_STR must be cached after first access'\n"
        "assert 'HELP_STR' in vars(c)\n"
        "for marker in (\n"
        "    '/reflection_bash:<path>',\n"
        "    '/reflection_<tool>:<path>',\n"
        "    '/swarm',\n"
        "    '/code:<path> [args...]',\n"
        "):\n"
        "    assert marker in text, marker\n"
        "assert 'Press <Tab>' in text\n"
        "print('HELP_LEN', len(text))\n"
    )
    result = _run_snippet(code)
    _assert_ok(result)
    assert "HELP_LEN" in result.stdout, result.stdout


def test_help_str_follows_no_color() -> None:
    """Deferral fixes the ``--no_color`` interaction.

    Rendering at import time baked ANSI codes in before ``--no_color`` was
    parsed, so ``/help`` used to emit raw escapes in that mode.
    """
    code = (
        "import kimix.ui.printing as p\n"
        "import kimix.cli_impl.constants as c\n"
        "p._colorful_print = False\n"
        "assert '\\x1b[' not in c.HELP_STR\n"
        "print('NO_COLOR_OK')\n"
    )
    result = _run_snippet(code)
    _assert_ok(result)
    assert "NO_COLOR_OK" in result.stdout, result.stdout


def test_args_import_defers_orjson_and_utils() -> None:
    """``import kimix.cli_impl.args`` keeps ``orjson`` / ``regex`` unloaded."""
    code = (
        "import sys\n"
        "import kimix.cli_impl.args  # noqa: F401\n"
        "assert 'orjson' not in sys.modules, 'orjson must stay lazy'\n"
        "assert 'regex' not in sys.modules, 'regex must stay lazy'\n"
        "assert 'kimix.cli_impl.utils' not in sys.modules, 'utils must stay lazy'\n"
        "print('ARGS_LAZY_OK')\n"
    )
    result = _run_snippet(code)
    _assert_ok(result)
    assert "ARGS_LAZY_OK" in result.stdout, result.stdout
