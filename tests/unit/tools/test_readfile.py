"""Tests for Defects 5.1-5.5: ReadFile improvements."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from kimi_cli.tools.file.read import Params as ReadFileParams


class TestReadFileParams:
    def test_scalar_params_only(self) -> None:
        params = ReadFileParams(path="file.txt", line_offset=1, n_lines=100,
                                max_char=8000, char_offset=0)
        assert isinstance(params.line_offset, int)
        assert isinstance(params.n_lines, int)

    def test_glob_param_accepted(self) -> None:
        params = ReadFileParams(path="*.py", glob=True)
        assert params.glob is True

    def test_glob_false_default(self) -> None:
        params = ReadFileParams(path="file.txt")
        assert params.glob is False

    def test_show_line_numbers_default_true(self) -> None:
        params = ReadFileParams(path="file.txt")
        assert params.show_line_numbers is True

    def test_show_line_numbers_can_be_false(self) -> None:
        params = ReadFileParams(path="file.txt", show_line_numbers=False)
        assert params.show_line_numbers is False

    def test_default_max_char_is_16000(self) -> None:
        params = ReadFileParams(path="file.txt")
        assert params.max_char == 16000

    def test_line_offset_cannot_be_zero(self) -> None:
        with pytest.raises(ValidationError):
            ReadFileParams(path="file.txt", line_offset=0)

    def test_line_offset_below_min(self) -> None:
        with pytest.raises(ValidationError):
            ReadFileParams(path="file.txt", line_offset=-6000)

    def test_multi_file_read_broadcasts_params(self) -> None:
        params = ReadFileParams(path=["a.txt", "b.txt"], line_offset=5, n_lines=10)
        assert params.line_offset == 5
        assert params.n_lines == 10
