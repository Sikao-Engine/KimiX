"""Tests for Defects 8.1-8.3: WriteFile improvements."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from kimi_cli.tools.file.write import Params as WriteFileParams


class TestWriteFileParams:
    def test_auto_fix_json_default_true(self) -> None:
        params = WriteFileParams(path="f.json", content="{}")
        assert params.auto_fix_json is True

    def test_auto_fix_json_can_be_false(self) -> None:
        params = WriteFileParams(path="f.json", content="{}", auto_fix_json=False)
        assert params.auto_fix_json is False

    def test_mkdir_default_true(self) -> None:
        params = WriteFileParams(path="f.txt", content="hello")
        assert params.mkdir is True

    def test_mkdir_can_be_false(self) -> None:
        params = WriteFileParams(path="f.txt", content="hello", mkdir=False)
        assert params.mkdir is False

    def test_show_diff_default_false(self) -> None:
        params = WriteFileParams(path="f.txt", content="hello")
        assert params.show_diff is False

    def test_show_diff_can_be_true(self) -> None:
        params = WriteFileParams(path="f.txt", content="hello", show_diff=True)
        assert params.show_diff is True
