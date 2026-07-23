"""Tests for Defects 6.1-6.3: ReadMediaFile improvements."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from kimi_cli.tools.file.read_media import Params as ReadMediaFileParams


class TestReadMediaFileControls:
    def test_max_dimension_accepted(self) -> None:
        params = ReadMediaFileParams(path="img.png", max_dimension=1024)
        assert params.max_dimension == 1024

    def test_quality_accepted(self) -> None:
        params = ReadMediaFileParams(path="img.png", quality=90)
        assert params.quality == 90

    def test_quality_bounds_enforced(self) -> None:
        with pytest.raises(ValidationError):
            ReadMediaFileParams(path="img.png", quality=0)
        with pytest.raises(ValidationError):
            ReadMediaFileParams(path="img.png", quality=101)

    def test_quality_default_85(self) -> None:
        params = ReadMediaFileParams(path="img.png")
        assert params.quality == 85


class TestReadMediaFileInfoOnly:
    def test_info_only_default_false(self) -> None:
        params = ReadMediaFileParams(path="img.png")
        assert params.info_only is False

    def test_info_only_can_be_true(self) -> None:
        params = ReadMediaFileParams(path="img.png", info_only=True)
        assert params.info_only is True

    def test_info_only_and_full_resolution_conflict(self) -> None:
        with pytest.raises(ValidationError, match="Cannot set both"):
            ReadMediaFileParams(path="img.png", full_resolution=True, info_only=True)


class TestReadMediaFileRegionPct:
    def test_region_pct_accepted(self) -> None:
        params = ReadMediaFileParams(path="img.png", region_pct="10,10,50,50")
        assert params.region_pct == "10,10,50,50"

    def test_region_pct_mutually_exclusive_with_region(self) -> None:
        from kimi_cli.tools.file.read_media import Region
        with pytest.raises(ValidationError, match="not both"):
            ReadMediaFileParams(
                path="img.png",
                region=Region(x=0, y=0, width=10, height=10),
                region_pct="10,10,50,50",
            )


class TestReadMediaFileAutoConvert:
    def test_auto_convert_defaults_to_true(self) -> None:
        params = ReadMediaFileParams(path="img.avif")
        assert params.auto_convert is True

    def test_auto_convert_can_be_false(self) -> None:
        params = ReadMediaFileParams(path="img.avif", auto_convert=False)
        assert params.auto_convert is False
