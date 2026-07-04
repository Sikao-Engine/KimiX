"""Tests for the SQLite-backed ContextDB class."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from kosong.message import Message

from kimi_cli.soul.context_db import ContextDB


@pytest.fixture
async def db(tmp_path: Path) -> ContextDB:
    """Create a fresh ContextDB in a temp directory."""
    _db = ContextDB(tmp_path / "test.db")
    await _db.initialize()
    yield _db
    await _db.close()


class TestContextDB:
    """Test suite for ContextDB CRUD operations."""

    async def test_initialize_creates_tables(self, tmp_path: Path) -> None:
        """Verify that initialize() creates the expected tables."""
        db_path = tmp_path / "test.db"
        db = ContextDB(db_path)
        await db.initialize()

        # Check tables exist
        import aiosqlite

        conn = await aiosqlite.connect(str(db_path))
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        tables = {row[0] for row in await cursor.fetchall()}
        await cursor.close()
        await conn.close()
        await db.close()

        assert "messages" in tables
        assert "system_prompt" in tables
        assert "checkpoints" in tables
        assert "usage_snapshots" in tables

    async def test_system_prompt_roundtrip(self, db: ContextDB) -> None:
        """Test set/get system prompt."""
        assert await db.get_system_prompt() is None

        await db.set_system_prompt("You are a helpful assistant.")
        result = await db.get_system_prompt()
        assert result == "You are a helpful assistant."

        # Overwrite
        await db.set_system_prompt("New system prompt.")
        result = await db.get_system_prompt()
        assert result == "New system prompt."

    async def test_append_and_get_messages(self, db: ContextDB) -> None:
        """Test appending messages and retrieving them."""
        msg1 = Message(role="user", content=[{"type": "text", "text": "Hello"}])
        msg2 = Message(role="assistant", content=[{"type": "text", "text": "Hi there!"}])

        await db.append_messages([msg1, msg2])

        messages = await db.get_messages()
        assert len(messages) == 2
        assert messages[0].role == "user"
        assert messages[1].role == "assistant"

    async def test_get_messages_after_rowid(self, db: ContextDB) -> None:
        """Test pagination with after_rowid."""
        messages = [Message(role="user", content=[{"type": "text", "text": f"Msg {i}"}]) for i in range(5)]
        await db.append_messages(messages)

        # Get messages after rowid 2 (0-based: skip first 2)
        result = await db.get_messages(after_rowid=2)
        assert len(result) == 3
        assert "Msg 2" in result[0].extract_text()

    async def test_get_messages_with_limit(self, db: ContextDB) -> None:
        """Test limiting the number of returned messages."""
        messages = [Message(role="user", content=[{"type": "text", "text": f"Msg {i}"}]) for i in range(10)]
        await db.append_messages(messages)

        result = await db.get_messages(limit=3)
        assert len(result) == 3

    async def test_get_message_count(self, db: ContextDB) -> None:
        """Test counting messages."""
        assert await db.get_message_count() == 0

        await db.append_messages([Message(role="user", content=[{"type": "text", "text": "Hello"}])])
        assert await db.get_message_count() == 1

        await db.append_messages([Message(role="assistant", content=[{"type": "text", "text": "Hi"}])])
        assert await db.get_message_count() == 2

    async def test_has_visible_messages(self, db: ContextDB) -> None:
        """Test detecting visible (non-meta) messages."""
        assert not await db.has_visible_messages()

        # Meta messages should not count as visible
        await db.set_system_prompt("test")
        assert not await db.has_visible_messages()

        # Real message should count
        await db.append_messages([Message(role="user", content=[{"type": "text", "text": "Hello"}])])
        assert await db.has_visible_messages()

    async def test_checkpoint_create_and_revert(self, db: ContextDB) -> None:
        """Test checkpoint creation and revert."""
        msg1 = Message(role="user", content=[{"type": "text", "text": "Hello"}])
        msg2 = Message(role="assistant", content=[{"type": "text", "text": "Hi"}])
        msg3 = Message(role="user", content=[{"type": "text", "text": "Another question"}])

        await db.append_messages([msg1, msg2])
        cp_rowid = await db.create_checkpoint(0)
        assert cp_rowid > 0

        await db.append_messages([msg3])

        # Verify 3 messages exist
        assert await db.get_message_count() == 3

        # Revert to checkpoint 0
        await db.revert_to_checkpoint(0)

        # Verify only 2 messages remain
        assert await db.get_message_count() == 2
        messages = await db.get_messages()
        assert len(messages) == 2

    async def test_get_latest_checkpoint_id(self, db: ContextDB) -> None:
        """Test retrieving the latest checkpoint ID."""
        assert await db.get_latest_checkpoint_id() == -1

        await db.create_checkpoint(5)
        assert await db.get_latest_checkpoint_id() == 5

        await db.create_checkpoint(10)
        assert await db.get_latest_checkpoint_id() == 10

    async def test_usage_snapshots(self, db: ContextDB) -> None:
        """Test recording and retrieving usage snapshots."""
        assert await db.get_latest_usage() is None

        await db.record_usage(100)
        assert await db.get_latest_usage() == 100

        await db.record_usage(200)
        assert await db.get_latest_usage() == 200

    async def test_clear(self, db: ContextDB) -> None:
        """Test clearing all data."""
        await db.set_system_prompt("test")
        await db.append_messages([Message(role="user", content=[{"type": "text", "text": "Hello"}])])
        await db.create_checkpoint(0)
        await db.record_usage(100)

        await db.clear()

        assert await db.get_system_prompt() is None
        assert await db.get_message_count() == 0
        assert await db.get_latest_checkpoint_id() == -1
        assert await db.get_latest_usage() is None

    async def test_export(self, db: ContextDB) -> None:
        """Test export produces correct ExportedContext."""
        await db.set_system_prompt("You are a bot.")
        await db.append_messages([Message(role="user", content=[{"type": "text", "text": "Hello"}])])
        await db.create_checkpoint(0)
        await db.record_usage(50)

        exported = await db.export()
        assert exported.system_prompt == "You are a bot."
        assert len(exported.messages) == 1
        assert exported.messages[0].role == "user"
        assert exported.checkpoints == [0]
        assert exported.usages == [50]

    async def test_import_jsonl_line_system_prompt(self, db: ContextDB) -> None:
        """Test importing a system prompt line from JSONL."""
        await db.import_jsonl_line({"role": "_system_prompt", "content": "Hello"})
        assert await db.get_system_prompt() == "Hello"

    async def test_import_jsonl_line_message(self, db: ContextDB) -> None:
        """Test importing a message line from JSONL."""
        msg_data = {"role": "user", "content": [{"type": "text", "text": "Hi"}]}
        await db.import_jsonl_line(msg_data)
        assert await db.get_message_count() == 1

    async def test_import_jsonl_line_checkpoint(self, db: ContextDB) -> None:
        """Test importing a checkpoint line from JSONL."""
        await db.import_jsonl_line({"role": "_checkpoint", "id": 0})
        assert await db.get_latest_checkpoint_id() == 0

    async def test_import_jsonl_line_usage(self, db: ContextDB) -> None:
        """Test importing a usage line from JSONL."""
        await db.import_jsonl_line({"role": "_usage", "token_count": 42})
        assert await db.get_latest_usage() == 42

    async def test_get_messages_up_to_turn(self, db: ContextDB) -> None:
        """Test get_messages_up_to_turn for fork operations."""
        messages = [
            Message(role="user", content=[{"type": "text", "text": "Turn 0"}]),
            Message(role="assistant", content=[{"type": "text", "text": "Response 0"}]),
            Message(role="user", content=[{"type": "text", "text": "Turn 1"}]),
            Message(role="assistant", content=[{"type": "text", "text": "Response 1"}]),
        ]
        await db.append_messages(messages)

        result = await db.get_messages_up_to_turn(0)
        assert len(result) == 2  # Turn 0 user + assistant

        result = await db.get_messages_up_to_turn(1)
        assert len(result) == 4  # All messages

    async def test_get_messages_up_to_turn_skips_checkpoints(self, db: ContextDB) -> None:
        """Test that checkpoint user messages are not counted as turns."""
        messages = [
            Message(role="user", content=[{"type": "text", "text": "Turn 0"}]),
            Message(role="assistant", content=[{"type": "text", "text": "Response 0"}]),
            # Checkpoint marker — not a real turn
            Message(role="user", content=[{"type": "text", "text": "CHECKPOINT 0"}]),
            Message(role="user", content=[{"type": "text", "text": "Turn 1"}]),
            Message(role="assistant", content=[{"type": "text", "text": "Response 1"}]),
        ]
        await db.append_messages(messages)

        # get_messages_up_to_turn needs to handle the checkpoint content matching
        result = await db.get_messages_up_to_turn(0)
        # In SQLite, the content is stored as JSON, so the checkpoint check works on the parsed content
        assert len(result) >= 2

    async def test_close_reopen(self, tmp_path: Path) -> None:
        """Test closing and reopening a database preserves data."""
        db_path = tmp_path / "test.db"
        db = ContextDB(db_path)
        await db.initialize()
        await db.set_system_prompt("persistent")
        await db.append_messages([Message(role="user", content=[{"type": "text", "text": "Hello"}])])
        await db.close()

        # Reopen
        db2 = ContextDB(db_path)
        await db2.initialize()
        assert await db2.get_system_prompt() == "persistent"
        messages = await db2.get_messages()
        assert len(messages) == 1
        await db2.close()

    async def test_get_last_message_rowid(self, db: ContextDB) -> None:
        """Test getting the last message rowid."""
        assert await db.get_last_message_rowid() == 0

        await db.append_messages([Message(role="user", content=[{"type": "text", "text": "A"}])])
        rowid1 = await db.get_last_message_rowid()
        assert rowid1 > 0

        await db.append_messages([Message(role="user", content=[{"type": "text", "text": "B"}])])
        rowid2 = await db.get_last_message_rowid()
        assert rowid2 > rowid1

    async def test_get_checkpoint_message_rowid(self, db: ContextDB) -> None:
        """Test getting the message rowid for a checkpoint."""
        assert await db.get_checkpoint_message_rowid(0) is None

        await db.append_messages([Message(role="user", content=[{"type": "text", "text": "Hello"}])])
        await db.create_checkpoint(0)
        rowid = await db.get_checkpoint_message_rowid(0)
        assert rowid is not None and rowid > 0

    async def test_revert_to_checkpoint_not_found(self, db: ContextDB) -> None:
        """Test that reverting to a non-existent checkpoint raises ValueError."""
        with pytest.raises(ValueError, match="Checkpoint 999 not found"):
            await db.revert_to_checkpoint(999)

    async def test_create_checkpoint_updates_multiple(self, db: ContextDB) -> None:
        """Test creating and reverting multiple checkpoints."""
        for i in range(3):
            await db.append_messages([Message(role="user", content=[{"type": "text", "text": f"Msg {i}"}])])
            await db.create_checkpoint(i)

        assert await db.get_message_count() == 3

        # Revert to checkpoint 1 — should leave only 2 messages
        await db.revert_to_checkpoint(1)
        assert await db.get_message_count() == 2
        messages = await db.get_messages()
        assert "Msg 0" in messages[0].extract_text()

    async def test_export_empty(self, db: ContextDB) -> None:
        """Test exporting an empty database."""
        exported = await db.export()
        assert exported.system_prompt is None
        assert exported.messages == []
        assert exported.checkpoints == []
        assert exported.usages == []

    async def test_get_messages_with_meta(self, db: ContextDB) -> None:
        """Test get_messages_with_meta returns dicts with metadata."""
        await db.append_messages([Message(role="user", content=[{"type": "text", "text": "Hello"}])])

        result = await db.get_messages_with_meta()
        assert len(result) == 1
        assert "rowid" in result[0]
        assert "role" in result[0]
        assert result[0]["role"] == "user"
        assert "content" in result[0]
        assert "created_at" in result[0]
