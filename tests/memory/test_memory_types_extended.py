"""Extended tests for MemoryType and MemoryEntry enhancements."""

import time

import pytest

from kimix.memory.types import MemoryEntry, MemoryType


class TestMemoryTypeExtended:
    def test_all_memory_types_exist(self):
        expected = {
            "WORKING", "EPISODIC", "SEMANTIC", "PROCEDURAL",
            "SCAR", "RULE", "COMPILED_TRUTH", "ENTITY", "FACT",
            "WORKFLOW", "TASK", "TRIGGER", "PROGRAMMATIC", "COLD_ARCHIVE",
        }
        assert {mt.name for mt in MemoryType} == expected


class TestMemoryEntryTemporalValidity:
    def test_not_expired_when_none(self):
        e = MemoryEntry(content="x", memory_type=MemoryType.SEMANTIC)
        assert e.expires_at is None
        assert not e.is_expired()

    def test_expired(self):
        e = MemoryEntry(content="x", memory_type=MemoryType.SEMANTIC, expires_at=time.time() - 10)
        assert e.is_expired()

    def test_not_expired_future(self):
        e = MemoryEntry(content="x", memory_type=MemoryType.SEMANTIC, expires_at=time.time() + 100)
        assert not e.is_expired()

    def test_agent_id_default(self):
        e = MemoryEntry(content="x", memory_type=MemoryType.SEMANTIC)
        assert e.agent_id == "default"

    def test_to_dict_includes_new_fields(self):
        e = MemoryEntry(content="x", memory_type=MemoryType.SEMANTIC, expires_at=12345.0, agent_id="agent_a")
        d = e.to_dict()
        assert d["expires_at"] == 12345.0
        assert d["agent_id"] == "agent_a"

    def test_from_dict_roundtrip(self):
        original = MemoryEntry(
            content="roundtrip",
            memory_type=MemoryType.ENTITY,
            importance=7.0,
            tags=["a", "b"],
            metadata={"k": "v"},
            expires_at=99999.0,
            agent_id="agent_x",
        )
        d = original.to_dict()
        restored = MemoryEntry.from_dict(d)
        assert restored.content == original.content
        assert restored.memory_type == original.memory_type
        assert restored.importance == original.importance
        assert restored.tags == original.tags
        assert restored.metadata == original.metadata
        assert restored.expires_at == original.expires_at
        assert restored.agent_id == original.agent_id

    def test_from_dict_minimal(self):
        d = {"content": "minimal", "memory_type": "semantic"}
        e = MemoryEntry.from_dict(d)
        assert e.content == "minimal"
        assert e.importance == 1.0
        assert e.agent_id == "default"
