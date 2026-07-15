"""Tests for OUTPUT_LIMIT change (16384 → 4096) and original file saving."""

import pytest

from kimix.tools.common import (
    OUTPUT_LIMIT,
    _maybe_export_output_async,
    _build_session_output_block,
)


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

