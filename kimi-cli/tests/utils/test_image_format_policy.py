"""Tests for kimi_cli.utils.image_format_policy — the provider-accepted
image format gate that prevents session poisoning."""

from __future__ import annotations

from kimi_cli.utils.image_format_policy import (
    MODEL_ACCEPTED_IMAGE_MIMES,
    build_image_conversion_guidance,
    is_model_accepted_image_mime,
    normalize_image_mime,
)


class TestNormalizeImageMime:
    def test_lowercases(self):
        assert normalize_image_mime("IMAGE/PNG") == "image/png"

    def test_strips_whitespace(self):
        assert normalize_image_mime("  image/webp  ") == "image/webp"

    def test_strips_parameters(self):
        assert normalize_image_mime("image/jpeg; charset=utf-8") == "image/jpeg"

    def test_maps_jpg_alias(self):
        assert normalize_image_mime("image/jpg") == "image/jpeg"
        assert normalize_image_mime("IMAGE/JPG") == "image/jpeg"


class TestIsModelAcceptedImageMime:
    def test_closed_set_members(self):
        assert frozenset({
            "image/png",
            "image/jpeg",
            "image/gif",
            "image/webp",
        }) == MODEL_ACCEPTED_IMAGE_MIMES
        for mime in ("image/png", "image/jpeg", "image/gif", "image/webp"):
            assert is_model_accepted_image_mime(mime)

    def test_normalization_applies(self):
        assert is_model_accepted_image_mime("IMAGE/JPG")
        assert is_model_accepted_image_mime("image/jpeg; charset=utf-8")

    def test_rejects_unsupported(self):
        for mime in (
            "image/avif",
            "image/heic",
            "image/heif",
            "image/bmp",
            "image/tiff",
            "image/x-icon",
            "image/svg+xml",
        ):
            assert not is_model_accepted_image_mime(mime)


class TestBuildImageConversionGuidance:
    def test_macos_uses_sips(self):
        guidance = build_image_conversion_guidance("/tmp/photo.bmp", "image/bmp", "macOS")
        assert guidance == (
            '"/tmp/photo.bmp" is an image/bmp image, which the provider does not accept. '
            "Convert it to JPEG first, then read the converted file. "
            'On macOS: sips -s format jpeg "/tmp/photo.bmp" --out "/tmp/photo.jpg"'
        )

    def test_linux_uses_imagemagick(self):
        guidance = build_image_conversion_guidance("/tmp/photo.bmp", "image/bmp", "Linux")
        assert guidance == (
            '"/tmp/photo.bmp" is an image/bmp image, which the provider does not accept. '
            "Convert it to JPEG first, then read the converted file. "
            'On Linux, with ImageMagick: magick "/tmp/photo.bmp" "/tmp/photo.jpg"'
        )

    def test_linux_heic_names_heif_convert(self):
        guidance = build_image_conversion_guidance("/tmp/photo.heic", "image/heic", "Linux")
        assert guidance == (
            '"/tmp/photo.heic" is an image/heic image, which the provider does not accept. '
            "Convert it to JPEG first, then read the converted file. "
            'On Linux: heif-convert "/tmp/photo.heic" "/tmp/photo.jpg" '
            "(package libheif-examples), or with ImageMagick: "
            'magick "/tmp/photo.heic" "/tmp/photo.jpg"'
        )

    def test_linux_heif_names_heif_convert(self):
        guidance = build_image_conversion_guidance("/tmp/photo.heif", "image/heif", "Linux")
        assert 'heif-convert "/tmp/photo.heif" "/tmp/photo.jpg"' in guidance
        assert "libheif-examples" in guidance

    def test_windows_uses_imagemagick_with_winget_hint(self):
        guidance = build_image_conversion_guidance(
            r"C:\tmp\photo.avif", "image/avif", "Windows"
        )
        assert guidance == (
            r'"C:\tmp\photo.avif" is an image/avif image, which the provider does not accept. '
            "Convert it to JPEG first, then read the converted file. "
            r'On Windows, with ImageMagick: magick "C:\tmp\photo.avif" "C:\tmp\photo.jpg" '
            "(install it first if missing: winget install ImageMagick.ImageMagick)"
        )

    def test_unknown_os_lists_all_options(self):
        guidance = build_image_conversion_guidance("/tmp/photo.heic", "image/heic", "Plan9")
        assert guidance == (
            '"/tmp/photo.heic" is an image/heic image, which the provider does not accept. '
            "Convert it to JPEG first, then read the converted file. "
            'Options: sips -s format jpeg "/tmp/photo.heic" --out "/tmp/photo.jpg" (macOS), '
            'heif-convert "/tmp/photo.heic" "/tmp/photo.jpg" (Linux, package libheif-examples), '
            'or magick "/tmp/photo.heic" "/tmp/photo.jpg" (ImageMagick)'
        )

    def test_unknown_os_without_decoder_omits_heif_convert(self):
        guidance = build_image_conversion_guidance("/tmp/photo.tiff", "image/tiff", "Plan9")
        assert "heif-convert" not in guidance
        assert "(macOS)" in guidance
        assert "(ImageMagick)" in guidance

    def test_converted_path_replaces_extension(self):
        guidance = build_image_conversion_guidance("/tmp/a.b/photo.png.heic", "image/heic", "Linux")
        assert '"/tmp/a.b/photo.png.jpg"' in guidance
