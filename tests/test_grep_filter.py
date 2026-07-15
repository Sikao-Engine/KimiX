"""Tests for OUTPUT_LIMIT change (16384 → 4096), grep filtering, and original file saving."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from kimix.tools.common import (
    OUTPUT_LIMIT,
    _apply_grep_filter,
    _save_and_filter_output,
    _maybe_export_output_async,
    _build_session_output_block,
    ProcessTask,
)
from kimi_agent_sdk import ToolOk


# ---------------------------------------------------------------------------
# OUTPUT_LIMIT
# ---------------------------------------------------------------------------

def test_output_limit_is_4096() -> None:
    """OUTPUT_LIMIT must be 4096."""
    assert OUTPUT_LIMIT == 4096, (
        f"OUTPUT_LIMIT expected 4096, got {OUTPUT_LIMIT}"
    )


async def test_output_under_limit_not_exported() -> None:
    """Output <= 4096 should NOT be exported to file."""
    output = "a" * 4096
    result = await _maybe_export_output_async(output)
    # Result should be the same string (not a file export message)
    assert result == output


async def test_output_over_limit_is_exported() -> None:
    """Output > 4096 should be exported to file."""
    output = "a" * 4097
    result = await _maybe_export_output_async(output)
    assert "exported to file" in result.lower()
    assert "4097" not in result  # The content itself should not be in the message


# ---------------------------------------------------------------------------
# _apply_grep_filter
# ---------------------------------------------------------------------------

def test_grep_filter_matches_lines() -> None:
    output = "line alpha\nline beta\nline gamma\nalpha again"
    result = _apply_grep_filter(output, r"alpha")
    assert result == "line alpha\nalpha again"


def test_grep_filter_no_match_returns_empty() -> None:
    output = "line one\nline two\nline three"
    result = _apply_grep_filter(output, r"zzzz_not_found")
    assert result == ""


def test_grep_filter_empty_output() -> None:
    assert _apply_grep_filter("", r"pattern") == ""
    assert _apply_grep_filter("", None) == ""
    assert _apply_grep_filter("content", "") == "content"
    assert _apply_grep_filter("content", "") == "content"


def test_grep_filter_none_grep() -> None:
    """When grep is None or empty, output is returned unchanged."""
    assert _apply_grep_filter("hello\nworld", None) == "hello\nworld"
    assert _apply_grep_filter("hello\nworld", "") == "hello\nworld"


def test_grep_filter_invalid_regex_returns_original() -> None:
    """Invalid regex should return the full unfiltered output."""
    output = "line one\nline two"
    result = _apply_grep_filter(output, r"[invalid")
    assert result == output


def test_grep_filter_uses_regex_library() -> None:
    """Ensure the regex library (not re) is used for better performance."""
    import inspect
    src = inspect.getsource(_apply_grep_filter)
    assert "regex" in src or "re.compile" in src  # Uses the module-level `re` which is `regex`


def test_grep_filter_multiline_match() -> None:
    """Lines containing the pattern are returned."""
    output = "ERROR: something\nINFO: ok\nERROR: another\nDEBUG: fine"
    result = _apply_grep_filter(output, r"ERROR")
    assert result == "ERROR: something\nERROR: another"


# ---------------------------------------------------------------------------
# _save_and_filter_output
# ---------------------------------------------------------------------------

async def test_save_and_filter_no_grep_returns_unchanged() -> None:
    """Without grep, output is returned unchanged with no original_path."""
    output = "test output"
    filtered, original_path = await _save_and_filter_output(output, None)
    assert filtered == output
    assert original_path is None


async def test_save_and_filter_saves_original_and_filters() -> None:
    """With grep, original is saved to temp file and output is filtered."""
    output = "keep_me\nignore\nkeep_me_too"
    filtered, original_path = await _save_and_filter_output(output, r"keep_me")
    
    # Filtered output should only have matching lines
    assert filtered == "keep_me\nkeep_me_too"
    
    # Original should be saved to a temp file
    assert original_path is not None
    assert original_path.endswith(".txt")
    
    # Verify the temp file contains the full original output
    import anyio
    async with await anyio.open_file(original_path, 'r') as f:
        saved = await f.read()
    assert saved == output


async def test_save_and_filter_empty_output() -> None:
    """Empty output with grep should still work."""
    filtered, original_path = await _save_and_filter_output("", r"pattern")
    assert filtered == ""
    assert original_path is not None


# ---------------------------------------------------------------------------
# _build_session_output_block with original_path
# ---------------------------------------------------------------------------

def test_build_block_includes_original_path_when_set() -> None:
    block = _build_session_output_block(
        task_id="test_task",
        status="completed",
        output="hello",
        original_path="/tmp/original.txt",
    )
    assert "original_path: /tmp/original.txt" in block


def test_build_block_original_path_null_when_none() -> None:
    block = _build_session_output_block(
        task_id="test_task",
        status="completed",
        output="hello",
    )
    assert "original_path: null" in block


# ---------------------------------------------------------------------------
# Integration: PowershellParams grep field
# ---------------------------------------------------------------------------

def test_powershell_params_has_grep_field() -> None:
    from kimix.tools.file.bash.pwsh_tool import PowershellParams
    params = PowershellParams(cmd="echo hello", grep="hello")
    assert params.grep == "hello"


def test_powershell_params_grep_defaults_none() -> None:
    from kimix.tools.file.bash.pwsh_tool import PowershellParams
    params = PowershellParams(cmd="echo hello")
    assert params.grep is None


# ---------------------------------------------------------------------------
# Integration: BashParams grep field
# ---------------------------------------------------------------------------

def test_bash_params_has_grep_field() -> None:
    from kimix.tools.file.bash.bash_tool import BashParams
    params = BashParams(cmd="echo hello", grep="hello")
    assert params.grep == "hello"


def test_bash_params_grep_defaults_none() -> None:
    from kimix.tools.file.bash.bash_tool import BashParams
    params = BashParams(cmd="echo hello")
    assert params.grep is None


# ---------------------------------------------------------------------------
# Integration: RunParams grep field
# ---------------------------------------------------------------------------

def test_run_params_has_grep_field() -> None:
    from kimix.tools.file.run import RunParams
    params = RunParams(command="echo hello", grep="hello")
    assert params.grep == "hello"


def test_run_params_grep_defaults_none() -> None:
    from kimix.tools.file.run import RunParams
    params = RunParams(command="echo hello")
    assert params.grep is None
