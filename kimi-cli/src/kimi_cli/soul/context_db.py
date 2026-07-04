from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import aiosqlite
from kosong.message import Message

from kimi_cli.soul.context_records import ExportedContext
from kimi_cli.utils.logging import logger


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CHECKPOINT_PATTERN = re.compile(r"^<system>CHECKPOINT \d+</system>$")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS messages (
    rowid       INTEGER PRIMARY KEY AUTOINCREMENT,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  REAL NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS system_prompt (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    content     TEXT NOT NULL,
    updated_at  REAL NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS checkpoints (
    id            INTEGER NOT NULL,
    message_rowid INTEGER,
    created_at    REAL NOT NULL DEFAULT (unixepoch()),
    PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS usage_snapshots (
    rowid       INTEGER PRIMARY KEY AUTOINCREMENT,
    token_count INTEGER NOT NULL,
    created_at  REAL NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_messages_role ON messages(role);
CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at);
"""


# ---------------------------------------------------------------------------
# ContextDB
# ---------------------------------------------------------------------------


class ContextDB:
    """SQLite-backed storage for conversation context.

    Lifecycle:
        >>> db = ContextDB(db_path)
        >>> await db.initialize()
        >>> ...  # use methods
        >>> await db.close()
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._in_transaction: bool = False
        self._last_message_rowid: int = 0

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    @property
    def db_path(self) -> Path:
        return self._db_path

    async def initialize(self) -> None:
        """Open connection, enable WAL mode, and create tables if needed."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self._db_path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(_SCHEMA_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _ensure_open(self) -> aiosqlite.Connection:
        if self._conn is None:
            await self.initialize()
        return self._conn  # type: ignore[return-value]

    async def _maybe_commit(self, conn: aiosqlite.Connection) -> None:
        """Commit if not inside an explicit transaction."""
        if not self._in_transaction:
            await conn.commit()

    async def begin_transaction(self) -> None:
        """Begin an explicit transaction for bulk operations."""
        conn = await self._ensure_open()
        await conn.execute("BEGIN")
        self._in_transaction = True

    async def commit_transaction(self) -> None:
        """Commit the current explicit transaction."""
        if self._conn is not None:
            await self._conn.execute("COMMIT")
            self._in_transaction = False

    async def rollback_transaction(self) -> None:
        """Rollback the current explicit transaction."""
        if self._conn is not None:
            try:
                await self._conn.execute("ROLLBACK")
            finally:
                self._in_transaction = False

    # ------------------------------------------------------------------ #
    # System prompt
    # ------------------------------------------------------------------ #

    async def get_system_prompt(self) -> str | None:
        conn = await self._ensure_open()
        cursor = await conn.execute("SELECT content FROM system_prompt WHERE id = 1")
        row = await cursor.fetchone()
        await cursor.close()
        return row["content"] if row else None

    async def set_system_prompt(self, content: str) -> None:
        conn = await self._ensure_open()
        await conn.execute(
            "INSERT OR REPLACE INTO system_prompt (id, content, updated_at) VALUES (1, ?, unixepoch())",
            (content,),
        )
        await self._maybe_commit(conn)

    # ------------------------------------------------------------------ #
    # Messages (append + read)
    # ------------------------------------------------------------------ #

    async def append_messages(self, messages: Sequence[Message]) -> None:
        conn = await self._ensure_open()
        params = [
            (msg.role, msg.model_dump_json(exclude_none=True))
            for msg in messages
        ]
        if params:
            await conn.executemany(
                "INSERT INTO messages (role, content) VALUES (?, ?)", params
            )
        await self._maybe_commit(conn)

    async def get_messages(
        self,
        *,
        after_rowid: int = 0,
        limit: int | None = None,
    ) -> list[Message]:
        rows = await self._get_message_rows(after_rowid=after_rowid, limit=limit)
        return [Message.model_validate_json(row["content"]) for row in rows]

    async def get_messages_with_meta(
        self,
        *,
        after_rowid: int = 0,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Like get_messages() but returns dicts with rowid, role, content, created_at."""
        return await self._get_message_rows(after_rowid=after_rowid, limit=limit)

    async def _get_message_rows(
        self,
        *,
        after_rowid: int = 0,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Shared helper: returns dict rows with rowid, role, content, created_at."""
        conn = await self._ensure_open()
        query = "SELECT rowid, role, content, created_at FROM messages WHERE rowid > ? ORDER BY rowid"
        params: list[Any] = [after_rowid]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        cursor = await conn.execute(query, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]

    async def get_message_count(self) -> int:
        conn = await self._ensure_open()
        cursor = await conn.execute("SELECT COUNT(*) FROM messages")
        row = await cursor.fetchone()
        await cursor.close()
        return row[0]  # type: ignore[index]

    async def has_visible_messages(self) -> bool:
        """Check if there are messages with non-meta roles (user/assistant/tool)."""
        conn = await self._ensure_open()
        cursor = await conn.execute(
            "SELECT 1 FROM messages WHERE role NOT IN ('_system_prompt', '_usage', '_checkpoint') LIMIT 1"
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row is not None

    async def get_last_message_rowid(self) -> int:
        conn = await self._ensure_open()
        cursor = await conn.execute("SELECT MAX(rowid) FROM messages")
        row = await cursor.fetchone()
        await cursor.close()
        return row[0] if row and row[0] else 0  # type: ignore[index]

    # ------------------------------------------------------------------ #
    # Checkpoints
    # ------------------------------------------------------------------ #

    async def create_checkpoint(self, checkpoint_id: int) -> int:
        """Record a checkpoint and return the current max message rowid."""
        max_rowid = await self.get_last_message_rowid()
        conn = await self._ensure_open()
        await conn.execute(
            "INSERT INTO checkpoints (id, message_rowid) VALUES (?, ?)",
            (checkpoint_id, max_rowid),
        )
        await self._maybe_commit(conn)
        return max_rowid

    async def get_latest_checkpoint_id(self) -> int:
        conn = await self._ensure_open()
        cursor = await conn.execute("SELECT COALESCE(MAX(id), -1) FROM checkpoints")
        row = await cursor.fetchone()
        await cursor.close()
        return row[0]  # type: ignore[index]

    async def get_checkpoint_message_rowid(self, checkpoint_id: int) -> int | None:
        conn = await self._ensure_open()
        cursor = await conn.execute(
            "SELECT message_rowid FROM checkpoints WHERE id = ?", (checkpoint_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row["message_rowid"] if row else None

    async def revert_to_checkpoint(self, checkpoint_id: int) -> None:
        """Delete all messages, checkpoints, and usage snapshots after the given checkpoint."""
        message_rowid = await self.get_checkpoint_message_rowid(checkpoint_id)
        if message_rowid is None:
            raise ValueError(f"Checkpoint {checkpoint_id} not found")

        conn = await self._ensure_open()
        already_in_tx = self._in_transaction
        if not already_in_tx:
            await conn.execute("BEGIN")
        try:
            await conn.execute("DELETE FROM messages WHERE rowid > ?", (message_rowid,))
            await conn.execute("DELETE FROM checkpoints WHERE id >= ?", (checkpoint_id,))

            # Find the boundary usage_snapshot rowid — usage rowids correlate with message rowids
            cursor = await conn.execute(
                "SELECT MAX(rowid) FROM usage_snapshots WHERE rowid <= ?", (message_rowid,)
            )
            boundary = await cursor.fetchone()
            await cursor.close()
            boundary_rowid = boundary[0] if boundary[0] else 0
            await conn.execute("DELETE FROM usage_snapshots WHERE rowid > ?", (boundary_rowid,))

            if not already_in_tx:
                await conn.execute("COMMIT")
        except Exception:
            if not already_in_tx:
                await conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------ #
    # Usage snapshots
    # ------------------------------------------------------------------ #

    async def record_usage(self, token_count: int) -> None:
        conn = await self._ensure_open()
        await conn.execute(
            "INSERT INTO usage_snapshots (token_count) VALUES (?)",
            (token_count,),
        )
        await self._maybe_commit(conn)

    async def get_latest_usage(self) -> int | None:
        conn = await self._ensure_open()
        cursor = await conn.execute(
            "SELECT token_count FROM usage_snapshots ORDER BY rowid DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row["token_count"] if row else None

    # ------------------------------------------------------------------ #
    # Bulk operations
    # ------------------------------------------------------------------ #

    async def clear(self) -> None:
        conn = await self._ensure_open()
        already_in_tx = self._in_transaction
        if not already_in_tx:
            await conn.execute("BEGIN")
        try:
            await conn.execute("DELETE FROM messages")
            await conn.execute("DELETE FROM system_prompt")
            await conn.execute("DELETE FROM checkpoints")
            await conn.execute("DELETE FROM usage_snapshots")
            if not already_in_tx:
                await conn.execute("COMMIT")
        except Exception:
            if not already_in_tx:
                await conn.execute("ROLLBACK")
            raise

    async def export(self) -> ExportedContext:
        """Export all context data. Acquires the connection once for all queries."""
        # Ensure connection is open (subsequent _ensure_open calls are no-ops)
        await self._ensure_open()

        result = ExportedContext()

        # system prompt
        sp = await self.get_system_prompt()
        if sp:
            result.system_prompt = sp

        # messages
        result.messages = await self.get_messages()

        # checkpoints
        cursor = await self._conn.execute("SELECT id FROM checkpoints ORDER BY id")  # type: ignore[union-attr]
        rows = await cursor.fetchall()
        await cursor.close()
        result.checkpoints = [row["id"] for row in rows]

        # usage snapshots
        cursor = await self._conn.execute("SELECT token_count FROM usage_snapshots ORDER BY rowid")  # type: ignore[union-attr]
        rows = await cursor.fetchall()
        await cursor.close()
        result.usages = [row["token_count"] for row in rows]

        return result

    # ------------------------------------------------------------------ #
    # Turn boundaries
    # ------------------------------------------------------------------ #

    async def get_messages_up_to_turn(self, turn_index: int) -> list[tuple[str, int]]:
        """Return (json_line, rowid) pairs for all messages up to and including the given turn.

        Turn detection is based on real user messages, excluding synthetic checkpoint
        user entries like ``<system>CHECKPOINT N</system>``.

        Uses a streaming cursor to avoid loading all rows into memory.
        """
        conn = await self._ensure_open()
        cursor = await conn.execute(
            "SELECT rowid, role, content FROM messages ORDER BY rowid"
        )

        result: list[tuple[str, int]] = []
        current_turn = -1

        async for row in cursor:
            role = row["role"]
            content = row["content"]

            # Detect user turn (excluding synthetic checkpoint markers)
            if role == "user":
                # Fast-path: only attempt JSON parsing if content contains checkpoint marker
                if "CHECKPOINT" in content:
                    try:
                        parsed = json.loads(content)
                        if isinstance(parsed, str) and _CHECKPOINT_PATTERN.fullmatch(parsed.strip()):
                            # Checkpoint user message — don't count as real turn
                            pass
                        elif isinstance(parsed, list) and len(parsed) == 1:
                            first = parsed[0]
                            if isinstance(first, dict) and isinstance(first.get("text"), str):
                                text = first["text"]
                                if _CHECKPOINT_PATTERN.fullmatch(text.strip()):
                                    pass  # skip checkpoint
                                else:
                                    current_turn += 1
                            else:
                                current_turn += 1
                        else:
                            current_turn += 1
                    except (json.JSONDecodeError, TypeError):
                        current_turn += 1
                else:
                    current_turn += 1

                if current_turn > turn_index:
                    break

            if current_turn <= turn_index:
                result.append((content, row["rowid"]))

        await cursor.close()
        return result

    # ------------------------------------------------------------------ #
    # Migration helpers
    # ------------------------------------------------------------------ #

    async def import_jsonl_line(self, line_json: dict[str, Any]) -> None:
        """Import a single parsed JSONL line into the appropriate table.

        Used during JSONL → SQLite migration.
        Tracks the last inserted message rowid so that checkpoints can
        reference the correct message boundary.
        """
        conn = await self._ensure_open()
        role = line_json.get("role")

        if role == "_system_prompt":
            content = line_json.get("content", "")
            await conn.execute(
                "INSERT OR REPLACE INTO system_prompt (id, content, updated_at) VALUES (1, ?, unixepoch())",
                (content,),
            )
        elif role == "_usage":
            token_count = line_json.get("token_count", 0)
            await conn.execute(
                "INSERT INTO usage_snapshots (token_count) VALUES (?)",
                (token_count,),
            )
        elif role == "_checkpoint":
            cpid = line_json.get("id", 0)
            await conn.execute(
                "INSERT INTO checkpoints (id, message_rowid) VALUES (?, ?)",
                (cpid, self._last_message_rowid),
            )
        else:
            cursor = await conn.execute(
                "INSERT INTO messages (role, content) VALUES (?, ?)",
                (role, json.dumps(line_json)),
            )
            self._last_message_rowid = cursor.lastrowid or self._last_message_rowid

    async def finalize_migration(self) -> None:
        """After all JSONL lines are imported, update checkpoint message_rowid references.

        Since checkpoints now get correct message_rowid during import_jsonl_line,
        this is a no-op retained for backward compatibility.
        """
        # No longer needed — message_rowid is set correctly during import

    async def fix_checkpoint_message_rowids(self) -> None:
        """Fix checkpoint message_rowid to point to actual message boundaries.

        Since import_jsonl_line now tracks the last message rowid during import,
        this method only fixes checkpoints with message_rowid=0 (from pre-fix
        migrations or edge cases). Uses a single UPDATE for efficiency.
        """
        conn = await self._ensure_open()
        # For any checkpoints still set to 0, assign message_rowid = id
        # as a reasonable fallback (checkpoint ids and message rowids
        # are both monotonically increasing during sequential import).
        await conn.execute(
            "UPDATE checkpoints SET message_rowid = id WHERE message_rowid = 0"
        )
        await self._maybe_commit(conn)
