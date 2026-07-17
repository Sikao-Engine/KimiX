"""Tests for kimi_cli.utils.image_compress — header-only dimension
sniffing, best-effort downsampling, and region cropping."""

from __future__ import annotations

import random
from io import BytesIO

import pytest
from PIL import Image

import kimi_cli.utils.image_compress as image_compress
from kimi_cli.utils.image_compress import (
    IMAGE_BYTE_BUDGET,
    MAX_IMAGE_EDGE_PX,
    READ_IMAGE_BYTE_BUDGET,
    CropImageFailure,
    CropImageSuccess,
    CropRegion,
    compress_image_for_model,
    crop_image_for_model,
    format_byte_size,
    resolve_max_image_edge_px,
    resolve_read_image_byte_budget,
    sniff_image_dimensions,
)

MAX_EDGE = 2000
BYTE_BUDGET = 256 * 1024


def make_image(fmt: str, size: tuple[int, int], color: tuple[int, int, int] = (51, 102, 204)) -> bytes:
    img = Image.new("RGB", size, color=color)
    buf = BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def make_noisy_png(size: tuple[int, int]) -> bytes:
    # Deterministic true-random pixels — the entropy upper bound for codecs.
    width, height = size
    rng = random.Random(42)
    img = Image.frombytes("RGB", size, rng.randbytes(width * height * 3))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def make_animated_webp_header() -> bytes:
    header = bytearray(30)
    header[0:4] = b"RIFF"
    header[8:12] = b"WEBP"
    header[12:16] = b"VP8X"
    header[20] = 0x02  # ANIM flag
    return bytes(header)


class TestSniffImageDimensions:
    def test_png(self):
        dims = sniff_image_dimensions(make_image("PNG", (3, 4)))
        assert dims is not None
        assert (dims.width, dims.height) == (3, 4)
        assert dims.transposed is False

    def test_gif(self):
        dims = sniff_image_dimensions(make_image("GIF", (5, 6)))
        assert dims is not None
        assert (dims.width, dims.height) == (5, 6)

    def test_bmp(self):
        dims = sniff_image_dimensions(make_image("BMP", (7, 8)))
        assert dims is not None
        assert (dims.width, dims.height) == (7, 8)

    def test_webp_lossy_vp8(self):
        dims = sniff_image_dimensions(make_image("WEBP", (9, 10)))
        assert dims is not None
        assert (dims.width, dims.height) == (9, 10)

    def test_webp_lossless_vp8l(self):
        img = Image.new("RGB", (11, 12), color=(1, 2, 3))
        buf = BytesIO()
        img.save(buf, format="WEBP", lossless=True)
        dims = sniff_image_dimensions(buf.getvalue())
        assert dims is not None
        assert (dims.width, dims.height) == (11, 12)

    def test_webp_vp8x_synthetic(self):
        header = bytearray(30)
        header[0:4] = b"RIFF"
        header[8:12] = b"WEBP"
        header[12:16] = b"VP8X"
        # 13x17 stored minus one, little-endian 24-bit.
        header[24:27] = (12).to_bytes(3, "little")
        header[27:30] = (16).to_bytes(3, "little")
        dims = sniff_image_dimensions(bytes(header))
        assert dims is not None
        assert (dims.width, dims.height) == (13, 17)

    def test_jpeg(self):
        dims = sniff_image_dimensions(make_image("JPEG", (60, 20)))
        assert dims is not None
        assert (dims.width, dims.height) == (60, 20)
        assert dims.transposed is False

    def test_jpeg_exif_orientation_transposes(self):
        img = Image.new("RGB", (60, 20), color=(9, 9, 9))
        exif = Image.Exif()
        exif[0x0112] = 6  # rotate 90° at display time
        buf = BytesIO()
        img.save(buf, format="JPEG", exif=exif)
        dims = sniff_image_dimensions(buf.getvalue())
        assert dims is not None
        assert (dims.width, dims.height) == (20, 60)
        assert dims.transposed is True

    def test_truncated_header_returns_none(self):
        assert sniff_image_dimensions(b"\x89PNG\r\n\x1a\n") is None
        assert sniff_image_dimensions(b"") is None
        assert sniff_image_dimensions(b"not an image") is None


class TestFormatByteSize:
    def test_bytes(self):
        assert format_byte_size(640) == "640 B"
        assert format_byte_size(1023) == "1023 B"

    def test_kilobytes(self):
        assert format_byte_size(1024) == "1 KB"
        assert format_byte_size(128 * 1024) == "128 KB"
        # JS Math.round semantics: half rounds up.
        assert format_byte_size(1536) == "2 KB"
        assert format_byte_size(2560) == "3 KB"

    def test_megabytes(self):
        assert format_byte_size(IMAGE_BYTE_BUDGET) == "3.8 MB"
        assert format_byte_size(4 * 1024 * 1024) == "4.0 MB"


class TestEnvResolvers:
    def test_max_edge_defaults(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("KIMI_IMAGE_MAX_EDGE_PX", raising=False)
        assert resolve_max_image_edge_px() == MAX_IMAGE_EDGE_PX

    def test_max_edge_env_override(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("KIMI_IMAGE_MAX_EDGE_PX", "4000")
        assert resolve_max_image_edge_px() == 4000

    def test_max_edge_env_invalid_values(self, monkeypatch: pytest.MonkeyPatch):
        for bad in ("", "abc", "0", "-5", "2.5", "12px"):
            monkeypatch.setenv("KIMI_IMAGE_MAX_EDGE_PX", bad)
            assert resolve_max_image_edge_px() == MAX_IMAGE_EDGE_PX

    def test_read_byte_budget_defaults(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("KIMI_IMAGE_READ_BYTE_BUDGET", raising=False)
        assert resolve_read_image_byte_budget() == READ_IMAGE_BYTE_BUDGET

    def test_read_byte_budget_env_override(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("KIMI_IMAGE_READ_BYTE_BUDGET", "1024")
        assert resolve_read_image_byte_budget() == 1024


class TestCompressImageForModel:
    def test_fast_path_passthrough(self):
        data = make_image("PNG", (100, 50))
        result = compress_image_for_model(
            data, "image/png", max_edge=MAX_EDGE, byte_budget=BYTE_BUDGET
        )
        assert result.changed is False
        assert result.data == data
        assert result.mime_type == "image/png"
        assert (result.width, result.height) == (100, 50)
        assert (result.original_width, result.original_height) == (100, 50)
        assert result.final_byte_length == len(data)

    def test_empty_input_passthrough(self):
        result = compress_image_for_model(b"", "image/png")
        assert result.changed is False
        assert result.final_byte_length == 0

    def test_non_recodable_mime_passthrough(self):
        data = make_image("GIF", (3000, 3000))
        result = compress_image_for_model(
            data, "image/gif", max_edge=MAX_EDGE, byte_budget=BYTE_BUDGET
        )
        assert result.changed is False
        assert result.data == data
        assert (result.width, result.height) == (3000, 3000)

    def test_animated_webp_passthrough(self):
        data = make_animated_webp_header()
        result = compress_image_for_model(
            data, "image/webp", max_edge=MAX_EDGE, byte_budget=10
        )
        assert result.changed is False
        assert result.data == data

    def test_corrupt_image_passthrough(self):
        # Sniffs as a small PNG (guards pass) but the decoder fails.
        data = make_image("PNG", (20, 20))[:40] + b"garbage"
        result = compress_image_for_model(
            data, "image/png", max_edge=MAX_EDGE, byte_budget=10
        )
        assert result.changed is False
        assert result.data == data

    def test_large_solid_png_compresses_to_lossless_png(self):
        data = make_image("PNG", (2200, 2200))
        result = compress_image_for_model(
            data, "image/png", max_edge=MAX_EDGE, byte_budget=BYTE_BUDGET
        )
        assert result.changed is True
        assert result.mime_type == "image/png"
        assert (result.width, result.height) == (2000, 2000)
        assert (result.original_width, result.original_height) == (2200, 2200)
        assert result.original_byte_length == len(data)
        assert result.final_byte_length == len(result.data)
        assert result.final_byte_length <= BYTE_BUDGET

    def test_noisy_png_falls_back_to_jpeg_within_budget(self):
        data = make_noisy_png((2200, 1100))
        result = compress_image_for_model(
            data, "image/png", max_edge=MAX_EDGE, byte_budget=BYTE_BUDGET
        )
        assert result.changed is True
        assert result.mime_type == "image/jpeg"
        assert result.final_byte_length <= BYTE_BUDGET
        assert max(result.width, result.height) <= MAX_EDGE
        assert (result.original_width, result.original_height) == (2200, 1100)

    def test_jpeg_source_stays_jpeg(self):
        img = Image.frombytes("RGB", (2100, 2100), bytes(2100 * 2100 * 3 // 2) * 2)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=95)
        data = buf.getvalue()
        result = compress_image_for_model(
            data, "image/jpeg", max_edge=MAX_EDGE, byte_budget=BYTE_BUDGET
        )
        assert result.mime_type == "image/jpeg"
        if result.changed:
            assert max(result.width, result.height) <= MAX_EDGE
            assert result.final_byte_length <= BYTE_BUDGET

    def test_unhelpful_reencode_returns_unchanged(self):
        # A tiny image that cannot possibly meet a 5-byte budget: every
        # re-encode is larger than the input, so the original is kept.
        data = make_image("PNG", (10, 10))
        result = compress_image_for_model(
            data, "image/png", max_edge=MAX_EDGE, byte_budget=5
        )
        assert result.changed is False
        assert result.data == data

    def test_pixel_count_guard(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(image_compress, "MAX_DECODE_PIXELS", 100)
        data = make_image("PNG", (20, 20))  # 400 px > 100
        result = compress_image_for_model(
            data, "image/png", max_edge=MAX_EDGE, byte_budget=1
        )
        assert result.changed is False
        assert result.data == data

    def test_max_decode_bytes_guard(self):
        data = make_image("PNG", (100, 100))
        result = compress_image_for_model(
            data, "image/png", max_edge=MAX_EDGE, byte_budget=1, max_decode_bytes=10
        )
        assert result.changed is False
        assert result.data == data

    def test_exif_orientation_reported_from_decode(self):
        img = Image.new("RGB", (2200, 1000), color=(7, 7, 7))
        exif = Image.Exif()
        exif[0x0112] = 6
        buf = BytesIO()
        img.save(buf, format="JPEG", exif=exif)
        data = buf.getvalue()
        result = compress_image_for_model(
            data, "image/jpeg", max_edge=MAX_EDGE, byte_budget=BYTE_BUDGET
        )
        assert result.changed is True
        # Decoded (display) space is authoritative: the 2200x1000 frame is
        # transposed to 1000x2200 before any resizing.
        assert (result.original_width, result.original_height) == (1000, 2200)
        assert max(result.width, result.height) <= MAX_EDGE


class TestCropImageForModel:
    def test_crop_clamps_to_bounds(self):
        data = make_image("PNG", (2200, 2200))
        outcome = crop_image_for_model(
            data, "image/png", CropRegion(x=2000, y=2000, width=400, height=400)
        )
        assert isinstance(outcome, CropImageSuccess)
        assert outcome.region == CropRegion(x=2000, y=2000, width=200, height=200)
        assert (outcome.width, outcome.height) == (200, 200)
        assert (outcome.original_width, outcome.original_height) == (2200, 2200)
        assert outcome.resized is False
        sniffed = sniff_image_dimensions(outcome.data)
        assert sniffed is not None
        assert (sniffed.width, sniffed.height) == (200, 200)

    def test_crop_out_of_bounds_names_original_size(self):
        data = make_image("PNG", (2200, 2200))
        outcome = crop_image_for_model(
            data, "image/png", CropRegion(x=2200, y=0, width=10, height=10)
        )
        assert isinstance(outcome, CropImageFailure)
        assert outcome.error == (
            "Region (x=2200, y=0, width=10, height=10) lies outside the 2200x2200 image."
        )

    def test_crop_oversized_region_is_downscaled(self):
        data = make_image("PNG", (4000, 3000))
        outcome = crop_image_for_model(
            data, "image/png", CropRegion(x=0, y=0, width=4000, height=3000)
        )
        assert isinstance(outcome, CropImageSuccess)
        assert outcome.resized is True
        assert max(outcome.width, outcome.height) <= MAX_EDGE
        assert (outcome.original_width, outcome.original_height) == (4000, 3000)

    def test_crop_jpeg_source_encodes_jpeg(self):
        data = make_image("JPEG", (500, 500))
        outcome = crop_image_for_model(
            data, "image/jpeg", CropRegion(x=0, y=0, width=100, height=100)
        )
        assert isinstance(outcome, CropImageSuccess)
        assert outcome.mime_type == "image/jpeg"

    def test_crop_skip_resize_native_resolution(self):
        data = make_image("PNG", (2200, 2200))
        outcome = crop_image_for_model(
            data,
            "image/png",
            CropRegion(x=10, y=10, width=500, height=400),
            skip_resize=True,
        )
        assert isinstance(outcome, CropImageSuccess)
        assert (outcome.width, outcome.height) == (500, 400)
        assert outcome.resized is False
        assert outcome.mime_type == "image/png"

    def test_crop_skip_resize_over_budget_fails_with_exact_bytes(self):
        data = make_noisy_png((2200, 1100))
        outcome = crop_image_for_model(
            data,
            "image/png",
            CropRegion(x=0, y=0, width=2000, height=1000),
            skip_resize=True,
            byte_budget=1024,
        )
        assert isinstance(outcome, CropImageFailure)
        assert outcome.error.startswith("The cropped region encodes to ")
        assert "over the 1024-byte (1 KB) per-image limit." in outcome.error
        assert outcome.error.endswith("Choose a smaller region, or allow downscaling.")

    def test_crop_rejects_non_recodable_mime(self):
        data = make_image("GIF", (100, 100))
        outcome = crop_image_for_model(data, "image/gif", CropRegion(0, 0, 10, 10))
        assert isinstance(outcome, CropImageFailure)
        assert outcome.error == (
            "Cropping is only supported for PNG, JPEG, and WebP images; got image/gif."
        )

    def test_crop_rejects_animated_webp(self):
        outcome = crop_image_for_model(
            make_animated_webp_header(), "image/webp", CropRegion(0, 0, 10, 10)
        )
        assert isinstance(outcome, CropImageFailure)
        assert outcome.error == "Cropping is not supported for animated WebP images."

    def test_crop_rejects_empty_input(self):
        outcome = crop_image_for_model(b"", "image/png", CropRegion(0, 0, 10, 10))
        assert isinstance(outcome, CropImageFailure)
        assert outcome.error == "The image is empty."

    def test_crop_rejects_undecodable_bytes(self):
        # Valid PNG signature and IHDR (small dims pass the decode guards),
        # but the file is truncated so the decoder fails.
        data = make_image("PNG", (20, 20))[:40] + b"garbage"
        outcome = crop_image_for_model(data, "image/png", CropRegion(0, 0, 10, 10))
        assert isinstance(outcome, CropImageFailure)
        assert outcome.error.startswith("Failed to decode the image for cropping: ")

    def test_crop_pixel_count_guard(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(image_compress, "MAX_DECODE_PIXELS", 100)
        data = make_image("PNG", (20, 20))
        outcome = crop_image_for_model(data, "image/png", CropRegion(0, 0, 10, 10))
        assert isinstance(outcome, CropImageFailure)
        assert outcome.error == (
            "The image (20x20 pixels) is too large to decode for cropping."
        )

    def test_crop_max_decode_bytes_guard(self):
        data = make_image("PNG", (100, 100))
        outcome = crop_image_for_model(
            data, "image/png", CropRegion(0, 0, 10, 10), max_decode_bytes=10
        )
        assert isinstance(outcome, CropImageFailure)
        assert outcome.error == "The image is too large to decode for cropping."
