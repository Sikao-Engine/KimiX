from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import Any

import orjson


def atomic_json_write(data: Any, path: Path) -> None:
    """Write JSON data to a file atomically using tmp-file + os.replace.

    This prevents data corruption if the process crashes mid-write: either the
    old file is kept intact or the new file is fully committed.
    """
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(orjson.dumps(data, option=orjson.OPT_INDENT_2).decode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise