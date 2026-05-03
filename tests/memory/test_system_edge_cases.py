"""Edge-case tests for AgentMemorySystem: empty ops, multi-agent, boundaries."""

import os
import tempfile
import time

import pytest

from kimix.memory.system import AgentMemorySystem
from kimix.memory.types import MemoryEntry, MemoryType


class TestSystemEmptyOperations:
    def test_recall_empty_system(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path)
            results = sys.recall("anything")
            assert results["working"] == []
            assert results["short_term"] == []
            assert results["long_term"] == []
        finally:
            os.unlink(path)

    def test_get_context_empty_system(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path)
            ctx = sys.get_context_for_llm("query")
            assert ctx == ""
        finally:
            os.unlink(path)

    def test_consolidate_empty_short_term(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path)
            sys._consolidate()  # should not raise
            assert sys.long_term.count() == 0
        finally:
            os.unlink(path)

    def test_archive_empty_long_term(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sys = AgentMemorySystem(
                ltm_path=f"{tmpdir}/ltm.json",
                agent_id="empty_test",
            )
            sys.cold_storage = type(sys.cold_storage)(archive_dir=f"{tmpdir}/cold")
            sys.archive_to_cold_storage()  # should not raise
            assert sys.cold_storage.list_archives() == []


class TestSystemMultiAgent:
    def test_agents_isolated_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sys_a = AgentMemorySystem(ltm_path=f"{tmpdir}/ltm.json", agent_id="agent_a")
            sys_b = AgentMemorySystem(ltm_path=f"{tmpdir}/ltm.json", agent_id="agent_b")
            sys_a.remember("secret a", importance=9.0)
            sys_b.remember("secret b", importance=9.0)
            assert len(sys_a.recall("secret", use_long=True)["long_term"]) == 1
            assert sys_a.recall("secret", use_long=True)["long_term"][0].content == "secret a"

    def test_cold_storage_per_agent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sys_a = AgentMemorySystem(ltm_path=f"{tmpdir}/ltm.json", agent_id="a")
            sys_b = AgentMemorySystem(ltm_path=f"{tmpdir}/ltm.json", agent_id="b")
            # Cold storage dirs should differ
            assert sys_a.cold_storage.archive_dir != sys_b.cold_storage.archive_dir


class TestSystemBoundaryConditions:
    def test_perceive_zero_importance(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path)
            entry = sys.perceive("zero", importance=0.0)
            assert entry.importance == 0.0
        finally:
            os.unlink(path)

    def test_remember_max_importance(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path)
            entry = sys.remember("max", importance=10.0)
            assert entry.importance == 10.0
        finally:
            os.unlink(path)

    def test_context_size_zero(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path)
            sys.perceive("test")
            results = sys.recall("test", context_size=0)
            assert all(v == [] for v in results.values())
        finally:
            os.unlink(path)

    def test_recall_negative_context_size(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path)
            sys.perceive("test")
            results = sys.recall("test", context_size=-1)
            assert all(v == [] for v in results.values())
        finally:
            os.unlink(path)

    def test_interaction_count_increment(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path)
            assert sys.interaction_count == 0
            sys.perceive("a")
            assert sys.interaction_count == 1
        finally:
            os.unlink(path)

    def test_consolidation_interval_boundary(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path)
            sys.consolidation_interval = 2
            sys.perceive("first", importance=9.0)
            assert sys.interaction_count == 1
            sys.perceive("second", importance=9.0)
            assert sys.interaction_count == 2
            # After 2 perceptions, consolidation should have triggered
        finally:
            os.unlink(path)
