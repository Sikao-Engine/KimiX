"""Tests for kimix.tools.check_fmt JSON/XML validation functions."""

import json
from pathlib import Path

import pytest

from kimix.tools.check_fmt import check_json, check_json_str


# ---------------------------------------------------------------------------
# check_json — file-based
# ---------------------------------------------------------------------------


def test_check_json_valid(tmp_path: Path) -> None:
    """Valid JSON file returns None."""
    fp = tmp_path / "valid.json"
    fp.write_text('{"name": "test", "value": 42}', encoding="utf-8")
    assert check_json(str(fp)) is None


def test_check_json_valid_with_callback(tmp_path: Path) -> None:
    """Valid JSON file triggers callback with parsed data."""
    fp = tmp_path / "valid.json"
    fp.write_text('{"name": "test"}', encoding="utf-8")
    seen: list[object] = []

    def cb(data: object) -> None:
        seen.append(data)

    result = check_json(str(fp), json_callback=cb)
    assert result is None
    assert len(seen) == 1
    assert isinstance(seen[0], dict)
    assert seen[0] == {"name": "test"}  # type: ignore[comparison-overlap]


def test_check_json_invalid_syntax(tmp_path: Path) -> None:
    """Invalid JSON file returns an error message mentioning the line/col."""
    fp = tmp_path / "bad.json"
    fp.write_text('{"name": "test", }', encoding="utf-8")  # trailing comma
    err = check_json(str(fp))
    assert err is not None
    assert "JSON decode error" in err


def test_check_json_file_not_found(tmp_path: Path) -> None:
    """Non-existent file returns an error."""
    err = check_json(str(tmp_path / "nope.json"))
    assert err is not None
    assert "Failed to validate" in err or "decode" in err or "found" in err or "exist" in err


# ---------------------------------------------------------------------------
# check_json_str — string-based
# ---------------------------------------------------------------------------


def test_check_json_str_valid() -> None:
    """Valid JSON string returns None."""
    assert check_json_str('[1, 2, 3]') is None


def test_check_json_str_valid_with_callback() -> None:
    """Valid JSON string triggers callback."""
    seen: list[object] = []

    def cb(data: object) -> None:
        seen.append(data)

    result = check_json_str('{"a": 1}', json_callback=cb)
    assert result is None
    assert seen == [{"a": 1}]


def test_check_json_str_invalid() -> None:
    """Invalid JSON string returns an error."""
    err = check_json_str("{broken")
    assert err is not None
    assert "JSON decode error" in err


def test_check_json_str_empty() -> None:
    """Empty string should produce an error."""
    err = check_json_str("")
    assert err is not None
