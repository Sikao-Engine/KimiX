"""Performance benchmarks for ContextDB (SQLite-backed context storage).

Benchmarks cover:
- Initialization (WAL mode, schema creation)
- append_messages with various batch sizes
- get_messages with pagination
- get_messages_up_to_turn (streaming + turn detection)
- create_checkpoint / revert_to_checkpoint
- record_usage / get_latest_usage
- export (full context export)
- clear
- Comparison with JsonlContextStorage

All timings are assert-based so the file doubles as a regression test.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from kosong.message import Message

pytestmark = pytest.mark.slow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_text_message(text: str) -> Message:
    """Create a minimal Message with TextPart content."""
    return Message(role="user", content=[{"type": "text", "text": text}])


def _make_messages(count: int, *, role: str = "user") -> list[Message]:
    """Create *count* synthetic messages with varying content."""
    msgs: list[Message] = []
    for i in range(count):
        text = f"This is message number {i} with some realistic content for benchmarking purposes. " * 3
        msgs.append(Message(role=role, content=[{"type": "text", "text": text}]))
    return msgs


def _make_conversation_turns(turn_count: int) -> list[Message]:
    """Create a realistic conversation with user/assistant turns."""
    msgs: list[Message] = []
    for i in range(turn_count):
        msgs.append(
            Message(role="user", content=[{"type": "text", "text": f"User turn {i}: Hello, can you help me with this question?"}])
        )
        msgs.append(
            Message(role="assistant", content=[{"type": "text", "text": f"Assistant response {i}: Sure, let me help you with that task. Here is my answer."}])
        )
    return msgs


# ---------------------------------------------------------------------------
# ContextDB benchmarks
# ---------------------------------------------------------------------------


class TestContextDBInitBenchmark:
    """Benchmarks for ContextDB initialization."""

    @pytest.mark.asyncio
    async def test_initialize_new_db(self, tmp_path: Path) -> None:
        """Initialize a fresh database (WAL + schema)."""
        from kimi_cli.soul.context_db import ContextDB

        start = time.perf_counter()
        for _ in range(100):
            db_path = tmp_path / f"init_{_}.db"
            db = ContextDB(db_path)
            await db.initialize()
            await db.close()
        elapsed = time.perf_counter() - start
        # 100 inits should be fast — SQLite in WAL mode + fresh schema
        assert elapsed < 30.0, f"100× init took {elapsed:.3f}s (>30.0s)"

    @pytest.mark.asyncio
    async def test_reinitialize_existing_db(self, tmp_path: Path) -> None:
        """Re-initialize an existing database (schema idempotency)."""
        from kimi_cli.soul.context_db import ContextDB

        db_path = tmp_path / "reinit.db"
        db = ContextDB(db_path)
        await db.initialize()
        await db.close()

        db2 = ContextDB(db_path)
        start = time.perf_counter()
        for _ in range(50):
            await db2.initialize()
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0, f"50× re-init took {elapsed:.3f}s (>10.0s)"
        await db2.close()


class TestContextDBAppendBenchmark:
    """Benchmarks for append_messages."""

    @pytest.mark.asyncio
    async def test_append_single_messages(self, tmp_path: Path) -> None:
        """Append messages one at a time (worst-case)."""
        from kimi_cli.soul.context_db import ContextDB
        db = ContextDB(tmp_path / "append_single.db")
        await db.initialize()

        start = time.perf_counter()
        for i in range(500):
            await db.append_messages([_make_text_message(f"Message {i}")])
        elapsed = time.perf_counter() - start
        count = await db.get_message_count()
        assert count == 500
        # Each individual append causes a separate commit
        assert elapsed < 15.0, f"500× single-append took {elapsed:.3f}s (>15.0s)"
        await db.close()

    @pytest.mark.asyncio
    async def test_append_batch_small(self, tmp_path: Path) -> None:
        """Append 100 messages at once, repeated 50 times."""
        from kimi_cli.soul.context_db import ContextDB
        db = ContextDB(tmp_path / "append_batch_small.db")
        await db.initialize()
        batch = _make_messages(100)

        start = time.perf_counter()
        for _ in range(50):
            await db.append_messages(batch)
        elapsed = time.perf_counter() - start
        count = await db.get_message_count()
        assert count == 5000
        assert elapsed < 15.0, f"50× batch-100 append took {elapsed:.3f}s (>15.0s)"
        await db.close()

    @pytest.mark.asyncio
    async def test_append_batch_large(self, tmp_path: Path) -> None:
        """Append 10,000 messages in one batch."""
        from kimi_cli.soul.context_db import ContextDB
        db = ContextDB(tmp_path / "append_batch_large.db")
        await db.initialize()

        # Warm up
        warmup = _make_messages(100)
        await db.append_messages(warmup)
        await db.clear()

        batch = _make_messages(10_000)
        start = time.perf_counter()
        await db.append_messages(batch)
        elapsed = time.perf_counter() - start
        count = await db.get_message_count()
        assert count == 10_000
        # 10k messages in one executemany call
        assert elapsed < 10.0, f"10k batch append took {elapsed:.3f}s (>10.0s)"
        await db.close()

    @pytest.mark.asyncio
    async def test_append_with_explicit_transaction(self, tmp_path: Path) -> None:
        """Bulk append inside an explicit transaction."""
        from kimi_cli.soul.context_db import ContextDB
        db = ContextDB(tmp_path / "append_tx.db")
        await db.initialize()

        total_msgs = 5000
        batch_size = 500
        batches = total_msgs // batch_size

        start = time.perf_counter()
        await db.begin_transaction()
        try:
            for _ in range(batches):
                await db.append_messages(_make_messages(batch_size))
            await db.commit_transaction()
        except Exception:
            await db.rollback_transaction()
            raise
        elapsed = time.perf_counter() - start
        count = await db.get_message_count()
        assert count == total_msgs
        # Explicit transaction should be faster (single commit)
        assert elapsed < 8.0, f"5k tx-append took {elapsed:.3f}s (>8.0s)"
        await db.close()


class TestContextDBReadBenchmark:
    """Benchmarks for message retrieval."""

    @pytest.mark.asyncio
    async def test_get_all_messages(self, tmp_path: Path) -> None:
        """get_messages() with 10,000 stored messages."""
        from kimi_cli.soul.context_db import ContextDB
        db = ContextDB(tmp_path / "get_all.db")
        await db.initialize()
        await db.append_messages(_make_messages(10_000))

        start = time.perf_counter()
        for _ in range(20):
            msgs = await db.get_messages()
            assert len(msgs) == 10_000
        elapsed = time.perf_counter() - start
        assert elapsed < 15.0, f"20× get_all(10k) took {elapsed:.3f}s (>15.0s)"
        await db.close()

    @pytest.mark.asyncio
    async def test_get_messages_with_limit(self, tmp_path: Path) -> None:
        """get_messages(limit=N) with various limits."""
        from kimi_cli.soul.context_db import ContextDB
        db = ContextDB(tmp_path / "get_limit.db")
        await db.initialize()
        await db.append_messages(_make_messages(10_000))

        for limit in [10, 100, 500]:
            start = time.perf_counter()
            for _ in range(100):
                msgs = await db.get_messages(limit=limit)
                assert len(msgs) == limit
            elapsed = time.perf_counter() - start
            assert elapsed < 10.0, f"100× get_messages(limit={limit}) took {elapsed:.3f}s"
        await db.close()

    @pytest.mark.asyncio
    async def test_get_messages_after_rowid(self, tmp_path: Path) -> None:
        """get_messages(after_rowid=N) pagination."""
        from kimi_cli.soul.context_db import ContextDB
        db = ContextDB(tmp_path / "get_after.db")
        await db.initialize()
        await db.append_messages(_make_messages(10_000))

        start = time.perf_counter()
        for after in range(0, 9000, 1000):
            msgs = await db.get_messages(after_rowid=after, limit=500)
            assert len(msgs) == min(500, 10000 - after)
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0, f"9 paginated reads took {elapsed:.3f}s (>5.0s)"
        await db.close()

    @pytest.mark.asyncio
    async def test_get_message_count(self, tmp_path: Path) -> None:
        """get_message_count() — fast COUNT query."""
        from kimi_cli.soul.context_db import ContextDB
        db = ContextDB(tmp_path / "count.db")
        await db.initialize()
        await db.append_messages(_make_messages(10_000))

        start = time.perf_counter()
        for _ in range(10_000):
            count = await db.get_message_count()
            assert count == 10_000
        elapsed = time.perf_counter() - start
        # COUNT is indexed? No — but it's a fast full scan on small table
        assert elapsed < 10.0, f"10k× COUNT took {elapsed:.3f}s (>10.0s)"
        await db.close()

    @pytest.mark.asyncio
    async def test_get_last_message_rowid(self, tmp_path: Path) -> None:
        """get_last_message_rowid() — MAX(rowid) query."""
        from kimi_cli.soul.context_db import ContextDB
        db = ContextDB(tmp_path / "last_rowid.db")
        await db.initialize()
        await db.append_messages(_make_messages(10_000))

        start = time.perf_counter()
        for _ in range(10_000):
            rowid = await db.get_last_message_rowid()
            assert rowid == 10_000
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0, f"10k× MAX(rowid) took {elapsed:.3f}s (>10.0s)"
        await db.close()

    @pytest.mark.asyncio
    async def test_has_visible_messages(self, tmp_path: Path) -> None:
        """has_visible_messages() with many messages."""
        from kimi_cli.soul.context_db import ContextDB
        db = ContextDB(tmp_path / "has_visible.db")
        await db.initialize()
        await db.append_messages(_make_messages(10_000))

        start = time.perf_counter()
        for _ in range(5_000):
            visible = await db.has_visible_messages()
            assert visible is True
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0, f"5k× has_visible took {elapsed:.3f}s (>10.0s)"
        await db.close()


class TestContextDBTurnDetectionBenchmark:
    """Benchmarks for get_messages_up_to_turn (streaming + checkpoint detection)."""

    @pytest.mark.asyncio
    async def test_turn_detection_small(self, tmp_path: Path) -> None:
        """get_messages_up_to_turn on 50 turns (100 messages)."""
        from kimi_cli.soul.context_db import ContextDB
        db = ContextDB(tmp_path / "turn_small.db")
        await db.initialize()
        await db.append_messages(_make_conversation_turns(50))

        start = time.perf_counter()
        for _ in range(500):
            result = await db.get_messages_up_to_turn(49)
            assert len(result) == 100
        elapsed = time.perf_counter() - start
        assert elapsed < 15.0, f"500× turn_detect(50) took {elapsed:.3f}s (>15.0s)"
        await db.close()

    @pytest.mark.asyncio
    async def test_turn_detection_medium(self, tmp_path: Path) -> None:
        """get_messages_up_to_turn on 500 turns (1000 messages)."""
        from kimi_cli.soul.context_db import ContextDB
        db = ContextDB(tmp_path / "turn_medium.db")
        await db.initialize()
        await db.append_messages(_make_conversation_turns(500))

        start = time.perf_counter()
        for _ in range(50):
            result = await db.get_messages_up_to_turn(499)
            assert len(result) == 1000
        elapsed = time.perf_counter() - start
        assert elapsed < 15.0, f"50× turn_detect(500) took {elapsed:.3f}s (>15.0s)"
        await db.close()

    @pytest.mark.asyncio
    async def test_turn_detection_early_stop(self, tmp_path: Path) -> None:
        """get_messages_up_to_turn with early termination (turn 0)."""
        from kimi_cli.soul.context_db import ContextDB
        db = ContextDB(tmp_path / "turn_early.db")
        await db.initialize()
        await db.append_messages(_make_conversation_turns(500))

        start = time.perf_counter()
        for _ in range(500):
            result = await db.get_messages_up_to_turn(0)
            assert len(result) == 2
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0, f"500× turn_detect(early_stop) took {elapsed:.3f}s (>10.0s)"
        await db.close()

    @pytest.mark.asyncio
    async def test_turn_detection_with_checkpoints(self, tmp_path: Path) -> None:
        """get_messages_up_to_turn with checkpoint markers in the data."""
        from kimi_cli.soul.context_db import ContextDB
        db = ContextDB(tmp_path / "turn_cp.db")
        await db.initialize()

        # Insert messages interleaved with checkpoints
        all_msgs: list[Message] = []
        for i in range(100):
            all_msgs.append(
                Message(role="user", content=[{"type": "text", "text": f"Turn {i}"}])
            )
            all_msgs.append(
                Message(role="assistant", content=[{"type": "text", "text": f"Response {i}"}])
            )
            # Add checkpoint user messages like the real system does (wrapped in <system> tags)
            all_msgs.append(
                Message(role="user", content=[{"type": "text", "text": f"<system>CHECKPOINT {i}</system>"}])
            )
        await db.append_messages(all_msgs)

        start = time.perf_counter()
        for _ in range(100):
            result = await db.get_messages_up_to_turn(49)
            # 49 real turns + checkpoints = 49*2 real msgs + 50 checkpoint msgs
            assert len(result) >= 98  # At least the real messages
        elapsed = time.perf_counter() - start
        assert elapsed < 15.0, f"100× turn_detect(cp) took {elapsed:.3f}s (>15.0s)"
        await db.close()


class TestContextDBCheckpointBenchmark:
    """Benchmarks for checkpoint operations."""

    @pytest.mark.asyncio
    async def test_create_checkpoints(self, tmp_path: Path) -> None:
        """create_checkpoint() — insert 1000 checkpoints."""
        from kimi_cli.soul.context_db import ContextDB
        db = ContextDB(tmp_path / "cp_create.db")
        await db.initialize()
        await db.append_messages(_make_messages(1000))

        start = time.perf_counter()
        for i in range(1000):
            rowid = await db.create_checkpoint(i)
            assert rowid > 0
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0, f"1000× create_checkpoint took {elapsed:.3f}s (>10.0s)"
        await db.close()

    @pytest.mark.asyncio
    async def test_revert_to_checkpoint_large(self, tmp_path: Path) -> None:
        """revert_to_checkpoint() — revert 5000 messages down to 100."""
        from kimi_cli.soul.context_db import ContextDB
        db = ContextDB(tmp_path / "cp_revert_large.db")
        await db.initialize()

        # Create 10 separate databases for independent revert measurements
        times: list[float] = []
        for trial in range(10):
            await db.clear()
            await db.append_messages(_make_messages(100))
            await db.create_checkpoint(0)
            await db.append_messages(_make_messages(4900))
            assert await db.get_message_count() == 5000

            t0 = time.perf_counter()
            await db.revert_to_checkpoint(0)
            elapsed = time.perf_counter() - t0
            times.append(elapsed)
            count = await db.get_message_count()
            assert count == 100, f"trial {trial}: expected 100, got {count}"

        total = sum(times)
        avg = total / len(times) * 1000
        print(f"\n  revert-large (10×) total={total:.4f}s avg={avg:.2f}ms")
        assert total < 8.0, f"10× revert(large) took {total:.3f}s (>8.0s)"
        await db.close()

    @pytest.mark.asyncio
    async def test_revert_mid_conversation(self, tmp_path: Path) -> None:
        """revert_to_checkpoint() — revert mid-conversation, deleting 400 msgs."""
        from kimi_cli.soul.context_db import ContextDB
        db = ContextDB(tmp_path / "cp_revert_mid.db")
        await db.initialize()

        times: list[float] = []
        for trial in range(10):
            await db.clear()
            # 10 checkpoints across 1000 messages
            for i in range(10):
                await db.append_messages(_make_messages(100))
                await db.create_checkpoint(i)
            assert await db.get_message_count() == 1000

            t0 = time.perf_counter()
            await db.revert_to_checkpoint(5)
            elapsed = time.perf_counter() - t0
            times.append(elapsed)
            count = await db.get_message_count()
            assert count == 600, f"trial {trial}: expected 600, got {count}"

        total = sum(times)
        avg = total / len(times) * 1000
        print(f"\n  revert-mid (10×) total={total:.4f}s avg={avg:.2f}ms")
        assert total < 8.0, f"10× revert(mid) took {total:.3f}s (>8.0s)"
        await db.close()

    @pytest.mark.asyncio
    async def test_get_checkpoint_message_rowid(self, tmp_path: Path) -> None:
        """get_checkpoint_message_rowid() — fast lookup."""
        from kimi_cli.soul.context_db import ContextDB
        db = ContextDB(tmp_path / "cp_get_rowid.db")
        await db.initialize()
        await db.append_messages(_make_messages(1000))
        await db.create_checkpoint(42)

        start = time.perf_counter()
        for _ in range(10_000):
            rowid = await db.get_checkpoint_message_rowid(42)
            assert rowid is not None and rowid > 0
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0, f"10k× get_checkpoint_message_rowid took {elapsed:.3f}s (>10.0s)"
        await db.close()


class TestContextDBUsageBenchmark:
    """Benchmarks for usage snapshots."""

    @pytest.mark.asyncio
    async def test_record_and_get_usage(self, tmp_path: Path) -> None:
        """record_usage() + get_latest_usage() — 1000 records."""
        from kimi_cli.soul.context_db import ContextDB
        db = ContextDB(tmp_path / "usage.db")
        await db.initialize()

        start = time.perf_counter()
        for i in range(1000):
            await db.record_usage(i * 100)
        elapsed_record = time.perf_counter() - start
        assert elapsed_record < 8.0, f"1000× record_usage took {elapsed_record:.3f}s (>8.0s)"

        start = time.perf_counter()
        for _ in range(1000):
            usage = await db.get_latest_usage()
            assert usage == 999 * 100
        elapsed_get = time.perf_counter() - start
        assert elapsed_get < 10.0, f"1000× get_latest_usage took {elapsed_get:.3f}s (>10.0s)"
        await db.close()


class TestContextDBExportBenchmark:
    """Benchmarks for full export."""

    @pytest.mark.asyncio
    async def test_export_small(self, tmp_path: Path) -> None:
        """export() on 100 messages."""
        from kimi_cli.soul.context_db import ContextDB
        db = ContextDB(tmp_path / "export_small.db")
        await db.initialize()
        await db.set_system_prompt("You are a helpful assistant.")
        await db.append_messages(_make_messages(100))
        await db.create_checkpoint(0)
        await db.record_usage(500)

        start = time.perf_counter()
        for _ in range(500):
            exported = await db.export()
            assert len(exported.messages) == 100
            assert exported.system_prompt == "You are a helpful assistant."
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0, f"500× export(small) took {elapsed:.3f}s (>10.0s)"
        await db.close()

    @pytest.mark.asyncio
    async def test_export_large(self, tmp_path: Path) -> None:
        """export() on 10,000 messages."""
        from kimi_cli.soul.context_db import ContextDB
        db = ContextDB(tmp_path / "export_large.db")
        await db.initialize()
        await db.append_messages(_make_messages(10_000))
        for i in range(10):
            await db.create_checkpoint(i)
        await db.record_usage(50_000)

        start = time.perf_counter()
        for _ in range(10):
            exported = await db.export()
            assert len(exported.messages) == 10_000
            assert len(exported.checkpoints) == 10
        elapsed = time.perf_counter() - start
        assert elapsed < 15.0, f"10× export(large) took {elapsed:.3f}s (>15.0s)"
        await db.close()


class TestContextDBClearBenchmark:
    """Benchmarks for clear operations."""

    @pytest.mark.asyncio
    async def test_clear(self, tmp_path: Path) -> None:
        """clear() — delete all data from 10,000 messages."""
        from kimi_cli.soul.context_db import ContextDB
        db = ContextDB(tmp_path / "clear.db")
        await db.initialize()
        await db.set_system_prompt("test")
        await db.append_messages(_make_messages(10_000))
        for i in range(10):
            await db.create_checkpoint(i)
        await db.record_usage(42)

        start = time.perf_counter()
        for _ in range(100):
            await db.clear()
            assert await db.get_message_count() == 0
            assert await db.get_system_prompt() is None
            assert await db.get_latest_checkpoint_id() == -1
            assert await db.get_latest_usage() is None
            # Re-populate for next iteration (except last)
            if _ < 99:
                await db.set_system_prompt("test")
                await db.append_messages(_make_messages(10_000))
                for i in range(10):
                    await db.create_checkpoint(i)
                await db.record_usage(42)
        elapsed = time.perf_counter() - start
        assert elapsed < 20.0, f"100× clear took {elapsed:.3f}s (>20.0s)"
        await db.close()


class TestContextDBSystemPromptBenchmark:
    """Benchmarks for system prompt operations."""

    @pytest.mark.asyncio
    async def test_set_system_prompt(self, tmp_path: Path) -> None:
        """set_system_prompt() — 1000 updates."""
        from kimi_cli.soul.context_db import ContextDB
        db = ContextDB(tmp_path / "sp_set.db")
        await db.initialize()

        start = time.perf_counter()
        for i in range(1000):
            await db.set_system_prompt(f"System prompt version {i}")
        elapsed = time.perf_counter() - start
        assert elapsed < 8.0, f"1000× set_system_prompt took {elapsed:.3f}s (>8.0s)"
        await db.close()

    @pytest.mark.asyncio
    async def test_get_system_prompt(self, tmp_path: Path) -> None:
        """get_system_prompt() — 10,000 reads."""
        from kimi_cli.soul.context_db import ContextDB
        db = ContextDB(tmp_path / "sp_get.db")
        await db.initialize()
        await db.set_system_prompt("You are a benchmark bot.")

        start = time.perf_counter()
        for _ in range(10_000):
            sp = await db.get_system_prompt()
            assert sp == "You are a benchmark bot."
        elapsed = time.perf_counter() - start
        assert elapsed < 8.0, f"10k× get_system_prompt took {elapsed:.3f}s (>8.0s)"
        await db.close()


# ---------------------------------------------------------------------------
# SQLite vs JSONL comparison benchmarks
# ---------------------------------------------------------------------------


class TestStorageBackendComparisonBenchmark:
    """Compare SQLite storage vs JSONL storage performance."""

    @pytest.mark.asyncio
    async def test_append_comparison(self, tmp_path: Path) -> None:
        """Compare append_messages speed: SQLite vs JSONL."""
        from kimi_cli.soul.context import SqliteContextStorage, JsonlContextStorage

        SQLITE_PATH = tmp_path / "compare.db"
        JSONL_PATH = tmp_path / "compare.jsonl"

        sqlite_store = SqliteContextStorage(SQLITE_PATH)
        jsonl_store = JsonlContextStorage(JSONL_PATH)
        await sqlite_store.initialize()
        await jsonl_store.initialize()

        msgs = _make_messages(500)

        # JSONL
        start = time.perf_counter()
        for _ in range(20):
            await jsonl_store.append_messages(msgs)
        elapsed_jsonl = time.perf_counter() - start

        # SQLite
        start = time.perf_counter()
        for _ in range(20):
            await sqlite_store.append_messages(msgs)
        elapsed_sqlite = time.perf_counter() - start

        # SQLite should be faster or comparable (no JSONL file parsing)
        ratio = elapsed_sqlite / max(elapsed_jsonl, 0.001)
        assert ratio < 2.0, f"SQLite {elapsed_sqlite:.3f}s vs JSONL {elapsed_jsonl:.3f}s (ratio={ratio:.2f})"

        await sqlite_store.close()
        # JSONL doesn't need close

    @pytest.mark.asyncio
    async def test_get_messages_comparison(self, tmp_path: Path) -> None:
        """Compare get_messages speed: SQLite vs JSONL."""
        from kimi_cli.soul.context import SqliteContextStorage, JsonlContextStorage

        SQLITE_PATH = tmp_path / "compare_get.db"
        JSONL_PATH = tmp_path / "compare_get.jsonl"

        sqlite_store = SqliteContextStorage(SQLITE_PATH)
        jsonl_store = JsonlContextStorage(JSONL_PATH)
        await sqlite_store.initialize()
        await jsonl_store.initialize()

        msgs = _make_messages(2000)
        await sqlite_store.append_messages(msgs)
        await jsonl_store.append_messages(msgs)

        # JSONL
        start = time.perf_counter()
        for _ in range(20):
            retrieved = await jsonl_store.get_messages()
            assert len(retrieved) == 2000
        elapsed_jsonl = time.perf_counter() - start

        # SQLite
        start = time.perf_counter()
        for _ in range(20):
            retrieved = await sqlite_store.get_messages()
            assert len(retrieved) == 2000
        elapsed_sqlite = time.perf_counter() - start

        # SQLite should be significantly faster for reads (indexed)
        assert elapsed_sqlite < elapsed_jsonl * 0.5, (
            f"SQLite {elapsed_sqlite:.3f}s should be faster than JSONL {elapsed_jsonl:.3f}s"
        )

        await sqlite_store.close()

    @pytest.mark.asyncio
    async def test_get_message_count_comparison(self, tmp_path: Path) -> None:
        """Compare get_message_count speed: SQLite vs JSONL."""
        from kimi_cli.soul.context import SqliteContextStorage, JsonlContextStorage

        SQLITE_PATH = tmp_path / "compare_count.db"
        JSONL_PATH = tmp_path / "compare_count.jsonl"

        sqlite_store = SqliteContextStorage(SQLITE_PATH)
        jsonl_store = JsonlContextStorage(JSONL_PATH)
        await sqlite_store.initialize()
        await jsonl_store.initialize()

        msgs = _make_messages(5000)
        await sqlite_store.append_messages(msgs)
        await jsonl_store.append_messages(msgs)

        # Warm up
        assert await sqlite_store.get_message_count() == 5000
        assert await jsonl_store.get_message_count() == 5000

        # JSONL
        start = time.perf_counter()
        for _ in range(100):
            await jsonl_store.get_message_count()
        elapsed_jsonl = time.perf_counter() - start

        # SQLite
        start = time.perf_counter()
        for _ in range(100):
            await sqlite_store.get_message_count()
        elapsed_sqlite = time.perf_counter() - start

        # SQLite COUNT should be much faster than JSONL line counting
        assert elapsed_sqlite < elapsed_jsonl * 0.2, (
            f"SQLite COUNT {elapsed_sqlite:.3f}s should be much faster than JSONL {elapsed_jsonl:.3f}s"
        )

        await sqlite_store.close()


# ---------------------------------------------------------------------------
# SqliteContextStorage (wrapper) benchmarks
# ---------------------------------------------------------------------------


class TestSqliteContextStorageBenchmark:
    """Benchmarks for SqliteContextStorage (the ContextStorage wrapper)."""

    @pytest.mark.asyncio
    async def test_restore_full(self, tmp_path: Path) -> None:
        """SqliteContextStorage.restore_full() — full state restoration."""
        from kimi_cli.soul.context import SqliteContextStorage
        store = SqliteContextStorage(tmp_path / "restore_full.db")
        await store.initialize()

        await store.set_system_prompt("Test system prompt.")
        await store.append_messages(_make_messages(2000))
        for i in range(5):
            await store.create_checkpoint(i)
        await store.record_usage(10_000)

        start = time.perf_counter()
        for _ in range(50):
            sp, msgs, usage, cp_id, pending = await store.restore_full()
            assert sp == "Test system prompt."
            assert len(msgs) == 2000
            assert usage == 10_000
        elapsed = time.perf_counter() - start
        assert elapsed < 15.0, f"50× restore_full took {elapsed:.3f}s (>15.0s)"
        await store.close()

    @pytest.mark.asyncio
    async def test_storage_lifecycle(self, tmp_path: Path) -> None:
        """Full lifecycle: init → append → checkpoint → revert → clear → close."""
        from kimi_cli.soul.context import SqliteContextStorage
        store = SqliteContextStorage(tmp_path / "lifecycle.db")

        start = time.perf_counter()
        for _ in range(100):
            await store.initialize()
            await store.set_system_prompt("test")
            await store.append_messages(_make_messages(50))
            await store.create_checkpoint(0)
            await store.record_usage(100)
            await store.revert_to_checkpoint(0)
            await store.clear()
            await store.close()
        elapsed = time.perf_counter() - start
        assert elapsed < 30.0, f"100× lifecycle took {elapsed:.3f}s (>30.0s)"