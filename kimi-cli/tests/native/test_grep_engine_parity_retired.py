"""FP-10: the native GREP engine parity suite is retired, deliberately.

`kimi-cli/tests/native/test_grep_engine_parity.py` compared the compiled
`runtime_py.grep` kernel against the pure-Python `kimix_native.grep` mirror. It
was left behind by the "remove native module grep" commit (`fe10182b`), which:

* deleted 883 lines of native delegation from `kimi_cli/tools/file/grep_local.py`;
* added `kimi-cli/tests/tools/test_grep_ripgrep_only.py`, whose docstring states
  "the grep tool must always use ripgrep" and which parametrically asserts that
  18 native-grep symbols are GONE from `grep_local`.

The two suites therefore contradicted each other, and the stale one failed with
38 test failures against the new ripgrep-only implementation.

These probes pin the retirement so it can only be undone on purpose.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
LEGACY_SUITE = REPO_ROOT / "kimi-cli/tests/native/test_grep_engine_parity.py"
GREP_LOCAL = REPO_ROOT / "kimi-cli/src/kimi_cli/tools/file/grep_local.py"
REPLACEMENT_SUITE = REPO_ROOT / "kimi-cli/tests/tools/test_grep_ripgrep_only.py"

# Production roots that must not reach for the native grep kernel.
PROD_ROOTS = [
    REPO_ROOT / "kimi-cli/src/kimi_cli",
    REPO_ROOT / "src/kimix",
]

_REMOVED_SYMBOLS = ("_native_grep", "_grep_kernel", "_native_select_files", "_NATIVE_GREP")


def test_legacy_parity_suite_is_gone() -> None:
    assert not LEGACY_SUITE.exists(), (
        "the native-grep parity suite was retired by FP-10; if the native engine "
        "comes back, re-add a working suite instead of restoring the stale one"
    )


def test_the_replacement_suite_still_asserts_ripgrep_only() -> None:
    text = REPLACEMENT_SUITE.read_text(encoding="utf-8")
    assert "always use ripgrep" in text
    assert "test_native_grep_symbol_is_gone" in text


@pytest.mark.parametrize("symbol", _REMOVED_SYMBOLS)
def test_grep_local_has_no_native_symbol(symbol: str) -> None:
    tree = ast.parse(GREP_LOCAL.read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert symbol not in names, f"`{symbol}` is back in grep_local — grep must use ripgrep only"


def _imports_native_grep(source: str) -> bool:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            a.name.startswith("kimix_native.grep") for a in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("kimix_native.grep"):
            return True
    return False


def test_no_production_module_imports_the_native_grep_kernel() -> None:
    offenders: list[str] = []
    for root in PROD_ROOTS:
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            if _imports_native_grep(path.read_text(encoding="utf-8", errors="replace")):
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], f"these modules still import kimix_native.grep: {offenders}"


def test_the_ripgrep_only_suite_is_collectable() -> None:
    """The replacement suite must be present and green-able (it is the spec now)."""
    assert REPLACEMENT_SUITE.exists()
