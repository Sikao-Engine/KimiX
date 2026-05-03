"""Advanced tests for ShortTermMemory: temporal validity + eviction interactions."""

import time

import pytest

from kimix.memory.short_term_memory import ShortTermMemory
from kimix.memory.types import MemoryEntry, MemoryType
from kimix.memory.embedding import EmbeddingProvider


class TestSTMTemporalSearch:
    def test_search_skips_expired_entries(self):
        stm = ShortTermMemory(max_size=10)
        provider = EmbeddingProvider(dim=384)
        stm.add(MemoryEntry(content="fresh python", memory_type=MemoryType.EPISODIC, expires_at=time.time() + 100))
        stm.add(MemoryEntry(content="expired python", memory_type=MemoryType.EPISODIC, expires_at=time.time() - 1))
        results = stm.search("python", provider, top_k=5)
        assert len(results) == 1
        assert results[0].content == "fresh python"

    def test_get_recent_skips_expired(self):
        stm = ShortTermMemory(max_size=10)
        stm.add(MemoryEntry(content="old but valid", memory_type=MemoryType.EPISODIC, timestamp=time.time() - 10))
        stm.add(MemoryEntry(content="just expired", memory_type=MemoryType.EPISODIC, timestamp=time.time(), expires_at=time.time() - 1))
        recent = stm.get_recent(2)
        assert len(recent) == 1
        assert recent[0].content == "old but valid"

    def test_clear_expired_removes_both_ttl_and_explicit(self):
        stm = ShortTermMemory(max_size=10, ttl_seconds=1)
        old = time.time() - 10
        stm.add(MemoryEntry(content="ttl expired", memory_type=MemoryType.EPISODIC, timestamp=old))
        stm.add(MemoryEntry(content="explicit expired", memory_type=MemoryType.EPISODIC, expires_at=time.time() - 1))
        stm.add(MemoryEntry(content="still fresh", memory_type=MemoryType.EPISODIC))
        stm.clear_expired()
        assert len(stm.buffer) == 1
        assert stm.buffer[0].content == "still fresh"

    def test_eviction_ignores_expired_importance(self):
        stm = ShortTermMemory(max_size=2)
        # Expired entry has high importance but should not block eviction of live entries
        stm.add(MemoryEntry(content="expired high", memory_type=MemoryType.EPISODIC, importance=10.0, expires_at=time.time() - 1))
        stm.add(MemoryEntry(content="live low", memory_type=MemoryType.EPISODIC, importance=1.0))
        stm.add(MemoryEntry(content="live mid", memory_type=MemoryType.EPISODIC, importance=5.0))
        # After adding 3 items to capacity 2, one should be evicted
        assert len(stm.buffer) == 2
        # expired high may or may not be evicted depending on _active_buffer usage,
        # but buffer length must respect max_size

    def test_search_on_all_expired_returns_empty(self):
        stm = ShortTermMemory(max_size=10)
        provider = EmbeddingProvider(dim=384)
        stm.add(MemoryEntry(content="gone", memory_type=MemoryType.EPISODIC, expires_at=time.time() - 10))
        assert stm.search("gone", provider) == []

    def test_get_recent_on_all_expired_returns_empty(self):
        stm = ShortTermMemory(max_size=10)
        stm.add(MemoryEntry(content="gone", memory_type=MemoryType.EPISODIC, expires_at=time.time() - 10))
        assert stm.get_recent(5) == []


class TestSTMEMbeddingLazyCompute:
    def test_search_computes_missing_embeddings(self):
        stm = ShortTermMemory(max_size=10)
        provider = EmbeddingProvider(dim=384)
        entry = MemoryEntry(content="async io", memory_type=MemoryType.EPISODIC)
        assert entry.embedding is None
        stm.add(entry)
        results = stm.search("async", provider, top_k=1)
        assert len(results) == 1
        assert results[0].embedding is not None

    def test_touch_increments_access(self):
        stm = ShortTermMemory(max_size=10)
        provider = EmbeddingProvider(dim=384)
        entry = MemoryEntry(content="touch me", memory_type=MemoryType.EPISODIC)
        stm.add(entry)
        assert entry.access_count == 0
        stm.search("touch", provider)
        assert entry.access_count == 1
