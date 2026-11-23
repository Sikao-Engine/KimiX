"""One-time read-only migration from legacy session SQLite files to JSONL.

This module is intentionally outside the runtime context/history backends.  It
never creates a database, never reads FTS tables, and must run before runtime
storage is constructed.  New runtime code must not import it on append/search
paths.
"""

from __future__ import annotations

import bisect
from pathlib import Path
from typing import Any

import apsw
import orjson
from kosong.message import Message

from kimi_cli.soul.context import message_record, replay_context
from kimi_cli.soul.jsonl_io import JsonlAppender, read_jsonl_records
from kimi_cli.utils.logging import logger

_CONTEXT_TABLES = {"messages", "system_prompt", "checkpoints", "usage_snapshots"}


def _open_readonly(path: Path) -> apsw.Connection:
    """Open an existing DB read-only, without journal/schema changes."""
    if not path.is_file():
        raise FileNotFoundError(path)
    # No immutable flag: a non-empty WAL may contain the latest committed turns
    # and must participate in the read.  READONLY never creates a missing DB.
    return apsw.Connection(str(path), flags=apsw.SQLITE_OPEN_READONLY)


def _rows(conn: apsw.Connection, sql: str) -> list[tuple[Any, ...]]:
    cursor = conn.execute(sql)
    try:
        return list(cursor)
    finally:
        cursor.close()


def _columns(conn: apsw.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in _rows(conn, f"PRAGMA table_info({table})")}


def _tables(conn: apsw.Connection) -> set[str]:
    return {
        str(row[0])
        for row in _rows(conn, "SELECT name FROM sqlite_master WHERE type='table'")
        if str(row[0]) in _CONTEXT_TABLES or str(row[0]) == "turns"
    }


def _backup_path(path: Path) -> Path:
    base = path.with_name(path.name + ".bak")
    candidate = base
    index = 1
    while candidate.exists():
        candidate = path.with_name(f"{base.name}.{index}")
        index += 1
    return candidate


def _rename_legacy_with_sidecars(path: Path) -> Path:
    backup = _backup_path(path)
    path.rename(backup)
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists():
            sidecar.rename(backup.with_name(backup.name + suffix))
    return backup


def _write_verified(path: Path, records: list[dict[str, Any]], verify: Any) -> bool:
    """Write/fsync/replay a temporary file, then atomically activate it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".migrating")
    temporary.unlink(missing_ok=True)
    writer = JsonlAppender(temporary)
    try:
        writer.open()
        writer.append_many(records)
        writer.save()
        writer.close()
        verify(temporary)
        if path.exists():
            # Canonical JSONL appeared concurrently; never overwrite it.
            temporary.unlink(missing_ok=True)
            return False
        temporary.replace(path)
        return True
    finally:
        writer.close()
        if temporary.exists():
            temporary.unlink()


def _decode_message(rowid: int, role: Any, content: Any, created_at: Any) -> dict[str, Any]:
    if not isinstance(role, str) or not isinstance(content, str):
        raise ValueError(f"legacy message row {rowid} has invalid role/content")
    data = orjson.loads(content)
    if isinstance(data, dict):
        record = dict(data)
        record.setdefault("role", role)
    else:
        record = {"role": role, "content": data}
    record.pop("created_at", None)
    message = Message.model_validate(record)
    timestamp = float(created_at) if isinstance(created_at, (int, float)) else 0.0
    return message_record(message, created_at=timestamp)


def _context_records(db_path: Path) -> list[dict[str, Any]]:
    if db_path.stat().st_size == 0:
        return []
    conn = _open_readonly(db_path)
    try:
        tables = _tables(conn)
        if "messages" not in tables:
            raise ValueError(f"legacy context {db_path} has no messages table")
        columns = _columns(conn, "messages")
        created_column = "created_at" if "created_at" in columns else "rowid"
        raw_messages = _rows(
            conn,
            f"SELECT rowid, role, content, {created_column} FROM messages ORDER BY rowid",
        )
        messages = [_decode_message(*row) for row in raw_messages]
        message_rowids = [int(row[0]) for row in raw_messages]

        records: list[dict[str, Any]] = []
        if "system_prompt" in tables:
            prompts = _rows(conn, "SELECT content FROM system_prompt WHERE id=1")
            if prompts:
                content = prompts[-1][0]
                if not isinstance(content, str):
                    raise ValueError("legacy system prompt is not text")
                records.append({"role": "_system_prompt", "content": content})

        events: list[tuple[int, float, int, int, dict[str, Any]]] = []
        if "checkpoints" in tables:
            checkpoint_columns = _columns(conn, "checkpoints")
            mapping = "message_rowid" if "message_rowid" in checkpoint_columns else "NULL"
            created = "created_at" if "created_at" in checkpoint_columns else "id"
            for checkpoint_id, message_rowid, created_at in _rows(
                conn, f"SELECT id, {mapping}, {created} FROM checkpoints ORDER BY id"
            ):
                if not isinstance(checkpoint_id, int):
                    raise ValueError("legacy checkpoint ID is not an integer")
                position = (
                    bisect.bisect_right(message_rowids, message_rowid)
                    if isinstance(message_rowid, int)
                    else 0
                )
                events.append(
                    (
                        position,
                        float(created_at),
                        0,
                        checkpoint_id,
                        {
                            "role": "_checkpoint",
                            "id": checkpoint_id,
                            "message_count": position,
                        },
                    )
                )
        if "usage_snapshots" in tables:
            usage_columns = _columns(conn, "usage_snapshots")
            mapping = "message_rowid" if "message_rowid" in usage_columns else "NULL"
            created = "created_at" if "created_at" in usage_columns else "rowid"
            for rowid, token_count, message_rowid, created_at in _rows(
                conn,
                f"SELECT rowid, token_count, {mapping}, {created} FROM usage_snapshots ORDER BY rowid",
            ):
                if not isinstance(token_count, int):
                    raise ValueError("legacy usage token_count is not an integer")
                # Pre-mapping SQLite snapshots were documented as covering all
                # messages, not none; retain that pending-token behavior.
                position = (
                    bisect.bisect_right(message_rowids, message_rowid)
                    if isinstance(message_rowid, int)
                    else len(messages)
                )
                events.append(
                    (
                        position,
                        float(created_at),
                        1,
                        int(rowid),
                        {
                            "role": "_usage",
                            "token_count": token_count,
                            "message_count": position,
                        },
                    )
                )
        events.sort(key=lambda event: event[:4])
        event_index = 0
        for position, message in enumerate(messages):
            while event_index < len(events) and events[event_index][0] == position:
                records.append(events[event_index][4])
                event_index += 1
            records.append(message)
        while event_index < len(events):
            records.append(events[event_index][4])
            event_index += 1
        return records
    finally:
        conn.close()


def migrate_legacy_context(db_path: Path, jsonl_path: Path) -> bool:
    """Migrate one context DB; return True when JSONL was activated."""
    if jsonl_path.exists() or not db_path.exists():
        return False
    records = _context_records(db_path)
    expected_messages = [record for record in records if not record["role"].startswith("_")]
    expected_prompt = next(
        (record["content"] for record in records if record["role"] == "_system_prompt"), None
    )
    expected_usages = [record["token_count"] for record in records if record["role"] == "_usage"]
    expected_checkpoints = [record["id"] for record in records if record["role"] == "_checkpoint"]

    def verify(temporary: Path) -> None:
        state = replay_context(temporary)
        if len(state.entries) != len(expected_messages):
            raise ValueError("migrated context message count mismatch")
        if state.system_prompt != expected_prompt:
            raise ValueError("migrated context prompt mismatch")
        if [usage.token_count for usage in state.usages] != expected_usages:
            raise ValueError("migrated context usage mismatch")
        if list(state.checkpoints) != expected_checkpoints:
            raise ValueError("migrated context checkpoints mismatch")
        actual = [entry.message.model_dump(mode="json", exclude_none=True) for entry in state.entries]
        expected = [
            Message.model_validate({key: value for key, value in record.items() if key != "created_at"}).model_dump(mode="json", exclude_none=True)
            for record in expected_messages
        ]
        if actual != expected:
            raise ValueError("migrated context message content mismatch")

    activated = _write_verified(jsonl_path, records, verify)
    if activated:
        backup = _rename_legacy_with_sidecars(db_path)
        logger.info(
            "Migrated legacy context {old} to {new}; backup at {backup}",
            old=db_path,
            new=jsonl_path,
            backup=backup,
        )
        return True
    return False


def _history_records(db_path: Path) -> list[dict[str, Any]]:
    if db_path.stat().st_size == 0:
        return []
    conn = _open_readonly(db_path)
    try:
        if "turns" not in _tables(conn):
            raise ValueError(f"legacy history {db_path} has no turns table")
        records: list[dict[str, Any]] = []
        for turn_id, role, text, timestamp, compacted in _rows(
            conn,
            "SELECT turn_id, role, text, timestamp, is_compacted FROM turns ORDER BY turn_id",
        ):
            if (
                not isinstance(turn_id, int)
                or role not in {"user", "assistant", "tool"}
                or not isinstance(text, str)
                or not isinstance(timestamp, (int, float))
            ):
                raise ValueError(f"legacy history turn {turn_id!r} has invalid fields")
            records.append(
                {
                    "turn_id": turn_id,
                    "role": role,
                    "text": text,
                    "timestamp": float(timestamp),
                    "is_compacted": bool(compacted),
                }
            )
        return records
    finally:
        conn.close()


def migrate_legacy_history(db_path: Path, jsonl_path: Path) -> bool:
    """Migrate the derived history archive, preserving IDs and compaction flags."""
    if jsonl_path.exists() or not db_path.exists():
        return False
    records = _history_records(db_path)

    def verify(temporary: Path) -> None:
        actual = read_jsonl_records(temporary)
        if actual != records:
            raise ValueError("migrated history verification failed")

    activated = _write_verified(jsonl_path, records, verify)
    if activated:
        backup = _rename_legacy_with_sidecars(db_path)
        logger.info(
            "Migrated legacy history {old} to {new}; backup at {backup}",
            old=db_path,
            new=jsonl_path,
            backup=backup,
        )
        return True
    return False


def migrate_session_directory(session_dir: Path) -> None:
    """Run pre-runtime migration for one root or subagent instance directory."""
    # Context is authoritative: any failure must remain visible to the caller.
    migrate_legacy_context(session_dir / "context.db", session_dir / "context.jsonl")
    try:
        migrate_legacy_history(session_dir / "history.db", session_dir / "history.jsonl")
    except Exception as exc:
        logger.warning(
            "Failed to migrate derived history for {session_dir}: {error}",
            session_dir=session_dir,
            error=exc,
        )


def migrate_subagent_directories(session_dir: Path) -> None:
    """Migrate every existing subagent before any child runtime is constructed."""
    root = session_dir / "subagents"
    if not root.is_dir():
        return
    for instance_dir in root.iterdir():
        if instance_dir.is_dir():
            migrate_session_directory(instance_dir)
