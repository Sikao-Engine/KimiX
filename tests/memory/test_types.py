"""Tests for memory types."""

import time

import pytest

from kimix.memory.types import MemoryEntry, MemoryType


class TestMemoryType:
    def test_memory_type_values(self):
        assert MemoryType.EPISODIC.value == "episodic"
        assert MemoryType.SEMANTIC.value == "semantic"
        assert MemoryType.WORKING.value == "working"
        assert MemoryType.WORKING.value == "working"


class TestMemoryEntry:
    def test_default_creation(self):
        entry = MemoryEntry(content="test", memory_type=MemoryType.SEMANTIC)
        assert entry.content == "test"
        assert entry.memory_type == MemoryType.SEMANTIC
        assert entry.importance == 1.0
        assert entry.access_count == 0
        assert entry.tags == []
        assert entry.source == ""
        assert entry.metadata == {}
        assert entry.embedding is None

    def test_custom_creation(self):
        entry = MemoryEntry(
            content="custom",
            memory_type=MemoryType.EPISODIC,
            importance=8.0,
            tags=["tag1", "tag2"],
            source="test_source",
            metadata={"key": "value"},
        )
        assert entry.importance == 8.0
        assert entry.tags == ["tag1", "tag2"]
        assert entry.source == "test_source"
        assert entry.metadata == {"key": "value"}

    def test_touch(self):
        entry = MemoryEntry(content="test", memory_type=MemoryType.WORKING)
        old_accessed = entry.last_accessed
        time.sleep(0.01)
        entry.touch()
        assert entry.access_count == 1
        assert entry.last_accessed > old_accessed

    def test_effective_importance_decay(self):
        entry = MemoryEntry(
            content="old",
            memory_type=MemoryType.SEMANTIC,
            timestamp=time.time() - 86400 * 10,  # 10 days old
            importance=10.0,
        )
        effective = entry.get_effective_importance()
        assert effective < entry.importance  # Should decay over time

    def test_access_boost(self):
        entry = MemoryEntry(content="popular", memory_type=MemoryType.SEMANTIC, importance=5.0)
        for _ in range(20):
            entry.touch()
        effective = entry.get_effective_importance()
        assert effective > entry.importance  # Access boosts importance

    def test_to_dict(self):
        entry = MemoryEntry(content="test", memory_type=MemoryType.WORKING)
        d = entry.to_dict()
        assert d["content"] == "test"
        assert d["memory_type"] == "working"
        assert "effective_importance" in d
