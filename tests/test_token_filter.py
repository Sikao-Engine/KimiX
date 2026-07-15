"""Tests for token filter pipeline: _dedup_output, _truncate_lines, _token_filter_output."""

import pytest
from kimix.tools.common import (
    _dedup_output,
    _truncate_lines,
    _token_filter_output,
)


# ── _dedup_output tests ──────────────────────────────────────────────

def test_dedup_empty():
    assert _dedup_output("") == ""


def test_dedup_single_line():
    assert _dedup_output("hello") == "hello"


def test_dedup_no_repeats():
    out = "a\nb\nc\nd"
    assert _dedup_output(out) == out


def test_dedup_below_threshold():
    # 3 copies, threshold=3 → all pass through
    out = "x\nx\nx"
    assert _dedup_output(out, threshold=3) == out


def test_dedup_above_threshold():
    # 4 copies, threshold=3 → collapsed to single annotated line
    out = "x\n" * 4
    result = _dedup_output(out.strip(), threshold=3)
    assert result == "x  (4 repeats)"


def test_dedup_preserves_order():
    out = "a\nb\na\nb\na\nc"
    # a appears 3× (≤3 threshold) → all pass; b appears 2× → all pass; c appears 1×
    result = _dedup_output(out, threshold=3)
    assert result == out


def test_dedup_interleaved_repeats():
    out = "ERROR\nINFO\nERROR\nINFO\nERROR\nINFO\nERROR"
    # ERROR 4× (>3) → collapsed; INFO 3× (≤3) → passes
    result = _dedup_output(out, threshold=3)
    assert "ERROR  (4 repeats)" in result
    assert result.count("INFO") == 3  # all 3 INFO lines preserved


def test_dedup_large_input():
    # 10,000 lines, 9,900 unique, 100 repeated 100×
    import random
    lines = [f"unique_{i}" for i in range(9900)] + ["repeat_me"] * 100
    random.shuffle(lines)
    out = "\n".join(lines)
    result = _dedup_output(out, threshold=3)
    assert "repeat_me  (100 repeats)" in result
    # Unique lines all present
    for i in range(9900):
        assert f"unique_{i}" in result


# ── _truncate_lines tests ───────────────────────────────────────────

def test_truncate_short_unchanged():
    out = "\n".join(str(i) for i in range(50))
    assert _truncate_lines(out, 100) == out


def test_truncate_exact_boundary():
    out = "\n".join(str(i) for i in range(100))
    assert _truncate_lines(out, 100) == out


def test_truncate_folds_middle():
    lines = [f"line_{i}" for i in range(1000)]
    out = "\n".join(lines)
    result = _truncate_lines(out, 100)
    result_lines = result.splitlines()
    # fold marker present, head lines at start, tail lines at end
    assert "lines omitted" in result
    assert result_lines[0] == "line_0"
    assert result_lines[-1] == "line_999"
    assert "line_49" in result   # last head line (index 49, 0-based)
    assert "line_50" not in result  # first omitted line NOT present


def test_truncate_max_lines_3():
    lines = [f"line_{i}" for i in range(100)]
    out = "\n".join(lines)
    result = _truncate_lines(out, 3)
    # head_n = 1, tail_n = 1, fold
    assert result.startswith("line_0")
    assert result.endswith("line_99")
    assert "lines omitted" in result


def test_truncate_no_output():
    assert _truncate_lines("", 100) == ""


def test_truncate_max_lines_zero():
    out = "a\nb\nc"
    assert _truncate_lines(out, 0) == out  # max_lines <= 0 → no truncation


# ── _token_filter_output integration tests ──────────────────────────

@pytest.mark.asyncio
async def test_token_filter_no_params_passthrough():
    out = "line1\nline2\nline3"
    result, orig_path = await _token_filter_output(
        out, dedup=False, max_lines=None
    )
    assert result == out
    assert orig_path is None  # no filter active → no original saved


@pytest.mark.asyncio
async def test_token_filter_dedup_only():
    out = "ERROR\n" * 10
    result, orig_path = await _token_filter_output(
        out, dedup=True, max_lines=None
    )
    assert "ERROR  (10 repeats)" in result
    assert orig_path is not None  # filter active → original saved


@pytest.mark.asyncio
async def test_token_filter_dedup_disabled():
    out = "ERROR\n" * 10
    result, orig_path = await _token_filter_output(
        out, dedup=False, max_lines=None
    )
    assert result == out  # unchanged
    assert orig_path is None


@pytest.mark.asyncio
async def test_token_filter_truncate_only():
    lines = [f"L{i}" for i in range(500)]
    out = "\n".join(lines)
    result, orig_path = await _token_filter_output(
        out, dedup=False, max_lines=50
    )
    assert "lines omitted" in result
    assert orig_path is not None
    assert "L0" in result
    assert "L499" in result


@pytest.mark.asyncio
async def test_token_filter_all_stages():
    # dedup → truncate
    lines = (
        ["ERROR: timeout"] * 100
        + ["INFO: ok"] * 50
        + ["WARN: check"] * 10
        + ["ERROR: retry"] * 5
    )
    out = "\n".join(lines)
    result, orig_path = await _token_filter_output(
        out, dedup=True, max_lines=3
    )
    assert "ERROR: timeout  (100 repeats)" in result  # first deduped line
    assert "ERROR: retry  (5 repeats)" in result  # last deduped line
    assert "lines omitted" in result  # 4 deduped lines → truncated
    assert orig_path is not None


@pytest.mark.asyncio
async def test_token_filter_saves_original_content():
    out = "original content here\nsecond line"
    result, orig_path = await _token_filter_output(
        out, dedup=True, max_lines=None
    )
    # Read the saved file
    import anyio
    async with await anyio.open_file(orig_path, 'r') as f:
        saved = await f.read()
    assert saved == out


@pytest.mark.asyncio
async def test_token_filter_empty_output():
    result, orig_path = await _token_filter_output(
        "", dedup=True, max_lines=10
    )
    assert result == ""
    # When filter is active, original is saved even for empty output
    assert orig_path is not None


@pytest.mark.asyncio
async def test_token_filter_ansi_stripped_when_dedup_enabled():
    """ANSI escape codes are stripped via rich when dedup=True (merged behavior)."""
    out = "\x1B[31mHello\x1B[0m"
    result, orig_path = await _token_filter_output(
        out, dedup=True, max_lines=None
    )
    assert result == "Hello"
    assert orig_path is not None  # dedup=True → filter active → original saved


@pytest.mark.asyncio
async def test_token_filter_ansi_left_intact_when_dedup_disabled():
    """ANSI codes are left intact when dedup=False (ANSI stripping is merged with dedup)."""
    out = "\x1B[31mHello\x1B[0m"
    result, orig_path = await _token_filter_output(
        out, dedup=False, max_lines=None
    )
    assert result == out  # unchanged
    assert orig_path is None


@pytest.mark.asyncio
async def test_token_filter_ansi_no_ansi_unchanged():
    """dedup=True with no ANSI codes leaves plain text unchanged."""
    out = "plain text without any escape codes\nsecond line"
    result, orig_path = await _token_filter_output(
        out, dedup=True, max_lines=None
    )
    assert result == out
    assert orig_path is not None


@pytest.mark.asyncio
async def test_token_filter_ansi_stripped_before_dedup():
    """ANSI stripping runs BEFORE dedup, so same text with different ANSI wrappers collapses."""
    out = "\x1B[31mERROR\x1B[0m\n\x1B[32mERROR\x1B[0m\n\x1B[31mERROR\x1B[0m\n\x1B[32mERROR\x1B[0m"
    result, orig_path = await _token_filter_output(
        out, dedup=True, max_lines=None
    )
    # After ANSI stripping, all 4 lines become "ERROR" -> dedup collapses to "ERROR  (4 repeats)"
    assert "ERROR  (4 repeats)" in result
    assert orig_path is not None


# ── Param validation tests ──────────────────────────────────────────

def test_powershell_params_new_fields_defaults():
    from kimix.tools.file.bash.pwsh_tool import PowershellParams
    p = PowershellParams(cmd="echo hi")
    assert p.dedup is True
    assert p.max_lines is None


def test_powershell_params_max_lines_min():
    from kimix.tools.file.bash.pwsh_tool import PowershellParams
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        PowershellParams(cmd="echo hi", max_lines=2)


def test_bash_params_new_fields():
    from kimix.tools.file.bash.bash_tool import BashParams
    p = BashParams(cmd="echo hi", dedup=False, max_lines=50)
    assert p.dedup is False
    assert p.max_lines == 50


def test_run_params_new_fields():
    from kimix.tools.file.run import RunParams
    p = RunParams(command="echo hi", dedup=False, max_lines=50)
    assert p.dedup is False
    assert p.max_lines == 50

