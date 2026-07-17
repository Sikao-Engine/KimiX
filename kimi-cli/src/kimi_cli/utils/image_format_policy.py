"""Provider-accepted image formats — the single source of truth.

Model providers accept only PNG, JPEG, GIF, and WebP image blocks. An
``image_url`` part carrying any other MIME (AVIF, HEIC, BMP, TIFF, ICO, …)
is rejected by the API — and because prompts and tool results persist in
the session history, that one part makes every subsequent request fail
too ("session poisoning"). Every ingestion point therefore refuses
unsupported formats instead of passing the bytes through: ReadMediaFile
refuses with a conversion command the model can run and then reads the
converted file.

The policy is deliberately a closed set, not a denylist: a format is only
ever sent when it is known to be accepted. Supporting a new format means
adding it to :data:`MODEL_ACCEPTED_IMAGE_MIMES`; tailoring the refusal
guidance for a newly-seen unsupported format means adding one row to
:data:`_UNSUPPORTED_IMAGE_FORMATS`.

Inbound MIME strings are normalized for the DECISION
(:func:`normalize_image_mime`: case, whitespace, ``image/jpg``), but every
call site must forward the CANONICAL MIME into the session — strict
provider whitelists reject the raw alias, which would re-create the very
session poisoning this module exists to prevent.

Ported from ``packages/agent-core/src/tools/support/image-format-policy.ts``
in the Kimi Code TypeScript monorepo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Image MIME types every provider accepts. The closed set.
MODEL_ACCEPTED_IMAGE_MIMES: frozenset[str] = frozenset({
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
})

# Human-readable list of the accepted formats, for notices.
ACCEPTED_FORMATS_TEXT = "PNG, JPEG, GIF, and WebP"


@dataclass(frozen=True)
class LinuxDecoder:
    """A format-specific Linux decoder named in the conversion guidance."""

    command: str
    package_name: str


# Unsupported formats worth tailoring the guidance for, by normalized MIME.
# A missing entry still means "refuse" — the entry only adds a
# format-specific conversion hint.
_UNSUPPORTED_IMAGE_FORMATS: dict[str, LinuxDecoder | None] = {
    "image/avif": None,
    "image/heic": LinuxDecoder(command="heif-convert", package_name="libheif-examples"),
    "image/heif": LinuxDecoder(command="heif-convert", package_name="libheif-examples"),
    "image/bmp": None,
    "image/tiff": None,
    "image/x-icon": None,
}

_TRAILING_EXTENSION_RE = re.compile(r"\.[^./\\]+$")


def normalize_image_mime(mime_type: str) -> str:
    """Lowercase, drop MIME parameters, and apply the ``image/jpg`` alias.

    Parameter stripping keeps a declared media type like
    ``image/jpeg; charset=utf-8`` consistent with a data-URL MIME token
    (which the parser already clips at the first ``;``), so an accepted
    image with parameters is treated exactly like the bare form instead of
    being misread as unsupported.
    """
    base = mime_type.strip().lower().split(";", 1)[0].strip()
    return "image/jpeg" if base == "image/jpg" else base


def is_model_accepted_image_mime(mime_type: str) -> bool:
    """Whether an image with this MIME may be sent to the model.

    Only the closed accepted set passes; everything else must be refused at
    the entry point — once an unsupported ``image_url`` lands in the session
    history, every later request in the session is rejected by the provider.
    """
    return normalize_image_mime(mime_type) in MODEL_ACCEPTED_IMAGE_MIMES


def build_image_conversion_guidance(path: str, mime_type: str, os_kind: str) -> str:
    """Refusal for an unsupported image, with a per-OS conversion command.

    ``os_kind`` describes where the Shell tool actually runs (e.g.
    ``"macOS"``, ``"Linux"``, ``"Windows"``), so SSH/container sessions get
    the right command too. The model can run the command through Shell
    (under the normal permission flow) and read the converted file.

    macOS converts with the built-in ``sips``; Linux and Windows have no
    built-in decoder for these formats, so the guidance names ImageMagick
    (plus the format's dedicated Linux decoder when one exists, e.g.
    heif-convert).
    """
    converted = _TRAILING_EXTENSION_RE.sub("", path) + ".jpg"
    guidance = _image_conversion_command(
        path,
        converted,
        os_kind,
        _UNSUPPORTED_IMAGE_FORMATS.get(normalize_image_mime(mime_type)),
    )
    return (
        f'"{path}" is an {mime_type} image, which the provider does not accept. '
        "Convert it to JPEG first, then read the converted file. " + guidance
    )


def _image_conversion_command(
    path: str,
    converted: str,
    os_kind: str,
    linux_decoder: LinuxDecoder | None,
) -> str:
    magick = f'magick "{path}" "{converted}"'
    match os_kind:
        case "macOS":
            return f'On macOS: sips -s format jpeg "{path}" --out "{converted}"'
        case "Linux":
            if linux_decoder is None:
                return f"On Linux, with ImageMagick: {magick}"
            return (
                f'On Linux: {linux_decoder.command} "{path}" "{converted}" '
                f"(package {linux_decoder.package_name}), or with ImageMagick: {magick}"
            )
        case "Windows":
            return (
                f"On Windows, with ImageMagick: {magick} "
                "(install it first if missing: winget install ImageMagick.ImageMagick)"
            )
        case _:
            options = f'Options: sips -s format jpeg "{path}" --out "{converted}" (macOS)'
            if linux_decoder is not None:
                options += (
                    f', {linux_decoder.command} "{path}" "{converted}" '
                    f"(Linux, package {linux_decoder.package_name})"
                )
            return options + f", or {magick} (ImageMagick)"
