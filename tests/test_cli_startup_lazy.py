"""Regression guard for lazy ``kimix`` package startup imports.

The CLI used to pay ~580ms on *every* invocation because ``import kimix`` eagerly
star-imported ``kimix.utils`` / ``kimix.base`` (and the ``kimix.ui`` package
eagerly imported ``stream``), each of which drags in the heavy
``kimi_agent_sdk`` -> ``kimi_cli`` -> ``kosong`` dependency stack.  Those
package ``__init__`` files are now lazy (PEP 562 ``__getattr__``) so that trivial
entry points (``--help``, ``serve``, ``gui``, ``mcp``) never import the stack.

These tests lock that behaviour in: they assert the heavy stack is *not* imported
by the package-level imports, that lazy attribute access still resolves, and that
the fast CLI path boots without loading ``kimi_agent_sdk``.

Run with the project interpreter:
    python -m pytest tests/test_cli_startup_lazy.py -q
"""

from __future__ import annotations

import subprocess
import sys

HEAVY_MODULES = ("kimi_agent_sdk", "kosong", "kimi_cli")


def _run_python(code: str) -> dict[str, object]:
    """Run ``code`` in a fresh interpreter and return its stdout JSON."""
    import json

    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"subprocess failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    return json.loads(proc.stdout)


def test_import_kimix_does_not_load_heavy_stack() -> None:
    code = (
        "import sys, json; import kimix;"
        "print(json.dumps([m for m in "
        "['kimi_agent_sdk','kosong','kimi_cli'] if m in sys.modules]))"
    )
    loaded = _run_python(code)
    assert loaded == [], f"import kimix must not import the heavy stack, got {loaded}"


def test_import_kimix_utils_does_not_load_heavy_stack() -> None:
    code = (
        "import sys, json; import kimix.utils;"
        "print(json.dumps([m for m in "
        "['kimi_agent_sdk','kosong','kimi_cli'] if m in sys.modules]))"
    )
    loaded = _run_python(code)
    assert loaded == [], f"import kimix.utils must not import the heavy stack, got {loaded}"


def test_import_kimix_ui_printing_does_not_load_kosong() -> None:
    # printing is the lightweight terminal layer every CLI module needs; it must
    # not drag in stream (kosong) via the kimix.ui package __init__.
    code = (
        "import sys, json; import kimix.ui.printing;"
        "print(json.dumps([m for m in ['kimi_agent_sdk','kosong'] "
        "if m in sys.modules]))"
    )
    loaded = _run_python(code)
    assert loaded == [], f"import kimix.ui.printing must not import kosong/kimi_agent_sdk, got {loaded}"


def test_lazy_export_resolves_and_loads_heavy_stack_on_demand() -> None:
    # Accessing a heavy top-level symbol (kimix.prompt -> utils.prompt) must work
    # and, by design, pull in kimi_agent_sdk at that point.
    code = (
        "import sys, json; import kimix;"
        "fn = kimix.prompt;"
        "print(json.dumps({"
        "'callable': callable(fn),"
        "'agent_sdk_loaded': 'kimi_agent_sdk' in sys.modules}))"
    )
    result = _run_python(code)
    assert result["callable"] is True
    assert result["agent_sdk_loaded"] is True


def test_stateful_lazy_export_shares_live_object() -> None:
    # Mutated module-level state (kimix.utils._cli_sessions) must resolve to the
    # same live object the submodule uses.
    code = (
        "import json; import kimix.utils as u; import kimix.utils._globals as g;"
        "u._cli_sessions['probe'] = {'title': 't'};"
        "print(json.dumps({'same': u._cli_sessions is g._cli_sessions, "
        "'has': 'probe' in g._cli_sessions}))"
    )
    result = _run_python(code)
    assert result["same"] is True
    assert result["has"] is True


def test_serve_help_boots_without_heavy_stack() -> None:
    # The real console entry point for a fast subcommand must not import the stack.
    code = (
        "import sys, json; sys.argv=['kimix','serve','--help'];\n"
        "import kimix.cli_impl.main as m\n"
        "try:\n    m.cli()\nexcept SystemExit:\n    pass\n"
        "sys.stderr.write('::JSON::' + json.dumps([mod for mod in "
        "['kimi_agent_sdk','kosong'] if mod in sys.modules]) + '\\n')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
    )
    # serve --help writes argparse help to stdout; our result is a sentinel line
    # on stderr (stderr is otherwise unused by the help path).
    assert proc.returncode == 0, f"failed:\n{proc.stdout}\n{proc.stderr}"
    json_line = next(
        ln for ln in proc.stderr.splitlines() if ln.startswith("::JSON::")
    )
    import json

    loaded = json.loads(json_line[len("::JSON::") :])
    assert loaded == [], f"serve --help must not load heavy stack, got {loaded}"
