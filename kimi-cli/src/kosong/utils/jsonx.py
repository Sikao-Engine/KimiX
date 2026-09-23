from __future__ import annotations

import json
from typing import Any

import json_repair
import orjson


def loads_relaxed(data: str | bytes | bytearray) -> Any:
    """Parse JSON with orjson for speed, fallback to json_repair for leniency.

    LLM-generated JSON may contain unescaped control characters, trailing
    commas, single-quoted strings, comments, and other relaxations that
    orjson (and stdlib json with strict=False) rejects.  This helper tries
    the fast path first and falls back to json_repair when necessary.
    """
    try:
        return orjson.loads(data)
    except orjson.JSONDecodeError:
        pass
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8", errors="ignore")
    try:
        return json_repair.loads(data)
    except ValueError as exc:
        raise json.JSONDecodeError(str(exc), data, 0) from exc
    except Exception as exc:
        raise json.JSONDecodeError(str(exc), data, 0) from exc


def truncate_to_first_json_value(data: str) -> str | None:
    """Return the prefix of *data* that forms the first complete JSON value.

    Some gateways stream duplicated or trailing-garbage argument chunks which
    get concatenated into strings like ``'{}{}'`` or
    ``'{"a": 1}garbage'``.  ``json_repair`` happily parses such inputs, but
    strict backends (e.g. scnet/Qwen) reject them with a 400 when the same
    string is echoed back in ``tool_calls[].function.arguments``.  This
    helper extracts the first syntactically complete JSON value using the
    stdlib decoder, returning the exact source prefix (stripped of leading
    whitespace) or ``None`` when no complete value exists.
    """
    stripped = data.lstrip()
    if not stripped:
        return None
    try:
        _, end = json.JSONDecoder().raw_decode(stripped)
    except json.JSONDecodeError:
        return None
    return stripped[:end]


def sanitize_tool_arguments(arguments: str | None) -> str:
    """Return *arguments* guaranteed to be a string containing valid JSON.

    Used before persisting a tool call into the message history and before
    sending history to strict backends.  Repair strategy, in order:

    1. Already valid JSON (fast path, orjson) — returned unchanged.
    2. Trailing garbage / duplicated chunks — truncate to the first complete
       JSON value (see :func:`truncate_to_first_json_value`).
    3. Nothing parseable — fall back to ``"{}"``.
    """
    if not arguments:
        # None or empty string: both are unusable; some backends also reject
        # a missing ``arguments`` key entirely.
        return "{}"
    try:
        orjson.loads(arguments)
        return arguments
    except orjson.JSONDecodeError:
        pass
    truncated = truncate_to_first_json_value(arguments)
    return truncated if truncated is not None else "{}"
