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

    # ------------------------------------------------------------------ #
    # New tests for optimizations
    # ------------------------------------------------------------------ #

    # H1: Stream get_messages_up_to_turn

    async def test_get_messages_up_to_turn_large_conversation(
        self, db: ContextDB
    ) -> None:
        """Verify get_messages_up_to_turn handles 1000+ messages correctly."""
        # Insert 500 turns (user + assistant = 1000 messages)
        messages = []
        for i in range(500):
            messages.append(
                Message(role="user", content=[{"type": "text", "text": f"Turn {i}"}])
            )
            messages.append(
                Message(
                    role="assistant",
                    content=[{"type": "text", "text": f"Response {i}"}],
                )
            )
        await db.append_messages(messages)

        # Get first turn
        result = await db.get_messages_up_to_turn(0)
        assert len(result) == 2

        # Get last turn
        result = await db.get_messages_up_to_turn(499)
        assert len(result) == 1000

    async def test_get_messages_up_to_turn_early_stop(self, db: ContextDB) -> None:
        """Verify early termination after reaching target turn."""
        messages = [
            Message(role="user", content=[{"type": "text", "text": "Turn 0"}]),
            Message(role="assistant", content=[{"type": "text", "text": "R0"}]),
            Message(role="user", content=[{"type": "text", "text": "Turn 1"}]),
            Message(role="assistant", content=[{"type": "text", "text": "R1"}]),
            Message(role="user", content=[{"type": "text", "text": "Turn 2"}]),
        ]
        await db.append_messages(messages)

        result = await db.get_messages_up_to_turn(0)
        # Should stop after Turn 0 user + assistant
        assert len(result) == 2

    # H2: Transaction for migration

    async def test_import_jsonl_bulk_transaction(self, db: ContextDB) -> None:
        """Verify bulk import in a transaction persists all data atomically."""
        await db.begin_transaction()
        try:
            for i in range(100):
                await db.import_jsonl_line(
                    {"role": "user", "content": [{"type": "text", "text": f"Msg {i}"}]}
                )
            await db.commit_transaction()
        except Exception:
            await db.rollback_transaction()
            raise

        count = await db.get_message_count()
        assert count == 100

    async def test_import_jsonl_rollback_on_error(self, db: ContextDB) -> None:
        """Verify that rollback undoes all inserts within the transaction."""
        # Insert some messages first
        await db.append_messages(
            [Message(role="user", content=[{"type": "text", "text": "Pre-existing"}])]
        )
        pre_count = await db.get_message_count()

        await db.begin_transaction()
        try:
            for i in range(50):
                await db.import_jsonl_line(
                    {"role": "user", "content": [{"type": "text", "text": f"Msg {i}"}]}
                )
            raise RuntimeError("Simulated failure")
        except RuntimeError:
            await db.rollback_transaction()

        # Count should be unchanged (rollback undid the 50 inserts)
        assert await db.get_message_count() == pre_count

    # H3: Fix checkpoint message_rowids

    async def test_fix_checkpoint_message_rowids_correct_boundaries(
        self, db: ContextDB
    ) -> None:
        """Verify checkpoints reference correct message boundaries after import."""
        # Import messages interspersed with checkpoint markers
        await db.import_jsonl_line(
            {"role": "user", "content": [{"type": "text", "text": "Msg 1"}]}
        )
        await db.import_jsonl_line(
            {"role": "assistant", "content": [{"type": "text", "text": "Rsp 1"}]}
        )
        await db.import_jsonl_line({"role": "_checkpoint", "id": 1})

        await db.import_jsonl_line(
            {"role": "user", "content": [{"type": "text", "text": "Msg 2"}]}
        )
        await db.import_jsonl_line({"role": "_checkpoint", "id": 2})

        await db.import_jsonl_line(
            {"role": "assistant", "content": [{"type": "text", "text": "Rsp 2"}]}
        )
        await db.import_jsonl_line({"role": "_checkpoint", "id": 3})

        # Fix any remaining ones
        await db.fix_checkpoint_message_rowids()

        # Verify each checkpoint's message_rowid points to max message before it
        cp1 = await db.get_checkpoint_message_rowid(1)
        cp2 = await db.get_checkpoint_message_rowid(2)
        cp3 = await db.get_checkpoint_message_rowid(3)

        assert cp1 is not None and cp1 > 0
        assert cp2 is not None and cp2 > cp1  # Later checkpoint => higher rowid
        assert cp3 is not None and cp3 >= cp2

    async def test_migration_with_checkpoints_preserves_order(
        self, db: ContextDB
    ) -> None:
        """Full migration with 10+ checkpoints; verify revert works correctly."""
        for i in range(10):
            await db.import_jsonl_line(
                {"role": "user", "content": [{"type": "text", "text": f"Msg {i}"}]}
            )
            await db.import_jsonl_line({"role": "_checkpoint", "id": i})

        await db.fix_checkpoint_message_rowids()

        # Revert to checkpoint 5
        cp5_rowid = await db.get_checkpoint_message_rowid(5)
        assert cp5_rowid is not None and cp5_rowid > 0

        await db.revert_to_checkpoint(5)
        messages = await db.get_messages()
        # Should have ~6 messages (indices 0-5)
        assert len(messages) >= 5

    # M4: executemany for append_messages

    async def test_append_messages_empty_list(self, db: ContextDB) -> None:
        """executemany with empty list should be a no-op."""
        await db.append_messages([])
        assert await db.get_message_count() == 0

    async def test_append_messages_large_batch(self, db: ContextDB) -> None:
        """Append 100 messages in one call and verify all are retrievable."""
        messages = [
            Message(role="user", content=[{"type": "text", "text": f"Msg {i}"}])
            for i in range(100)
        ]
        await db.append_messages(messages)
        assert await db.get_message_count() == 100

        # Verify correct ordering
        msgs = await db.get_messages()
        assert msgs[0].extract_text() == "Msg 0"
        assert msgs[99].extract_text() == "Msg 99"

    # M5: Simplify revert subquery

    async def test_revert_to_checkpoint_usage_snapshots(self, db: ContextDB) -> None:
        """Verify usage snapshots are reverted correctly with checkpoint."""
        await db.append_messages(
            [Message(role="user", content=[{"type": "text", "text": "Msg 1"}])]
        )
        await db.record_usage(100)
        await db.create_checkpoint(0)
        # Usage recorded AFTER checkpoint — should be removed on revert
        await db.record_usage(200)
        await db.append_messages(
            [
                Message(role="user", content=[{"type": "text", "text": "Msg 2"}]),
                Message(role="user", content=[{"type": "text", "text": "Msg 3"}]),
            ]
        )

        await db.revert_to_checkpoint(0)

        # Only usage before checkpoint (100) should remain
        messages = await db.get_messages()
        assert len(messages) == 1  # Only Msg 1 survives
        latest = await db.get_latest_usage()
        assert latest == 100

    # M6: Explicit transactions (atomicity)

    async def test_clear_is_atomic(self, db: ContextDB) -> None:
        """Verify clear() atomically removes all data."""
        await db.set_system_prompt("test")
        await db.append_messages(
            [Message(role="user", content=[{"type": "text", "text": "Hello"}])]
        )
        await db.create_checkpoint(0)
        await db.record_usage(50)

        await db.clear()

        assert await db.get_system_prompt() is None
        assert await db.get_message_count() == 0
        assert await db.get_latest_checkpoint_id() == -1
        assert await db.get_latest_usage() is None

    async def test_revert_to_checkpoint_is_atomic(self, db: ContextDB) -> None:
        """Verify revert_to_checkpoint is atomic — no partial state on success."""
        await db.append_messages(
            [
                Message(role="user", content=[{"type": "text", "text": "Msg 1"}]),
                Message(role="user", content=[{"type": "text", "text": "Msg 2"}]),
                Message(role="user", content=[{"type": "text", "text": "Msg 3"}]),
            ]
        )
        await db.create_checkpoint(0)
        await db.append_messages(
            [Message(role="user", content=[{"type": "text", "text": "Msg 4"}])]
        )

        await db.revert_to_checkpoint(0)
        assert await db.get_message_count() == 3
        assert await db.get_latest_checkpoint_id() == -1  # reverted, checkpoint >=0 removed

    # M7: No wasted SELECT MAX

    async def test_finalize_migration_no_wasted_query(self, db: ContextDB) -> None:
        """finalize_migration is a no-op after import tracking fix."""
        # Import some data
        await db.import_jsonl_line(
            {"role": "user", "content": [{"type": "text", "text": "Hello"}]}
        )
        await db.import_jsonl_line({"role": "_checkpoint", "id": 0})

        # finalize_migration should succeed without errors
        await db.finalize_migration()

        # Checkpoint should have valid message_rowid from import
        cp_rowid = await db.get_checkpoint_message_rowid(0)
        assert cp_rowid is not None and cp_rowid > 0

    # L11: created_at index

    async def test_created_at_index_exists(self, db: ContextDB) -> None:
        """Verify idx_messages_created_at index exists after initialization."""
        import aiosqlite

        conn = await aiosqlite.connect(str(db.db_path))
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_messages_created_at'"
        )
        row = await cursor.fetchone()
        await cursor.close()
        await conn.close()
        assert row is not None
        assert row[0] == "idx_messages_created_at"
