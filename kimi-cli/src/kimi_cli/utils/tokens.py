from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

import orjson

from kimi_cli.native_loader import (
    get_compat as _native_get_compat,
    get_module as _native_get_module,
    use_native as _native_use_native,
)

# Resolved once at import time (stable runtime: result never changes).
_NATIVE_TEXT = _native_get_module("text")
# Pure-Python reference implementation (canonical copy lives in the shim);
# resolved lazily to avoid an import-time dependency on the shim package.
_COMPAT_TEXT = None


def _compat_text():
    global _COMPAT_TEXT
    if _COMPAT_TEXT is None:
        _COMPAT_TEXT = _native_get_compat("text")
    return _COMPAT_TEXT

if TYPE_CHECKING:
    from kosong.message import Message
    from kosong.tooling import Tool


def _is_cjk_text(text: str, threshold: float = 0.15) -> bool:
    """Return True if the fraction of CJK characters exceeds *threshold*."""
    # Native acceleration: kimix_native.text.is_cjk_text (bit-identical).
    if _native_use_native("TEXT") and _NATIVE_TEXT is not None:
        return _NATIVE_TEXT.is_cjk_text(text, threshold)
    return _compat_text()._compat_is_cjk_text(text, threshold)


def _estimate_chars_tokens(text: str) -> int:
    """Language-aware character heuristic.

    - English / mostly-ASCII  → ~4 chars per token
    - CJK-detected text       → ~3 chars per token (closer to reality for
      ideographic languages where each character is often its own token)
    - Mixed / code            → ~3.5 chars per token (split the difference)
    """
    if not text:
        return 0
    # Native acceleration: kimix_native.text.estimate_chars_tokens.
    if _native_use_native("TEXT") and _NATIVE_TEXT is not None:
        return _NATIVE_TEXT.estimate_chars_tokens(text)
    return _compat_text()._compat_estimate(text)


def count_tokens(text: str, model: str | None = None) -> int:
    """Count tokens in *text* using the best available method.

    If ``tiktoken`` is installed and *model* is provided, the model-specific
    encoding is used.  Otherwise falls back to a language-aware character
    heuristic that is more accurate than a flat ``len(text) // 4``.
    """
    # Attempt tiktoken only when the package is present and a model is hinted.
    if model:
        try:
            import tiktoken

            enc = tiktoken.encoding_for_model(model)
            return len(enc.encode(text))
        except Exception:
            pass
    return _estimate_chars_tokens(text)


def count_message_tokens(messages: Sequence[Message], model: str | None = None) -> int:
    """Estimate tokens for a sequence of messages.

    Sums tokens from all :class:`TextPart` content in each message.
    """
    from kimi_cli.wire.types import TextPart

    total = 0
    for msg in messages:
        for part in msg.content:
            if isinstance(part, TextPart):
                total += count_tokens(part.text, model=model)
    return total


def count_tool_tokens(tools: Sequence[Tool], model: str | None = None) -> int:
    """Estimate the token cost of the tool schemas sent with every request.

    Tool definitions are part of the request's input but live outside the
    message history, so :func:`count_message_tokens` never sees them. They are
    serialized the same way a provider sends them (JSON) and counted as one
    blob; an empty/missing toolset costs nothing.
    """
    if not tools:
        return 0
    try:
        payload = orjson.dumps([tool.model_dump(exclude_none=True) for tool in tools]).decode()
    except Exception:  # noqa: BLE001 - counting must never break the caller
        payload = " ".join(str(getattr(tool, "name", "")) for tool in tools)
    return count_tokens(payload, model=model)


def estimate_request_overhead(
    system_prompt: str | None,
    tools: Sequence[Tool],
    model: str | None = None,
) -> int:
    """Estimate the non-history part of a request (system prompt + tool schemas).

    Providers bill the system prompt and the tool schemas on every request, so
    any token count that must match the provider-reported usage has to include
    them on top of :func:`count_message_tokens`.
    """
    overhead = count_tokens(system_prompt, model=model) if system_prompt else 0
    return overhead + count_tool_tokens(tools, model=model)
