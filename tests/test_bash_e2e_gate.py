"""FP-00 probes: the root suite must be collectable without the optional bash corpus.

`tests/bash/cases.json` is not tracked by git, and `tests/test_bash_e2e.py` reads
it in `pytest_generate_tests` (collection time), so a missing corpus used to abort
the ENTIRE root suite::

    ERROR tests/test_bash_e2e.py - ValueError: cannot read corpus file ...
    Interrupted: 1 error during collection !!!
    1 skipped, 1 warning, 1 error in 1.81s          (exit code 2)

The corpus is optional: a missing *default* corpus must collect as a skipping
placeholder, while an explicitly requested ``--case <path>`` that does not exist
must still fail loudly.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
E2E_FILE = REPO_ROOT / "tests" / "test_bash_e2e.py"


def _run_pytest(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


def test_default_corpus_is_absent_in_this_checkout() -> None:
    """Guard: if the corpus ever gets committed, this file must be revisited."""
    assert not (REPO_ROOT / "tests" / "bash" / "cases.json").exists()


def test_root_suite_collects_without_the_corpus() -> None:
    """`pytest tests --collect-only` must not abort on the missing corpus."""
    proc = _run_pytest("tests", "--collect-only", "-q")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "cannot read corpus file" not in (proc.stdout + proc.stderr)


def test_e2e_file_alone_collects_and_skips() -> None:
    proc = _run_pytest("tests/test_bash_e2e.py", "-q", "-rs")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "bash e2e corpus not available" in (proc.stdout + proc.stderr)


def test_explicit_missing_case_path_is_still_a_hard_error(tmp_path: Path) -> None:
    """An explicit --case pointing at nothing must not be silently ignored."""
    missing = tmp_path / "nope.json"
    proc = _run_pytest("tests/test_bash_e2e.py", "--case", str(missing), "-q")
    assert proc.returncode != 0
    assert "cannot read corpus file" in (proc.stdout + proc.stderr)


def test_explicit_empty_case_path_skips(tmp_path: Path) -> None:
    corpus = tmp_path / "empty.json"
    corpus.write_text("[]", encoding="utf-8")
    proc = _run_pytest("tests/test_bash_e2e.py", "--case", str(corpus), "-q", "-rs")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_load_cases_rejects_a_non_array_corpus(tmp_path: Path) -> None:
    import importlib.util
    import sys as _sys

    name = "_bash_e2e_gate_mod"
    spec = importlib.util.spec_from_file_location(name, E2E_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # `@dataclass` looks the class's module up in `sys.modules` while resolving
    # string annotations, so the module must be registered before exec_module.
    _sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        _sys.modules.pop(name, None)

    bad = tmp_path / "object.json"
    bad.write_text('{"not": "a list"}', encoding="utf-8")
    with pytest.raises(ValueError):
        module.load_cases(bad)

    malformed = tmp_path / "pair.json"
    malformed.write_text(textwrap.dedent('[["only-one-part"]]'), encoding="utf-8")
    with pytest.raises(ValueError):
        module.load_cases(malformed)
