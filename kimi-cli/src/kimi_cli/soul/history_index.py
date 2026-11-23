"""Append-only JSONL conversation history with in-memory BM25 search.

Each user/assistant/tool message is one dense turn record.  Compaction and
explicit clears append control records; replay reconstructs the active archive
without editing old bytes.  Search operates only on the incrementally maintained
in-memory postings, never on the JSONL file.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import orjson
from kosong.message import Message

from kimi_cli.soul.jsonl_io import JsonlAppender, iter_jsonl_records
from kimi_cli.soul.jsonl_search import InvertedSearchIndex, message_text
from kimi_cli.utils.logging import logger

_INDEXED_ROLES = frozenset({"user", "assistant", "tool"})


def _valid_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _replay_history(path: Path) -> tuple[InvertedSearchIndex, int]:
    engine = InvertedSearchIndex()
    next_turn_id = 0
    for line_number, record in iter_jsonl_records(path):
        role = record.get("role")
        if role == "_reset":
            engine = InvertedSearchIndex()
            next_turn_id = 0
            continue
        if role == "_compacted":
            through = record.get("through_turn_id")
            if _valid_int(through):
                engine.mark_compacted(through)
            else:
                logger.warning(
                    "Skipping invalid history compaction marker at line {line}", line=line_number
                )
            continue
        turn_id = record.get("turn_id")
        timestamp = record.get("timestamp")
        text = record.get("text")
        compacted = record.get("is_compacted", False)
        if (
            role not in _INDEXED_ROLES
            or not _valid_int(turn_id)
            or turn_id < 0
            or not isinstance(text, str)
            or not isinstance(timestamp, (int, float))
            or isinstance(timestamp, bool)
            or not isinstance(compacted, (bool, int))
        ):
            logger.warning("Skipping invalid history turn at line {line}", line=line_number)
            continue
        turn = {
            "turn_id": turn_id,
            "role": role,
            "text": text,
            "timestamp": float(timestamp),
            "is_compacted": bool(compacted),
        }
        try:
            engine.add_turn(turn)
        except ValueError as exc:
            logger.warning(
                "Skipping duplicate history turn at line {line}: {error}",
                line=line_number,
                error=exc,
            )
            continue
        next_turn_id = max(next_turn_id, turn_id + 1)
    return engine, next_turn_id


class HistoryIndex:
    """Durable append-only history, or the same neutral engine in memory."""

    def __init__(
        self,
        persist_path: Path | None = None,
        *,
        jsonl_path: Path | None = None,
        legacy_json_path: Path | None = None,
    ) -> None:
        if persist_path is not None and jsonl_path is not None:
            raise ValueError("Specify only one of persist_path and jsonl_path")
        selected = jsonl_path if jsonl_path is not None else persist_path
        self._jsonl_path = Path(selected) if selected is not None else None
        self._legacy_json_path = Path(legacy_json_path) if legacy_json_path is not None else None
        self._appender = JsonlAppender(self._jsonl_path) if self._jsonl_path is not None else None
        self._engine = InvertedSearchIndex()
        self._doc_id_counter = 0
        self._loaded = False
        self._lock = threading.Lock()

    @property
    def jsonl_path(self) -> Path | None:
        return self._jsonl_path

    def _load_locked(self) -> bool:
        if self._loaded:
            return self._jsonl_path.exists() if self._jsonl_path is not None else False
        if self._jsonl_path is None:
            self._loaded = True
            return False

        path = self._jsonl_path
        existed = path.exists()
        if not existed and self._legacy_json_path is not None and self._legacy_json_path.exists():
            try:
                self._migrate_legacy_json()
                existed = True
            except Exception as exc:
                logger.warning(
                    "Failed to migrate legacy history JSON {path}: {error}",
                    path=self._legacy_json_path,
                    error=exc,
                )

        if path.exists():
            self._engine, self._doc_id_counter = _replay_history(path)
        else:
            self._engine = InvertedSearchIndex()
            self._doc_id_counter = 0
        assert self._appender is not None
        self._appender.open()
        self._loaded = True
        return existed

    def _ensure_loaded_locked(self) -> None:
        if not self._loaded:
            self._load_locked()

    def _migrate_legacy_json(self) -> None:
        """Validate and atomically activate JSONL before archiving legacy JSON."""
        assert self._jsonl_path is not None
        assert self._legacy_json_path is not None
        path = self._jsonl_path
        legacy = self._legacy_json_path
        data = orjson.loads(legacy.read_bytes())
        if not isinstance(data, dict) or not isinstance(data.get("turns", []), list):
            raise ValueError("legacy history must be an object with a turns list")
        turns: list[dict[str, Any]] = []
        for index, raw in enumerate(data.get("turns", [])):
            if not isinstance(raw, dict):
                raise ValueError(f"legacy turn {index} is not an object")
            turn_id = raw.get("turn_id", index)
            role = raw.get("role", "user")
            text = raw.get("text", "")
            timestamp = raw.get("timestamp", 0.0) or 0.0
            compacted = raw.get("is_compacted", False)
            if (
                not _valid_int(turn_id)
                or turn_id < 0
                or role not in _INDEXED_ROLES
                or not isinstance(text, str)
                or not isinstance(timestamp, (int, float))
                or isinstance(timestamp, bool)
            ):
                raise ValueError(f"legacy turn {index} has invalid fields")
            turns.append(
                {
                    "turn_id": turn_id,
                    "role": role,
                    "text": text,
                    "timestamp": float(timestamp),
                    "is_compacted": bool(compacted),
                }
            )
        if len({turn["turn_id"] for turn in turns}) != len(turns):
            raise ValueError("legacy history contains duplicate turn IDs")

        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.unlink(missing_ok=True)
        writer = JsonlAppender(temporary)
        try:
            writer.open()
            writer.append_many(turns)
            writer.save()
            writer.close()
            engine, _ = _replay_history(temporary)
            if engine.turns != turns:
                raise ValueError("migrated history verification failed")
            if path.exists():
                # Canonical JSONL won a race; never overwrite it implicitly.
                temporary.unlink(missing_ok=True)
                return
            temporary.replace(path)
        finally:
            writer.close()
            if temporary.exists():
                temporary.unlink()
        backup = legacy.with_suffix(".json.bak")
        legacy.replace(backup)
        logger.info("Migrated legacy history {old} to {new}", old=legacy, new=path)

    def index_messages(self, messages: Sequence[Message]) -> None:
        """Append new turns and update only their postings."""
        with self._lock:
            self._ensure_loaded_locked()
            now = time.time()
            turns: list[dict[str, Any]] = []
            next_id = self._doc_id_counter
            for message in messages:
                if message.role not in _INDEXED_ROLES:
                    continue
                text = message_text(message)
                if not text.strip():
                    continue
                turns.append(
                    {
                        "turn_id": next_id,
                        "role": message.role,
                        "text": text,
                        "timestamp": now,
                        "is_compacted": False,
                    }
                )
                next_id += 1
            if not turns:
                return
            if self._appender is not None:
                self._appender.append_many(turns)
            for turn in turns:
                self._engine.add_turn(turn)
            self._doc_id_counter = next_id

    def mark_compacted(self) -> None:
        """Append a boundary marking all currently active turns compacted."""
        with self._lock:
            self._ensure_loaded_locked()
            if not self._engine.turns:
                return
            through = max(turn["turn_id"] for turn in self._engine.turns)
            if self._appender is not None:
                self._appender.append({"role": "_compacted", "through_turn_id": through})
            self._engine.mark_compacted(through)

    def search(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_loaded_locked()
            return self._engine.search(query, top_k)

    def save(self) -> None:
        """Flush/fsync only; turns have already been appended incrementally."""
        if self._appender is not None:
            self._appender.save()

    def load(self) -> bool:
        """Rebuild in-memory state once; return whether JSONL already existed."""
        with self._lock:
            return self._load_locked()

    def clear(self) -> None:
        """Append a reset generation; obsolete bytes remain in the archive."""
        with self._lock:
            self._ensure_loaded_locked()
            if self._appender is not None:
                self._appender.append({"role": "_reset"})
            self._engine = InvertedSearchIndex()
            self._doc_id_counter = 0

    def close(self) -> None:
        if self._appender is not None:
            self._appender.close()

    def __del__(self) -> None:  # pragma: no cover - defensive cleanup
        try:
            self.close()
        except Exception:
            pass

    @property
    def _turns(self) -> list[dict[str, Any]]:
        return self._engine.turns

    @property
    def _turns_list(self) -> list[dict[str, Any]]:
        """Compatibility alias for older tests and diagnostic callers."""
        return self._engine.turns
