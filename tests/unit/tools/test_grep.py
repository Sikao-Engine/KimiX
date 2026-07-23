"""Tests for Defects 10.1-10.4: Grep improvements."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from kimi_cli.tools.file.grep_local import Params as GrepParams


class TestGrepParams:
    def test_before_context_no_alias_in_schema(self) -> None:
        """Schema should use 'before_context', not '-B'."""
        fields = GrepParams.model_fields
        assert "before_context" in fields

    def test_before_context_alias_still_works(self) -> None:
        params = GrepParams(pattern="test", **{"-B": 3})
        assert params.before_context == 3

    def test_default_head_limit_is_500(self) -> None:
        params = GrepParams(pattern="test")
        assert params.head_limit == 500

    def test_head_limit_unlimited(self) -> None:
        params = GrepParams(pattern="test", head_limit=0)
        assert params.head_limit == 0


class TestGrepOutputModeSynonyms:
    @pytest.mark.parametrize("valid", ["files_with_matches", "count_matches", "content"])
    def test_canonical_modes_accepted(self, valid: str) -> None:
        GrepParams(pattern="test", output_mode=valid)

    @pytest.mark.parametrize("invalid", ["files", "file", "list", "count", "full"])
    def test_old_synonyms_rejected(self, invalid: str) -> None:
        with pytest.raises(ValidationError) as exc:
            GrepParams(pattern="test", output_mode=invalid)
        assert "files_with_matches" in str(exc.value)
