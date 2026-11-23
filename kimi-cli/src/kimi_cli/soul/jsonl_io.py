"""Dense UTF-8 JSON Lines framing and append-only file handles.

The helpers in this module deliberately do not implement atomic replacement or
migration.  Runtime users append one self-contained JSON object per line;
administrative code can build and validate a temporary file separately.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any, BinaryIO

import orjson
from kosong.utils.jsonx import loads_relaxed

from kimi_cli.utils.logging import logger


def encode_jsonl_record(record: Mapping[str, Any]) -> bytes:
    """Encode one mapping as strict, dense JSON followed by exactly one LF."""
    return orjson.dumps(dict(record), option=orjson.OPT_APPEND_NEWLINE)


def _structurally_complete_object(text: str) -> bool:
    """Reject truncated objects before the permissive legacy parser repairs them."""
    depth = 0
    quote: str | None = None
    escaped = False
    for char in text:
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth < 0:
                return False
    return quote is None and depth == 0


def _has_no_trailing_tokens(text: str) -> bool:
    """Accept exactly one outer array/object and optional surrounding space."""
    depth = 0
    quote: str | None = None
    escaped = False
    started = False
    for index, char in enumerate(text):
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char in "[{":
            started = True
            depth += 1
        elif char in "]}":
            depth -= 1
            if started and depth == 0:
                return not text[index + 1 :].strip()
    return False


def iter_jsonl_records(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    """Yield ``(physical_line_number, record)`` without modifying *path*.

    Strict JSON is preferred.  The existing relaxed parser remains a read-only
    compatibility boundary for legacy lines and recoverable truncated tails.
    UTF-8 decoding uses replacement semantics, matching the previous context
    reader.  Malformed records are logged with their physical line number and
    skipped; bytes before them remain available to later replay operations.
    """
    with path.open("rb") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            text = raw_line.decode("utf-8", errors="replace")
            if not _structurally_complete_object(text) or not _has_no_trailing_tokens(text):
                logger.warning(
                    "Skipping incomplete JSONL record in {path} at line {line}",
                    path=path,
                    line=line_number,
                )
                continue
            try:
                try:
                    value = orjson.loads(text)
                except (orjson.JSONDecodeError, ValueError, TypeError):
                    value = loads_relaxed(text)
            except Exception as exc:
                logger.warning(
                    "Skipping malformed JSONL record in {path} at line {line}: {error}",
                    path=path,
                    line=line_number,
                    error=exc,
                )
                continue
            if not isinstance(value, dict):
                logger.warning(
                    "Skipping non-object JSONL record in {path} at line {line}",
                    path=path,
                    line=line_number,
                )
                continue
            yield line_number, value


def read_jsonl_records(path: Path) -> list[dict[str, Any]]:
    """Read all valid object records from *path* in physical order."""
    return [record for _, record in iter_jsonl_records(path)]


def read_jsonl_records_for_copy(path: Path) -> list[dict[str, Any]]:
    """Read records for an administrative copy, rejecting interior corruption.

    Only an incomplete final non-empty record is tolerated, matching a
    concurrent append interrupted after the source's last flushed operation.
    """
    with path.open("rb") as handle:
        physical = list(handle)
    nonempty_lines = [index for index, raw in enumerate(physical, start=1) if raw.strip()]
    indexed = list(iter_jsonl_records(path))
    valid_lines = {line for line, _ in indexed}
    invalid = [line for line in nonempty_lines if line not in valid_lines]
    if invalid:
        last = nonempty_lines[-1]
        if invalid != [last] or _structurally_complete_object(
            physical[last - 1].decode("utf-8", errors="replace")
        ):
            raise ValueError(f"Malformed interior JSONL record in {path} at line {invalid[0]}")
    return [record for _, record in indexed]


class JsonlAppender:
    """A retained synchronous append handle protected by a thread lock.

    Public append operations flush before returning.  ``save()`` and ``close()``
    additionally fsync, providing the explicit durability boundary.  The lock is
    deliberately a ``threading.Lock`` rather than an ``asyncio.Lock`` so one
    instance can be used safely across successive event loops.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: BinaryIO | None = None
        self._lock = threading.Lock()
        self._needs_separator = False

    @property
    def path(self) -> Path:
        return self._path

    def _open_locked(self) -> None:
        if self._handle is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._needs_separator = False
        if self._path.exists() and self._path.stat().st_size > 0:
            with self._path.open("rb") as existing:
                existing.seek(-1, os.SEEK_END)
                self._needs_separator = existing.read(1) != b"\n"
        self._handle = self._path.open("ab")

    def open(self) -> None:
        """Create the file if needed and retain its append handle."""
        with self._lock:
            self._open_locked()

    def append(self, record: Mapping[str, Any]) -> None:
        """Append and flush one dense record."""
        self.append_many((record,))

    def append_many(self, records: Iterable[Mapping[str, Any]]) -> None:
        """Append and flush records, framing a batch as consecutive lines."""
        payload = b"".join(encode_jsonl_record(record) for record in records)
        if not payload:
            return
        with self._lock:
            self._open_locked()
            if self._needs_separator:
                payload = b"\n" + payload
                self._needs_separator = False
            assert self._handle is not None
            self._handle.write(payload)
            self._handle.flush()

    def save(self) -> None:
        """Flush and fsync the current file without serializing it again."""
        with self._lock:
            if self._handle is None:
                return
            self._handle.flush()
            os.fsync(self._handle.fileno())

    def close(self) -> None:
        """Flush, fsync and release the handle; safe to call repeatedly."""
        with self._lock:
            handle = self._handle
            self._handle = None
            self._needs_separator = False
            if handle is None:
                return
            try:
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                handle.close()
