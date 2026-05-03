"""Behavioral tests for MemoryEntry: importance math, touch, serialization stability."""

import time
import math

import pytest

from kimix.memory.types import MemoryEntry, MemoryType


class TestEffectiveImportance:
    def test_default_effective_importance(self):
        e = MemoryEntry(content="x", memory_type=MemoryType.SEMANTIC)
        # Fresh entry: days_old ≈ 0, recency ≈ 1, access_boost = 0
        assert e.get_effective_importance() == pytest.approx(1.0, abs=0.01)

    def test_decay_over_time(self):
        old_time = time.time() - 10 * 86400  # 10 days ago
        e = MemoryEntry(content="x", memory_type=MemoryType.SEMANTIC, timestamp=old_time, importance=1.0)
        eff = e.get_effective_importance()
        expected = math.exp(-0.1 * 10)  # ~0.368
        assert eff == pytest.approx(expected, abs=0.01)

    def test_access_boost(self):
        e = MemoryEntry(content="x", memory_type=MemoryType.SEMANTIC, importance=1.0, access_count=10)
        # access_boost = min(10 * 0.1, 2.0) = 1.0
        assert e.get_effective_importance() == pytest.approx(2.0, abs=0.01)

    def test_access_boost_capped(self):
        e = MemoryEntry(content="x", memory_type=MemoryType.SEMANTIC, importance=1.0, access_count=100)
        # access_boost capped at 2.0
        assert e.get_effective_importance() == pytest.approx(3.0, abs=0.01)

    def test_combined_decay_and_boost(self):
        old_time = time.time() - 5 * 86400
        e = MemoryEntry(content="x", memory_type=MemoryType.SEMANTIC, timestamp=old_time, importance=2.0, access_count=5)
        recency = math.exp(-0.1 * 5)
        boost = 1 + min(5 * 0.1, 2.0)
        expected = 2.0 * recency * boost
        assert e.get_effective_importance() == pytest.approx(expected, abs=0.01)


class TestTouch:
    def test_touch_increments_count(self):
        e = MemoryEntry(content="x", memory_type=MemoryType.SEMANTIC)
        assert e.access_count == 0
        e.touch()
        assert e.access_count == 1

    def test_touch_updates_last_accessed(self):
        past = time.time() - 100
        e = MemoryEntry(content="x", memory_type=MemoryType.SEMANTIC, last_accessed=past)
        e.touch()
        assert e.last_accessed > past

    def test_multiple_touches(self):
        e = MemoryEntry(content="x", memory_type=MemoryType.SEMANTIC)
        for _ in range(5):
            e.touch()
        assert e.access_count == 5


class TestToDict:
    def test_to_dict_contains_effective_importance(self):
        e = MemoryEntry(content="x", memory_type=MemoryType.SEMANTIC, importance=5.0)
        d = e.to_dict()
        assert "effective_importance" in d
        assert d["effective_importance"] == pytest.approx(5.0, abs=0.1)

    def test_to_dict_embedding_as_list(self):
        import numpy as np
        e = MemoryEntry(content="x", memory_type=MemoryType.SEMANTIC, embedding=np.array([0.1, 0.2]))
        d = e.to_dict()
        assert d["embedding"] == [0.1, 0.2]

    def test_to_dict_none_embedding(self):
        e = MemoryEntry(content="x", memory_type=MemoryType.SEMANTIC, embedding=None)
        d = e.to_dict()
        assert d["embedding"] is None


class TestFromDict:
    def test_from_dict_with_extra_fields_ignored(self):
        d = {
            "content": "x",
            "memory_type": "semantic",
            "unknown_field": 123,
        }
        e = MemoryEntry.from_dict(d)
        assert e.content == "x"

    def test_from_dict_partial(self):
        d = {
            "content": "partial",
            "memory_type": "episodic",
            "importance": 7.5,
        }
        e = MemoryEntry.from_dict(d)
        assert e.importance == 7.5
        assert e.tags == []
        assert e.metadata == {}


class TestTemporalValidity:
    def test_is_expired_at_boundary(self):
        now = time.time()
        e = MemoryEntry(content="x", memory_type=MemoryType.SEMANTIC, expires_at=now)
        # expires_at is absolute timestamp; if now == expires_at, it's expired
        time.sleep(0.01)
        assert e.is_expired()

    def test_is_not_expired_slightly_future(self):
        e = MemoryEntry(content="x", memory_type=MemoryType.SEMANTIC, expires_at=time.time() + 0.1)
        assert not e.is_expired()
