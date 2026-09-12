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
