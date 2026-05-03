"""Advanced tests for LongTermMemory: BM25 hybrid, SQLite backend, temporal validity."""

import os
import tempfile
import time

import pytest

from kimix.memory.long_term_memory import LongTermMemory
from kimix.memory.short_term_memory import ShortTermMemory
from kimix.memory.sqlite_backend import SQLiteBackend
from kimix.memory.types import MemoryEntry, MemoryType


class TestLongTermMemoryTemporalValidity:
    def test_retrieve_skips_expired(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            ltm = LongTermMemory(storage_path=path)
            ltm.store("fresh fact", importance=8.0)
            ltm.store("expired fact", importance=8.0, expires_at=time.time() - 1)
            results = ltm.retrieve("fact")
            assert len(results) == 1
            assert results[0].content == "fresh fact"
        finally:
            os.unlink(path)

    def test_forget_reduces_importance_then_deletes(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            ltm = LongTermMemory(storage_path=path)
            ltm.store("forgettable", importance=1.0)
            eid = ltm._hash("forgettable")
            for _ in range(4):
                ltm.forget(eid)
                if eid not in ltm.entries:
                    break
            assert eid not in ltm.entries
        finally:
            os.unlink(path)


class TestLongTermMemoryBM25Hybrid:
    def test_hybrid_retrieve_uses_bm25(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            ltm = LongTermMemory(storage_path=path)
            ltm.store("python asyncio tutorial", importance=5.0, tags=["python"])
            ltm.store("python threading guide", importance=5.0, tags=["python"])
            ltm.store("javascript promises", importance=5.0, tags=["js"])
            # Keyword-heavy query should still find python docs via BM25
            results = ltm.retrieve("python asyncio", top_k=2, use_hybrid=True, bm25_weight=0.5)
            assert len(results) == 2
            contents = {r.content for r in results}
            assert "python asyncio tutorial" in contents
        finally:
            os.unlink(path)

    def test_disable_hybrid(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            ltm = LongTermMemory(storage_path=path)
            ltm.store("a", importance=5.0)
            results = ltm.retrieve("a", use_hybrid=False)
            assert len(results) == 1
        finally:
            os.unlink(path)


class TestLongTermMemorySQLiteBackend:
    def test_store_and_retrieve_sqlite(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = SQLiteBackend(db_path=f"{tmpdir}/ltm.db")
            ltm = LongTermMemory(backend=backend, agent_id="test_agent")
            ltm.store("sqlite fact", importance=7.0, tags=["db"])
            results = ltm.retrieve("sqlite", tag_filter=["db"])
            assert len(results) == 1
            assert results[0].content == "sqlite fact"
            backend.close()

    def test_agent_isolation_sqlite(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = SQLiteBackend(db_path=f"{tmpdir}/ltm.db")
            ltm_a = LongTermMemory(backend=backend, agent_id="a")
            ltm_b = LongTermMemory(backend=backend, agent_id="b")
            ltm_a.store("secret a", importance=5.0)
            ltm_b.store("secret b", importance=5.0)
            assert len(ltm_a.retrieve("secret")) == 1
            assert ltm_a.retrieve("secret")[0].content == "secret a"
            backend.close()

    def test_consolidate_with_sqlite_backend(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = SQLiteBackend(db_path=f"{tmpdir}/ltm.db")
            ltm = LongTermMemory(backend=backend, agent_id="test")
            stm = ShortTermMemory(max_size=10)
            stm.add(MemoryEntry(content="important event", memory_type=MemoryType.EPISODIC, importance=9.0))
            ltm.consolidate(stm, threshold=7.0)
            assert ltm.count() == 1
            assert len(stm.buffer) == 0
            backend.close()

    def test_forget_with_sqlite_backend(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = SQLiteBackend(db_path=f"{tmpdir}/ltm.db")
            ltm = LongTermMemory(backend=backend, agent_id="test")
            ltm.store("forgettable", importance=1.0)
            eid = ltm._hash("forgettable")
            for _ in range(4):
                ltm.forget(eid)
                if ltm._get_entry(eid) is None:
                    break
            assert ltm._get_entry(eid) is None
            backend.close()
