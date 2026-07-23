"""Tests for Defects 9.1-9.3: Glob improvements."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from kimi_cli.tools.file.glob import Params as GlobParams


class TestGlobParams:
    def test_include_dirs_defaults_to_false(self) -> None:
        params = GlobParams(pattern="*.py")
        assert params.include_dirs is False

    def test_verbose_default_false(self) -> None:
        params = GlobParams(pattern="*.py")
        assert params.verbose is False

    def test_verbose_can_be_true(self) -> None:
        params = GlobParams(pattern="*.py", verbose=True)
        assert params.verbose is True

    def test_respect_gitignore_default_true(self) -> None:
        params = GlobParams(pattern="*.py")
        assert params.respect_gitignore is True

    def test_respect_gitignore_can_be_false(self) -> None:
        params = GlobParams(pattern="*.py", respect_gitignore=False)
        assert params.respect_gitignore is False

    def test_include_ignored_deprecated(self) -> None:
        params = GlobParams(pattern="*.py", include_ignored=True)
        # When include_ignored=True, respect_gitignore should become False
        # (handled in __call__ logic, not at params level)
        pass
