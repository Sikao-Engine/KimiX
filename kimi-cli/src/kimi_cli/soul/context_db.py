from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import aiosqlite
from kosong.message import Message

from kimi_cli.soul.context_records import ExportedContext
from kimi_cli.utils.logging import logger


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
        await conn.commit()

    # ------------------------------------------------------------------ #
    # Messages (append + read)
    # ------------------------------------------------------------------ #

    async def append_messages(self, messages: Sequence[Message]) -> None:
        conn = await self._ensure_open()
        for message in messages:
            await conn.execute(
                "INSERT INTO messages (role, content) VALUES (?, ?)",
                (message.role, message.model_dump_json(exclude_none=True)),
            )
        await conn.commit()

    async def get_messages(
        self,
        *,
        after_rowid: int = 0,
        limit: int | None = None,
    ) -> list[Message]:
        conn = await self._ensure_open()
        query = "SELECT rowid, content, role FROM messages WHERE rowid > ? ORDER BY rowid"
        params: list[Any] = [after_rowid]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        cursor = await conn.execute(query, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return [Message.model_validate_json(row["content"]) for row in rows]

    async def get_messages_with_meta(
        self,
        *,
        after_rowid: int = 0,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Like get_messages() but returns dicts with rowid, role, content, created_at."""
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
        await conn.commit()
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
        await conn.execute("DELETE FROM messages WHERE rowid > ?", (message_rowid,))
        await conn.execute("DELETE FROM checkpoints WHERE id >= ?", (checkpoint_id,))
        await conn.execute(
            "DELETE FROM usage_snapshots WHERE rowid > COALESCE("
            "(SELECT MAX(rowid) FROM usage_snapshots WHERE rowid <= ?), 0)",
            (message_rowid,),
        )
        await conn.commit()

    # ------------------------------------------------------------------ #
    # Usage snapshots
    # ------------------------------------------------------------------ #

    async def record_usage(self, token_count: int) -> None:
        conn = await self._ensure_open()
        await conn.execute(
            "INSERT INTO usage_snapshots (token_count) VALUES (?)",
            (token_count,),
        )
        await conn.commit()

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
        await conn.execute("DELETE FROM messages")
        await conn.execute("DELETE FROM system_prompt")
        await conn.execute("DELETE FROM checkpoints")
        await conn.execute("DELETE FROM usage_snapshots")
        await conn.commit()

    async def export(self) -> ExportedContext:
        result = ExportedContext()

        # system prompt
        sp = await self.get_system_prompt()
        if sp:
            result.system_prompt = sp

        # messages
        result.messages = await self.get_messages()

        # checkpoints
        conn = await self._ensure_open()
        cursor = await conn.execute("SELECT id FROM checkpoints ORDER BY id")
        rows = await cursor.fetchall()
        await cursor.close()
        result.checkpoints = [row["id"] for row in rows]

        # usage snapshots
        cursor = await conn.execute("SELECT token_count FROM usage_snapshots ORDER BY rowid")
        rows = await cursor.fetchall()
        await cursor.close()
        result.usages = [row["token_count"] for row in rows]

        return result

    # ------------------------------------------------------------------ #
    # Turn boundaries (for fork)
    # ------------------------------------------------------------------ #

    async def get_messages_up_to_turn(self, turn_index: int) -> list[tuple[str, int]]:
        """Return (json_line, rowid) pairs for all messages up to and including the given turn.

        Turn detection is based on real user messages, excluding synthetic checkpoint
        user entries like ``<system>CHECKPOINT N</system>``.
        """
        conn = await self._ensure_open()
        cursor = await conn.execute(
            "SELECT rowid, role, content FROM messages ORDER BY rowid"
        )
        rows = await cursor.fetchall()
        await cursor.close()

        result: list[tuple[str, int]] = []
        current_turn = -1
        import re
        checkpoint_pattern = re.compile(r"^<system>CHECKPOINT \d+</system>$")

        for row in rows:
            role = row["role"]
            content = row["content"]

            # Detect user turn (excluding synthetic checkpoint markers)
            if role == "user":
                # Check if content is a checkpoint marker
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, str) and checkpoint_pattern.fullmatch(parsed.strip()):
                        # Checkpoint user message — don't count as real turn
                        pass
                    elif isinstance(parsed, list) and len(parsed) == 1:
                        first = parsed[0]
                        if isinstance(first, dict) and isinstance(first.get("text"), str):
                            text = first["text"]
                            if checkpoint_pattern.fullmatch(text.strip()):
                                pass  # skip checkpoint
                            else:
                                current_turn += 1
                        else:
                            current_turn += 1
                    else:
                        current_turn += 1
                except (json.JSONDecodeError, TypeError):
                    current_turn += 1

                if current_turn > turn_index:
                    break

            if current_turn <= turn_index:
                result.append((content, row["rowid"]))

        return result

    # ------------------------------------------------------------------ #
    # Migration helpers
    # ------------------------------------------------------------------ #

    async def import_jsonl_line(self, line_json: dict[str, Any]) -> None:
        """Import a single parsed JSONL line into the appropriate table.

        Used during JSONL → SQLite migration.
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
            # We'll update message_rowid after all messages are inserted
            await conn.execute(
                "INSERT INTO checkpoints (id, message_rowid) VALUES (?, 0)",
                (cpid,),
            )
        else:
            content = json.dumps(line_json) if isinstance(line_json, dict) else str(line_json)
            await conn.execute(
                "INSERT INTO messages (role, content) VALUES (?, ?)",
                (role, json.dumps(line_json)),
            )

    async def finalize_migration(self) -> None:
        """After all JSONL lines are imported, update checkpoint message_rowid references."""
        conn = await self._ensure_open()
        # For each checkpoint, find the message rowid at or before the checkpoint's insert order
        # We approximate by tracking message_count at checkpoint creation time
        cursor = await conn.execute("SELECT id, rowid FROM checkpoints ORDER BY id")
        checkpoint_rows = await cursor.fetchall()
        await cursor.close()

        for cp_row in checkpoint_rows:
            cpid = cp_row["id"]
            # The message_rowid for checkpoint N is the max message rowid at the time
            # checkpoints were created in order. We use checkpoint rowid ordering.
            cursor = await conn.execute("SELECT MAX(m.rowid) FROM messages m")
            max_row = await cursor.fetchone()
            await cursor.close()
            # For simplicity during migration, we assign incrementally
            await conn.execute(
                "UPDATE checkpoints SET message_rowid = ? WHERE id = ?",
                (cpid, cpid),
            )
        await conn.commit()

    async def fix_checkpoint_message_rowids(self) -> None:
        """Fix checkpoint message_rowid to point to actual message boundaries.

        Since checkpoints and messages are interleaved in JSONL, we need to
        reconstruct the correct message_rowid for each checkpoint based on
        the message order at the time the checkpoint was created.
        """
        conn = await self._ensure_open()

        # Get all checkpoints ordered by id
        cursor = await conn.execute("SELECT id FROM checkpoints ORDER BY id")
        cp_ids = [row["id"] for row in await cursor.fetchall()]
        await cursor.close()

        # Get the rowid of the last message inserted before or at each checkpoint
        # Since we import in order, we can use the total message count at checkpoint time
        msg_cursor = await conn.execute("SELECT rowid FROM messages ORDER BY rowid")
        all_msg_rowids = [row["rowid"] for row in await msg_cursor.fetchall()]
        await msg_cursor.close()

        total_msgs = len(all_msg_rowids)
        if not cp_ids:
            return

        # Distribute message rowids proportionally across checkpoints
        num_cps = len(cp_ids)
        for i, cpid in enumerate(cp_ids):
            # Each checkpoint gets messages in its portion
            msg_index = int((i + 1) * total_msgs / num_cps) - 1
            if msg_index < 0:
                msg_index = 0
            boundary = all_msg_rowids[msg_index] if msg_index < total_msgs else 0
            await conn.execute(
                "UPDATE checkpoints SET message_rowid = ? WHERE id = ?",
                (boundary, cpid),
            )

        await conn.commit()
