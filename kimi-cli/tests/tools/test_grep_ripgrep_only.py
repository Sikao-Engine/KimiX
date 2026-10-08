"""Guard: the grep tool always uses ripgrep, never the native grep kernel.

The native (``kimix_native`` / ``runtime_py``) GREP kernel integration was
removed from ``kimi_cli/tools/file/grep_local.py``: the tool now always spawns
the ripgrep binary (optionally rtk-wrapped) and only falls back to the
pure-Python ``backup_grep`` when ripgrep cannot be resolved (or the session has
a dirty VFS).

These tests pin the removal (no native symbols / imports / call sites) and the
behaviour that motivated it (searches must reach ripgrep even when the native
runtime is staged), so a future re-introduction has to change this contract on
purpose.
"""

from __future__ import annotations

import ast
import inspect
import tempfile
from pathlib import Path

import pytest

import kimi_cli.tools.file.grep_local as grep_local
from kimi_cli.tools.file.grep_local import (
    Grep,
    Params,
    _multiline_pattern,
    _pattern_has_regex_newline,
)

# Symbols that only existed to drive the native GREP kernel.
_REMOVED_NATIVE_SYMBOLS = (
    "_NATIVE_TOOLS",
    "_NATIVE_GREP",
    "_COMPAT_TOOLS",
    "_compat_tools",
    "_grep_kernel",
    "_native_call",
    "_native_grep_job",
    "_native_select_files",
    "_native_stream_lines",
    "_native_unsupported",
    "_native_want_file",
    "_native_ignored",
    "_NativeSelection",
    "_pattern_can_overflow",
    "_use_native_line_scan",
    "_ENGINE_MAX_FILE_BYTES",
    "_ENGINE_BACKTRACK_SAFE_FILE_BYTES",
    "_NONASCII_BLOCK",
)

_SOURCE = inspect.getsource(grep_local)
_TREE = ast.parse(_SOURCE)


# ---------------------------------------------------------------------------
# removal guards (static)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _REMOVED_NATIVE_SYMBOLS)
def test_native_grep_symbol_is_gone(name: str) -> None:
    assert not hasattr(grep_local, name), (
        f"`{name}` is back — the grep tool must always use ripgrep"
    )


def test_module_does_not_import_the_native_stack() -> None:
    imported: set[str] = set()
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    offenders = sorted(
        m
        for m in imported
        if m.startswith("kimi_cli.native_loader") or m.startswith("kimix_native")
    )
    assert offenders == [], f"native imports are back: {offenders}"


def test_module_has_no_native_identifier_references() -> None:
    offenders: list[str] = []
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Name) and node.id.startswith("_native"):
            offenders.append(node.id)
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_native"):
            offenders.append(node.attr)
    assert offenders == [], f"native call sites are back: {sorted(set(offenders))}"


# ---------------------------------------------------------------------------
# behaviour: ripgrep is always the engine
# ---------------------------------------------------------------------------


async def test_single_file_search_spawns_ripgrep(
    grep_tool: Grep, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: an explicit single-file target must be searched by ripgrep.

    With the native kernel active this call used to return "No matches found"
    for a file that does contain the pattern (single explicit file root).
    """
    calls: list[list[str]] = []
    original = Grep._run_rg_subprocess

    async def spy(self: Grep, args: list[str], timeout: int):
        calls.append(list(args))
        return await original(self, args, timeout)

    monkeypatch.setattr(Grep, "_run_rg_subprocess", spy)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py") as fh:
        fh.write("def test_function():\n    return 'hello world'\n")
        fh.flush()

        result = await grep_tool(Params(pattern="hello", path=fh.name, output_mode="content"))

    assert not result.is_error
    assert "hello" in str(result.output)
    assert calls, "ripgrep was never spawned"
    assert calls[0][-1] == str(Path(fh.name).resolve())


async def test_tool_always_prepares_the_ripgrep_binary(grep_tool: Grep) -> None:
    """The tool instance always tracks rg/rtk — never the "no binaries" state
    the native branch used to leave behind."""
    assert grep_tool._rg_path is not None or grep_tool._rg_path_task is not None
    assert grep_tool._rtk_path is not None or grep_tool._rtk_path_task is not None


async def test_backup_fallback_paginates_every_mode(grep_tool: Grep) -> None:
    """With ripgrep unavailable the pure-Python fallback still paginates.

    Guards the offset/head_limit block shared by all three output modes in
    ``_backup_grep_search`` (it used to be duplicated per mode).
    """
    grep_tool._rg_path = None
    grep_tool._rg_path_task = None

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        for name in ("a.py", "b.py", "c.py"):
            (root / name).write_text("marker one\nmarker two\n")

        files = await grep_tool(
            Params(
                pattern="marker",
                path=str(root),
                output_mode="files_with_matches",
                head_limit=2,
            )
        )
        assert "Results truncated to 2 lines (total: 3)." in files.message
        assert "Use offset=2 to see more." in files.message
        assert len([ln for ln in str(files.output).splitlines() if ln]) == 2

        counts = await grep_tool(
            Params(
                pattern="marker",
                path=str(root),
                output_mode="count_matches",
                head_limit=2,
            )
        )
        assert "Found 6 total occurrences across 3 files." in counts.message
        assert "Results truncated to 2 lines (total: 3)." in counts.message

        content = await grep_tool(
            Params(pattern="marker", path=str(root), output_mode="content", head_limit=2)
        )
        assert "Results truncated to 2 lines (total: 6)." in content.message

        later = await grep_tool(
            Params(
                pattern="marker",
                path=str(root),
                output_mode="content",
                head_limit=2,
                offset=1,
            )
        )
        assert "Results truncated to 2 lines (total: 6)." in later.message
        assert "Use offset=3 to see more." in later.message
        assert str(later.output).splitlines()[0] == "a.py:2:marker two"


# ---------------------------------------------------------------------------
# pure-Python pattern helpers (previously gated on the native fast path)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("abc", False),
        ("a\nb", True),
        ("a\\nb", True),
        ("a\\\\nb", False),
        ("a\\\\\\nb", True),
        ("\\n", True),
        ("", False),
        ("é\\n", True),  # non-ASCII: the pure-Python body is the only body
    ],
)
def test_pattern_has_regex_newline_is_pure_python(pattern: str, expected: bool) -> None:
    assert _pattern_has_regex_newline(pattern) is expected


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("abc", "abc"),
        ("\\n", r"\r?\n"),
        ("a\\nb", r"a\r?\nb"),
        ("a\\\\nb", "a\\\\nb"),  # literal backslash + n: untouched
        ("a\r\nb", r"a\r?\nb"),
        ("a\nb\\nc", r"a\r?\nb\r?\nc"),
    ],
)
def test_multiline_pattern_rewrites_newlines(pattern: str, expected: str) -> None:
    assert _multiline_pattern(pattern) == expected
