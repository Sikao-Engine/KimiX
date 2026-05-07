"""Tests for SQLiteBackend."""

import tempfile
import time

import pytest

from kimix.memory.sqlite_backend import SQLiteBackend
from kimix.memory.types import MemoryEntry, MemoryType


class TestSQLiteBackend:
    def test_store_and_get(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteBackend(db_path=f"{tmpdir}/test.db")
            entry = MemoryEntry(content="hello", memory_type=MemoryType.SEMANTIC, importance=5.0)
            db.store(entry, "id1", dim=384)
            fetched = db.get("id1", dim=384)
            assert fetched is not None
            assert fetched.content == "hello"
            assert fetched.memory_type == MemoryType.SEMANTIC
            db.close()

    def test_delete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteBackend(db_path=f"{tmpdir}/test.db")
            db.store(MemoryEntry(content="del me", memory_type=MemoryType.SEMANTIC), "id2")
            assert db.delete("id2") is True
            assert db.get("id2") is None
            assert db.delete("missing") is False
            db.close()

    def test_list_all_agent_isolation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteBackend(db_path=f"{tmpdir}/test.db")
            db.store(MemoryEntry(content="a", memory_type=MemoryType.SEMANTIC, agent_id="agent1"), "a1")
            db.store(MemoryEntry(content="b", memory_type=MemoryType.SEMANTIC, agent_id="agent2"), "a2")
            a1_items = db.list_all(agent_id="agent1")
            assert len(a1_items) == 1
            assert a1_items[0][1].agent_id == "agent1"
            db.close()

    def test_list_all_exclude_expired(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteBackend(db_path=f"{tmpdir}/test.db")
            db.store(MemoryEntry(content="fresh", memory_type=MemoryType.SEMANTIC, expires_at=time.time() + 100), "f1")
            db.store(MemoryEntry(content="expired", memory_type=MemoryType.SEMANTIC, expires_at=time.time() - 1), "e1")
            active = db.list_all(exclude_expired=True)
            assert len(active) == 1
            assert active[0][1].content == "fresh"
            db.close()

    def test_update_access(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteBackend(db_path=f"{tmpdir}/test.db")
            db.store(MemoryEntry(content="acc", memory_type=MemoryType.SEMANTIC, access_count=0), "acc1")
            db.update_access("acc1")
            fetched = db.get("acc1")
            assert fetched is not None
            assert fetched.access_count == 1
            assert fetched.last_accessed > 0
            db.close()

    def test_update_importance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteBackend(db_path=f"{tmpdir}/test.db")
            db.store(MemoryEntry(content="imp", memory_type=MemoryType.SEMANTIC, importance=3.0), "imp1")
            db.update_importance("imp1", 9.0)
            fetched = db.get("imp1")
            assert fetched is not None
            assert fetched.importance == 9.0
            db.close()

    def test_search_by_tag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteBackend(db_path=f"{tmpdir}/test.db")
            db.store(MemoryEntry(content="tagged", memory_type=MemoryType.SEMANTIC, tags=["python", "async"]), "t1")
            db.store(MemoryEntry(content="other", memory_type=MemoryType.SEMANTIC, tags=["python"]), "t2")
            results = db.search_by_tag(["python", "async"])
            assert len(results) == 1
            assert results[0][1].content == "tagged"
            db.close()

    def test_count(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteBackend(db_path=f"{tmpdir}/test.db")
            assert db.count() == 0
            db.store(MemoryEntry(content="c1", memory_type=MemoryType.SEMANTIC), "c1")
            assert db.count() == 1
            db.close()

    def test_embedding_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteBackend(db_path=f"{tmpdir}/test.db")
            import numpy as np
            vec = np.array([0.1, 0.2, 0.3], dtype=np.float32)
            db.store(MemoryEntry(content="emb", memory_type=MemoryType.SEMANTIC, embedding=vec), "emb1")
            fetched = db.get("emb1", dim=3, include_embedding=True)
            assert fetched is not None
            assert fetched.embedding is not None
            assert np.allclose(fetched.embedding, vec)
            db.close()

    def test_reflect(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteBackend(db_path=f"{tmpdir}/test.db")
            assert "0 rows" in db.reflect()
            db.close()
