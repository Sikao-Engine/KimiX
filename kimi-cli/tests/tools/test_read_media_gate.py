"""C06 `read_image` (ReadMediaFile) behavioural gate probes (plan §4.4 C06).

Adversarial probes for the `Params` model, the validation error paths, the
`SkipThisTool` capability gate, and the image-delivery-limit error path.

The last one is the regression probe for finding **F-01**: the
`_build_image_delivery_limit_error` helper was called but never defined, so the
"still over budget after mipmap" path raised `NameError` instead of returning a
`ToolError`.
"""

from __future__ import annotations

from io import BytesIO
from typing import cast
from unittest.mock import MagicMock

import pytest
from inline_snapshot import snapshot
from kaos.path import KaosPath
from PIL import Image
from pydantic import ValidationError

import kimi_cli.tools.file.read_media as read_media_module
from kimi_cli.llm import ModelCapability
from kimi_cli.soul.agent import Runtime
from kimi_cli.tools.file.read_media import Params, ReadMediaFile
from kimi_cli.tools.file.read_media import _build_image_delivery_limit_error


def _make_png(size: tuple[int, int], color: tuple[int, int, int] = (51, 102, 204)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Params: aliases and cross-field validation
# --------------------------------------------------------------------------- #


def test_file_path_and_path_alias_parity() -> None:
    """`path` is an accepted alias of `file_path` and both must round-trip."""
    via_path = Params(path="a.png")
    via_field = Params(file_path="a.png")
    assert via_path.file_path == via_field.file_path == "a.png"


def test_region_and_region_pct_together_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        Params(
            path="a.png",
            region={"x": 0, "y": 0, "width": 1, "height": 1},
            region_pct="0,0,1,1",
        )
    assert "not both" in str(exc.value)


def test_full_resolution_and_info_only_together_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        Params(path="a.png", full_resolution=True, info_only=True)
    assert "full_resolution=True and info_only=True" in str(exc.value)


@pytest.mark.parametrize("quality", [0, 101, -1])
def test_quality_out_of_bounds_rejected(quality: int) -> None:
    with pytest.raises(ValidationError):
        Params(path="a.png", quality=quality)


@pytest.mark.parametrize("quality", [1, 50, 100])
def test_quality_in_bounds_accepted(quality: int) -> None:
    assert Params(path="a.png", quality=quality).quality == quality


@pytest.mark.parametrize("region_pct", ["nonsense", "1,2,3", "1,2,3,4,5", "a,b,c,d"])
async def test_region_pct_malformed_returns_tool_error(
    read_media_file_tool: ReadMediaFile, temp_work_dir: KaosPath, region_pct: str
) -> None:
    """Malformed `region_pct` is rejected at call time with a precise brief.

    NOTE: the rejection happens in ``__call__``, not in the pydantic model, so the
    JSON schema advertised to the model still accepts any string here (finding F-27).
    """
    image = temp_work_dir / "sample.png"
    await image.write_bytes(_make_png((8, 8)))
    result = await read_media_file_tool(Params(path=str(image), region_pct=region_pct))
    assert result.is_error
    assert result.brief == snapshot("Invalid region_pct")


def test_region_pct_valid_accepted() -> None:
    assert Params(path="a.png", region_pct="0,0,50,50").region_pct == "0,0,50,50"


def test_region_negative_or_zero_size_rejected() -> None:
    with pytest.raises(ValidationError):
        Params(path="a.png", region={"x": -1, "y": 0, "width": 1, "height": 1})
    with pytest.raises(ValidationError):
        Params(path="a.png", region={"x": 0, "y": 0, "width": 0, "height": 1})


# --------------------------------------------------------------------------- #
# Error paths
# --------------------------------------------------------------------------- #


async def test_missing_file_returns_tool_error(
    read_media_file_tool: ReadMediaFile, temp_work_dir: KaosPath
) -> None:
    result = await read_media_file_tool(Params(path=str(temp_work_dir / "nope.png")))
    assert result.is_error
    assert "nope.png" in result.message


async def test_directory_path_returns_tool_error(
    read_media_file_tool: ReadMediaFile, temp_work_dir: KaosPath
) -> None:
    result = await read_media_file_tool(Params(path=str(temp_work_dir)))
    assert result.is_error


async def test_unsupported_extension_returns_tool_error(
    read_media_file_tool: ReadMediaFile, temp_work_dir: KaosPath
) -> None:
    path = temp_work_dir / "notes.unknownext"
    await path.write_bytes(b"hello")
    result = await read_media_file_tool(Params(path=str(path)))
    assert result.is_error


# --------------------------------------------------------------------------- #
# Capability gate
# --------------------------------------------------------------------------- #


def test_skip_this_tool_when_model_has_no_media_capability(runtime: Runtime) -> None:
    assert runtime.llm is not None
    runtime.llm.capabilities = cast(set[ModelCapability], set())
    with pytest.raises(Exception) as exc:
        ReadMediaFile(runtime)
    assert "SkipThisTool" in type(exc.value).__name__


def test_tool_is_constructible_with_image_capability(runtime: Runtime) -> None:
    assert runtime.llm is not None
    runtime.llm.capabilities = cast(set[ModelCapability], {"image_in"})
    tool = ReadMediaFile(runtime)
    assert tool.name == "read_image"
    assert tool.params is Params


# --------------------------------------------------------------------------- #
# F-01 regression: the delivery-limit path
# --------------------------------------------------------------------------- #


def test_delivery_limit_error_builder_is_defined_and_informative() -> None:
    """The helper must exist (it used to be referenced but never defined)."""
    assert callable(_build_image_delivery_limit_error)
    message = _build_image_delivery_limit_error(9_000_000, 4_000_000, 2048)
    assert "9000000" in message
    assert "4000000" in message
    assert "2048" in message
    assert "region" in message


async def test_delivery_limit_path_returns_tool_error_not_name_error(
    read_media_file_tool: ReadMediaFile,
    temp_work_dir: KaosPath,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force the "normal compressor over budget AND mipmap fallback failed" path."""
    image_file = temp_work_dir / "sample.png"
    await image_file.write_bytes(_make_png((64, 64)))

    over_budget = MagicMock()
    over_budget.final_byte_length = 9_000_000
    over_budget.width = 4096
    over_budget.height = 4096
    over_budget.original_width = 4096
    over_budget.original_height = 4096
    over_budget.mime_type = "image/png"
    over_budget.data = b"x"

    monkeypatch.setattr(read_media_module, "compress_image_for_model", lambda *a, **k: over_budget)
    monkeypatch.setattr(read_media_module, "_try_mipmap_fallback", lambda *a, **k: None)

    result = await read_media_file_tool(Params(path=str(image_file)))

    assert result.is_error, "the delivery-limit path must return a ToolError, never raise"
    assert result.brief == snapshot("Image too large")
    assert "still too large to deliver" in result.message
    assert "region" in result.message
