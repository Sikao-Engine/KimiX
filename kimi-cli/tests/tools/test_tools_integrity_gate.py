"""Symbol-integrity gate probes across the built-in tool trees (FP-08).

These probes pin the *latent* defects found by the structural review, which no
behavioural test covered:

* `conflict_detect.__all__` exported `EchoTrimLimit`, a name that does not exist
  (F-03) - `from module import *` would raise `AttributeError`.
* `pwsh_tool` / `run` annotated a parameter as `'BackgroundStream'` without ever
  importing the name (F-02) - the string annotation was unresolvable.
* `run.py` imported `shlex` twice (F-04).

Each probe asserts an observable property of the imported module, so it fails on
the pre-fix tree and passes on the fixed one.
"""

from __future__ import annotations

import ast
import importlib
import typing
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


# --------------------------------------------------------------------------- #
# __all__ integrity
# --------------------------------------------------------------------------- #

_ALL_MODULES = [
    "kimi_cli.tools.file.conflict_detect",
    "kimi_cli.tools.file.read_media",
    "kimi_cli.tools.file.read",
    "kimi_cli.tools.file.glob",
    "kimi_cli.tools.file.grep_local",
    "kimi_cli.tools.file.edit",
    "kimi_cli.tools.file.write",
]


@pytest.mark.parametrize("module_name", _ALL_MODULES)
def test_every_dunder_all_name_resolves(module_name: str) -> None:
    """`from <module> import *` must not be able to raise AttributeError."""
    module = importlib.import_module(module_name)
    exported = getattr(module, "__all__", None)
    if exported is None:
        pytest.skip(f"{module_name} does not declare __all__")
    missing = [name for name in exported if not hasattr(module, name)]
    assert missing == [], f"{module_name}.__all__ names undefined symbols: {missing}"


def test_conflict_detect_does_not_export_the_removed_echo_trim_limit() -> None:
    module = importlib.import_module("kimi_cli.tools.file.conflict_detect")
    assert "EchoTrimLimit" not in module.__all__
    assert not hasattr(module, "EchoTrimLimit")


# --------------------------------------------------------------------------- #
# string-annotation integrity
# --------------------------------------------------------------------------- #


def test_pwsh_tool_background_stream_annotation_resolves() -> None:
    """`Powershell._format_session_result` annotates `stream: 'BackgroundStream' | None`."""
    module = importlib.import_module("kimix.tools.file.bash.pwsh_tool")
    hints = typing.get_type_hints(module.Powershell._format_session_result)
    assert "stream" in hints
    assert "BackgroundStream" in str(hints["stream"])


def test_run_tool_background_stream_annotation_resolves() -> None:
    module = importlib.import_module("kimix.tools.file.run")
    hints = typing.get_type_hints(module.Run._format_session_result)
    assert "BackgroundStream" in str(hints["stream"])


# --------------------------------------------------------------------------- #
# duplicate imports
# --------------------------------------------------------------------------- #

_TOOL_MODULES = [
    "src/kimix/tools/file/run.py",
    "src/kimix/tools/file/bash/pwsh_tool.py",
    "src/kimix/tools/file/bash/bash_tool.py",
    "src/kimix/tools/common.py",
    "kimi-cli/src/kimi_cli/tools/file/read_media.py",
    "kimi-cli/src/kimi_cli/tools/file/read.py",
    "kimi-cli/src/kimi_cli/tools/file/write.py",
    "kimi-cli/src/kimi_cli/tools/file/conflict_detect.py",
]


def _top_level_import_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.extend(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.extend(alias.asname or alias.name for alias in node.names)
    return names


@pytest.mark.parametrize("rel_path", _TOOL_MODULES)
def test_no_duplicate_top_level_imports(rel_path: str) -> None:
    """A module must not import the same top-level name twice (ruff F811)."""
    path = REPO_ROOT / rel_path
    assert path.exists(), f"{rel_path} not found under {REPO_ROOT}"
    names = _top_level_import_names(path)
    duplicates = sorted({n for n in names if names.count(n) > 1})
    assert duplicates == [], f"{rel_path} imports these names more than once: {duplicates}"


def test_run_module_imports_shlex_exactly_once() -> None:
    path = REPO_ROOT / "src/kimix/tools/file/run.py"
    names = _top_level_import_names(path)
    assert names.count("shlex") == 1


# --------------------------------------------------------------------------- #
# no string literal inside a PEP-604 union annotation
# --------------------------------------------------------------------------- #

_ANNOTATION_FILES = [
    "src/kimix/tools/common.py",
    "src/kimix/tools/file/bash/bash_tool.py",
    "src/kimix/tools/file/bash/pwsh_tool.py",
    "src/kimix/tools/file/run.py",
    "src/kimix/tools/py/__init__.py",
    "kimi-cli/src/kimi_cli/tools/file/read_media.py",
    "kimi-cli/src/kimi_cli/tools/file/read.py",
    "kimi-cli/src/kimi_cli/tools/file/write.py",
]


def _annotations(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            yield node.annotation
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            for arg in (
                list(args.args) + list(args.posonlyargs) + list(args.kwonlyargs)
                + [a for a in (args.vararg, args.kwarg) if a is not None]
            ):
                if arg.annotation is not None:
                    yield arg.annotation
            if node.returns is not None:
                yield node.returns


def _quoted_union_sites(annotation: ast.expr) -> list[str]:
    """Find `'Name' | None` style unions: a str constant inside a PEP-604 BinOp."""
    hits: list[str] = []
    for node in ast.walk(annotation):
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.BitOr):
            continue
        for side in (node.left, node.right):
            if isinstance(side, ast.Constant) and isinstance(side.value, str):
                hits.append(side.value)
    return hits


@pytest.mark.parametrize("rel_path", _ANNOTATION_FILES)
def test_no_string_literal_inside_union_annotation(rel_path: str) -> None:
    """`'Name' | None` is unevaluatable: `typing.get_type_hints` raises TypeError.

    The inner quotes are redundant now that PEP 649 defers annotation evaluation,
    and they break every runtime annotation consumer (ruff F821 too).
    """
    path = REPO_ROOT / rel_path
    assert path.exists(), f"{rel_path} not found under {REPO_ROOT}"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for annotation in _annotations(tree):
        offenders.extend(_quoted_union_sites(annotation))
    assert offenders == [], f"{rel_path} has quoted names inside a union annotation: {offenders}"
